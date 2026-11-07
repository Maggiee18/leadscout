"""Lead analysis: AI (or offline rules) extraction, deterministic scoring, SMS draft.

    The AI reads. The rules score. The agent decides.
"""

import io
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd
from fastapi import HTTPException

import config
from listings import match_listings
from scorer import (
    confidence,
    extract_with_llm,
    extract_with_rules,
    format_money,
    format_timeline,
    interest_level,
    llm_followup,
    next_action,
    score_lead,
    template_followup,
)

log = logging.getLogger("leadscout.analysis")

MAX_BATCH = 50
MAX_MESSAGE_CHARS = 2000
MAX_NAME_CHARS = 80
BATCH_WORKERS = 2  # Groq free tier: 8k tokens/minute, so keep concurrency low

# ---------- AI client and honest status tracking ----------

_client = None
_client_lock = threading.Lock()
_ai_state = {"last_ok": None, "last_error": None, "last_error_at": None}


def get_client():
    global _client
    if not config.API_KEY:
        return None
    with _client_lock:
        if _client is None:
            from openai import OpenAI
            _client = OpenAI(api_key=config.API_KEY, base_url=config.BASE_URL, timeout=config.LLM_TIMEOUT,
                             max_retries=2)  # also honours Groq's "retry after" on 429
    return _client


def _describe_ai_error(exc):
    name = type(exc).__name__
    if "Timeout" in name:
        return "The AI service timed out"
    if "Authentication" in name or "PermissionDenied" in name:
        return "The AI API key was rejected"
    if "RateLimit" in name:
        return "the AI provider's per-minute limit was reached"
    if "Connection" in name:
        return "The AI service could not be reached"
    if isinstance(exc, RuntimeError) and "valid JSON" in str(exc):
        return "The AI returned unreadable output twice"
    return "The AI service returned an error"


def _record_ai(ok, reason=None):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if ok:
        _ai_state["last_ok"] = now
    else:
        _ai_state["last_error"], _ai_state["last_error_at"] = reason, now


# ---------- Core analysis ----------

INTENT_LABEL = {"buy": "Buy", "sell": "Sell", "rent": "Rent", "invest": "Invest"}
FIN_LABEL = {"cash": "Cash", "preapproved": "Pre-approved", "mortgage": "Needs mortgage"}
SENTIMENT_LABEL = {"positive": "Positive", "neutral": "Neutral", "negative": "Negative"}


def property_label(f):
    beds, ptype = f.get("bedrooms"), f.get("property_type")
    if beds and ptype:
        return f"{beds} bed {ptype}"
    if beds:
        return f"{beds} bed"
    return ptype


def display_fields(f):
    return {
        "property": property_label(f),
        "sentiment": SENTIMENT_LABEL.get(f.get("sentiment")),
        "intent": INTENT_LABEL.get(f["intent"]),
        "timeline": format_timeline(f["timeline_months"]),
        "budget": format_money(f["budget"], f["currency"]),
        "location": f["location"],
        "financing": FIN_LABEL.get(f["financing"]),
        "motivation": f["motivation"],
    }


def analyze(name, message, use_ai=True, ai_allowed=None, ai_sms=True, listings=None, contact=None):
    """Extract (AI or rules), score (rules), recommend, draft SMS. Never raises for AI.

    ai_allowed: optional callable returning None (go ahead) or a reason string to skip AI.
    ai_sms: draft the reply with AI too (single leads) or with templates (batches, to save quota).
    """
    client = get_client() if use_ai else None
    skip_reason = ai_allowed() if (client is not None and ai_allowed is not None) else None
    if skip_reason:
        client = None
    engine, fallback_reason, sms_engine = "fallback", None, "template"

    if client is None:
        fallback_reason = ("No AI API key configured" if not config.API_KEY else
                           "AI turned off for this request" if not use_ai else skip_reason)
        fields = extract_with_rules(message)
    else:
        try:
            fields = extract_with_llm(message, client, config.MODEL)
            engine = "ai"
            _record_ai(True)
        except Exception as exc:  # network, auth, rate limit, bad JSON
            fallback_reason = _describe_ai_error(exc)
            fallback_reason = fallback_reason[0].upper() + fallback_reason[1:]
            _record_ai(False, fallback_reason)
            log.warning("AI extraction failed (%s); using offline rules", type(exc).__name__)
            fields = extract_with_rules(message)

    for key, value in (contact or {}).items():  # details typed into the form or a CSV column win
        if value:
            fields[key] = value
    score, tier, points = score_lead(fields)
    matches = match_listings(fields, listings or [])
    sms = template_followup(name, fields, tier, matches=len(matches))
    if engine == "ai" and ai_sms:
        try:
            brief = [{"title": m["title"], "price": m["price_label"], "location": m["location"]} for m in matches]
            sms = llm_followup(name, message, fields, tier, client, config.MODEL, matches=brief)
            sms_engine = "ai"
        except Exception as exc:
            log.warning("AI SMS failed (%s); using template", type(exc).__name__)

    return {
        "id": uuid.uuid4().hex,
        "name": name or fields.get("customer_name") or None,
        "phone": fields.get("phone"),
        "email": fields.get("email"),
        "confidence": confidence(fields, engine),
        "interest": interest_level(fields, tier),
        "matches": matches,
        "message": message,
        "fields": fields,
        "display": display_fields(fields),
        "score": score,
        "tier": tier,
        "raw_total": sum(p for _, _, p in points),
        "breakdown": [{"factor": fa, "reason": r, "points": p} for fa, r, p in points],
        "next_action": next_action(fields, tier),
        "sms": sms,
        "engine": engine,
        "sms_engine": sms_engine,
        "model": config.MODEL if engine == "ai" else None,
        "fallback_reason": fallback_reason,
        "analyzed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }



# ---------- Batch ----------

COLUMN_ALIASES = {
    "message": ("message", "inquiry", "enquiry", "query", "text", "comment", "comments", "notes", "note",
                "requirement", "requirements", "description", "lead message", "lead", "details"),
    "name": ("name", "customer", "customer name", "client", "client name", "full name", "lead name",
             "contact name", "buyer", "buyer name"),
    "phone": ("phone", "mobile", "phone number", "mobile number", "contact number", "whatsapp", "contact", "cell"),
    "email": ("email", "e-mail", "email address", "mail"),
}
EXCEL_MAGIC = b"PK\x03\x04"


def _table_from_upload(data: bytes, filename: str):
    """Read an uploaded CSV, TSV or Excel file into a DataFrame of strings, or raise a friendly 400."""
    name = (filename or "").lower()
    if not name.endswith((".csv", ".txt", ".tsv", ".xlsx")):
        raise HTTPException(400, "Please upload a .csv or .xlsx file.")
    if not data.strip():
        raise HTTPException(400, "The file is empty.")
    is_excel = data[:4] == EXCEL_MAGIC
    if name.endswith(".xlsx") != is_excel:
        raise HTTPException(400, "The file's contents don't match its extension. Save it again as CSV or Excel.")
    if is_excel:
        try:
            df = pd.read_excel(io.BytesIO(data), dtype=str, engine="openpyxl").fillna("")
        except Exception:
            raise HTTPException(400, "The Excel file couldn't be read. Save it again as .xlsx or .csv.") from None
        return df, 0
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1252")
        except UnicodeDecodeError:
            raise HTTPException(400, "The file is not readable text. Save it as CSV (UTF-8).") from None
    if "\x00" in text:
        raise HTTPException(400, "This doesn't look like a CSV file.")
    try:
        df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False, sep=_delimiter(text),
                         engine="c", index_col=False, skipinitialspace=True, on_bad_lines="skip",
                         skip_blank_lines=True)
    except Exception:
        raise HTTPException(400, "The CSV couldn't be parsed. Check commas and quotes.") from None
    lines = sum(1 for ln in text.splitlines() if ln.strip()) - 1  # rough: rows with broken quoting or extra cells
    return df, max(0, lines - len(df))


def _delimiter(text):
    """Comma, semicolon or tab, whichever the header row uses most (outside quotes)."""
    header = next((ln for ln in text.splitlines() if ln.strip()), "")
    header = re.sub(r'"[^"]*"', "", header)
    counts = {d: header.count(d) for d in (",", ";", "\t")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def _find_column(columns, key):
    for alias in COLUMN_ALIASES[key]:
        for c in columns:
            if c == alias or c.replace("_", " ") == alias:
                return c
    return None


def read_leads_file(data: bytes, filename: str = "leads.csv"):
    """Rows of {name, message, phone, email} from an upload, plus how many rows were skipped."""
    df, bad_lines = _table_from_upload(data, filename)
    df.columns = [str(c).strip().lower() for c in df.columns]
    cols = {k: _find_column(df.columns, k) for k in COLUMN_ALIASES}
    if not cols["message"]:
        if len(df.columns) == 1:
            cols["message"] = df.columns[0]
        else:
            raise HTTPException(400, "The file needs a column with the customer's message, called 'message' "
                                     "(or inquiry, enquiry, comments). A 'name' column is optional.")
    rows, skipped = [], bad_lines
    for i, r in df.iterrows():
        msg = str(r.get(cols["message"], "") or "").strip()
        if len(msg) < 3:
            skipped += 1
            continue
        cell = {k: str(r.get(c, "") or "").strip() if c else "" for k, c in cols.items()}
        rows.append({"name": cell["name"][:MAX_NAME_CHARS] or None, "message": msg[:MAX_MESSAGE_CHARS],
                     "phone": cell["phone"][:20] or None, "email": cell["email"][:120] or None, "row": i + 2})
    if not rows:
        raise HTTPException(400, "No leads found. Every row's message is empty.")
    if len(rows) > MAX_BATCH:
        raise HTTPException(400, f"This file has {len(rows)} leads. The limit is {MAX_BATCH} "
                                 f"per upload, so split it into smaller files.")
    return rows, skipped


def _read_csv(data: bytes):
    """Backwards compatible helper used by the sample batch."""
    return read_leads_file(data, "sample.csv")


def _run_batch(rows, skipped=0, use_ai=True, time_budget=None, listings=None):
    """Score many leads. AI extraction runs until it fails twice or the time budget is used,
    then the rest use offline rules, each labelled with the honest reason."""
    failures = {"n": 0, "last": None}
    lock = threading.Lock()
    deadline = time.monotonic() + (config.BATCH_AI_SECONDS if time_budget is None else time_budget)

    def ai_allowed():
        if failures["n"] >= 2:
            return f"AI paused for the rest of this batch: {failures['last'] or 'repeated errors'}"
        if time.monotonic() > deadline:
            return "AI time budget for this batch was used up"
        return None

    def work(row):
        if isinstance(row, tuple):  # (name, message)
            row = {"name": row[0], "message": row[1]}
        res = analyze(row.get("name"), row["message"], use_ai=use_ai, ai_allowed=ai_allowed,
                      ai_sms=config.BATCH_AI_SMS, listings=listings,
                      contact={"phone": row.get("phone"), "email": row.get("email")})
        if not res["name"] and row.get("row"):
            res["name"] = f"Lead {row['row'] - 1}"
        reason = res["fallback_reason"] or ""
        if res["engine"] == "fallback" and use_ai and config.API_KEY and not reason.startswith("AI "):
            with lock:
                failures["n"] += 1
                failures["last"] = reason[0].lower() + reason[1:] if reason else reason
        return res

    workers = BATCH_WORKERS if (use_ai and config.API_KEY) else 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(work, rows))
    results.sort(key=lambda r: r["score"], reverse=True)
    n = len(results)
    tiers = {t: sum(r["tier"] == t for r in results) for t in ("Hot", "Warm", "Cold")}
    return {
        "count": n,
        "skipped_rows": skipped,
        "summary": {"hot": tiers["Hot"], "warm": tiers["Warm"], "cold": tiers["Cold"],
                    "average_score": round(sum(r["score"] for r in results) / n, 1)},
        "engines": {"ai": sum(r["engine"] == "ai" for r in results),
                    "fallback": sum(r["engine"] == "fallback" for r in results)},
        "results": results,
    }


