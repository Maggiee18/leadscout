"""Lead analysis: AI (or offline rules) extraction, deterministic scoring, SMS draft.

    The AI reads. The rules score. The agent decides.
"""

import io
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd
from fastapi import HTTPException

import config
from scorer import (
    extract_with_llm,
    extract_with_rules,
    format_money,
    format_timeline,
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


def display_fields(f):
    return {
        "intent": INTENT_LABEL.get(f["intent"]),
        "timeline": format_timeline(f["timeline_months"]),
        "budget": format_money(f["budget"], f["currency"]),
        "location": f["location"],
        "financing": FIN_LABEL.get(f["financing"]),
        "motivation": f["motivation"],
    }


def analyze(name, message, use_ai=True, ai_allowed=None, ai_sms=True):
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

    score, tier, points = score_lead(fields)
    sms = template_followup(name, fields, tier)
    if engine == "ai" and ai_sms:
        try:
            sms = llm_followup(name, message, fields, tier, client, config.MODEL)
            sms_engine = "ai"
        except Exception as exc:
            log.warning("AI SMS failed (%s); using template", type(exc).__name__)

    return {
        "id": uuid.uuid4().hex,
        "name": name or None,
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

def _read_csv(data: bytes):
    if not data.strip():
        raise HTTPException(400, "The CSV file is empty.")
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
        df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False,
                         skipinitialspace=True)
    except Exception:
        raise HTTPException(400, "The CSV couldn't be parsed. Check commas and quotes.") from None
    df.columns = [str(c).strip().lower() for c in df.columns]
    if "message" not in df.columns:
        raise HTTPException(400, "The CSV needs a 'message' column (and optionally 'name').")
    rows, skipped = [], 0
    for i, r in df.iterrows():
        msg = str(r.get("message", "")).strip()
        if len(msg) < 3:
            skipped += 1
            continue
        name = str(r.get("name", "")).strip()[:MAX_NAME_CHARS] if "name" in df.columns else ""
        rows.append((name or f"Lead {i + 1}", msg[:MAX_MESSAGE_CHARS]))
    if not rows:
        raise HTTPException(400, "No leads found. Every row's message is empty.")
    if len(rows) > MAX_BATCH:
        raise HTTPException(400, f"This file has {len(rows)} leads. The limit is {MAX_BATCH} "
                                 f"per upload, so split it into smaller files.")
    return rows, skipped


def _run_batch(rows, skipped=0, use_ai=True, time_budget=None):
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
        res = analyze(row[0], row[1], use_ai=use_ai, ai_allowed=ai_allowed, ai_sms=config.BATCH_AI_SMS)
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


