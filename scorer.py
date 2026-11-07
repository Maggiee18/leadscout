"""
LeadScout scoring engine.

Design: the LLM only EXTRACTS facts from a lead's message into a fixed JSON
shape. The SCORE is computed by plain Python rules on those facts, so the
same facts always give the same score and every point can be explained.

    The AI reads. The rules score. The agent decides.
"""

import json
import math
import re

INTENTS = ("buy", "sell", "rent", "invest", "unknown")
PROPERTY_TYPES = ("Apartment", "House", "Villa", "Plot", "Commercial")
SENTIMENTS = ("positive", "neutral", "negative")
FINANCING = ("cash", "preapproved", "mortgage", "unknown")
CURRENCIES = ("USD", "INR")

EMPTY_FIELDS = {
    "intent": "unknown",        # buy / sell / rent / invest / unknown
    "timeline_months": None,    # number, 0 means right now
    "budget": None,             # number, in `currency`
    "currency": None,           # "USD" / "INR" / None
    "location": None,           # text
    "financing": "unknown",     # cash / preapproved / mortgage / unknown
    "motivation": None,         # short text, why they are moving
    "red_flags": [],            # e.g. "just browsing", "no budget"
    "summary": "",
    # customer details (shown and searchable, never part of the score)
    "customer_name": None,      # name the lead gives in the message
    "phone": None,              # read by fixed patterns, never by the AI
    "email": None,
    "property_type": None,      # Apartment / House / Villa / Plot / Commercial
    "bedrooms": None,           # e.g. 3 for "3BHK" or "3 bed"
    "sentiment": "neutral",     # positive / neutral / negative (tone of the message)
}

EXTRACT_PROMPT = """You are a real estate lead analyst. Read the lead's message and
extract facts. Only use what the message says; never guess or invent.

Return ONLY a JSON object, no markdown, with exactly these keys:
{
  "intent": "buy" | "sell" | "rent" | "invest" | "unknown",
  "timeline_months": number or null (0 if they say now / ASAP, 0.5 for two weeks; for a range like "3 to 6 months" use the later end; if they give several timelines use the soonest),
  "budget": number or null (the full amount: 450k -> 450000, 80 lakhs -> 8000000, 1.2 crore -> 12000000; for rentals the monthly rent),
  "currency": "USD" | "INR" | null ("INR" for rupees, lakhs, crores or Rs; "USD" for $ or dollars; null if no budget),
  "location": string or null (city or area exactly as written),
  "financing": "cash" | "preapproved" | "mortgage" | "unknown",
  "motivation": string or null (why they are moving, a few words, only if stated),
  "red_flags": list of short strings for signals the lead may not be serious or able to act, e.g. "just browsing", "unsure if moving", "unrealistic budget", "contradictory details"; [] if none. Never list missing information (no budget, unknown financing, no timeline) as a red flag,
  "summary": one sentence summary of the lead,
  "customer_name": string or null (only if the writer states their own name, e.g. "I'm Ravi"),
  "property_type": "Apartment" | "House" | "Villa" | "Plot" | "Commercial" | null (flat or condo -> Apartment; bungalow or independent house -> House; land -> Plot; office or shop -> Commercial),
  "bedrooms": number or null (3BHK or 3 bed -> 3),
  "sentiment": "positive" | "neutral" | "negative" (the tone: eager or excited -> positive; frustrated or annoyed -> negative)
}
Phone numbers and email addresses are never a budget."""


# ---------- JSON safety ----------

def parse_json(text):
    """LLMs sometimes wrap JSON in ``` fences or add words. Pull out the object."""
    text = re.sub(r"```(?:json)?", "", text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in model output")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("Model output is not a JSON object")
    return data


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.replace(",", "").strip()
        if not value:
            return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(n) or math.isinf(n) or n < 0:
        return None
    return n


def _text(value, limit=160):
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("null", "none", "unknown", "n/a", "not provided"):
        return None
    return s[:limit]


def clean_fields(raw):
    """Fill missing keys and fix wrong types so the scorer never crashes."""
    raw = dict(raw or {})
    if "budget" not in raw and "budget_usd" in raw:  # older key name
        raw["budget"] = raw["budget_usd"]
        raw.setdefault("currency", "USD")
    f = dict(EMPTY_FIELDS)
    f["red_flags"] = []
    f.update({k: v for k, v in raw.items() if k in EMPTY_FIELDS})

    f["timeline_months"] = _num(f["timeline_months"])
    f["budget"] = _num(f["budget"]) or None

    cur = str(f["currency"] or "").upper().strip()
    cur = {"RS": "INR", "₹": "INR", "RUPEES": "INR", "$": "USD", "DOLLARS": "USD"}.get(cur, cur)
    f["currency"] = cur if cur in CURRENCIES and f["budget"] else (
        "USD" if f["budget"] else None)

    intent = str(f["intent"] or "unknown").lower().strip()
    intent = {"buying": "buy", "purchase": "buy", "buyer": "buy", "selling": "sell",
              "seller": "sell", "renting": "rent", "lease": "rent", "investing": "invest",
              "investment": "invest", "investor": "invest"}.get(intent, intent)
    f["intent"] = intent if intent in INTENTS else "unknown"

    fin = re.sub(r"[\s\-_]", "", str(f["financing"] or "unknown").lower())
    fin = {"preapproval": "preapproved", "approved": "preapproved", "loan": "mortgage",
           "homeloan": "mortgage", "needsmortgage": "mortgage", "none": "unknown"}.get(fin, fin)
    f["financing"] = fin if fin in FINANCING else "unknown"

    flags = f["red_flags"]
    if not isinstance(flags, list):
        flags = [flags] if flags else []
    f["red_flags"] = [s for s in (_text(x, 60) for x in flags) if s][:5]

    f["customer_name"] = _person_name(f["customer_name"])
    ptype = str(f["property_type"] or "").strip().lower()
    ptype = {"flat": "apartment", "condo": "apartment", "condominium": "apartment", "bungalow": "house",
             "independent house": "house", "home": "house", "land": "plot", "office": "commercial",
             "shop": "commercial"}.get(ptype, ptype)
    f["property_type"] = next((t for t in PROPERTY_TYPES if t.lower() == ptype), None)
    beds = _num(f["bedrooms"])
    f["bedrooms"] = int(beds) if beds is not None and 0 < beds <= 20 and float(beds).is_integer() else None
    sent = str(f["sentiment"] or "neutral").lower().strip()
    f["sentiment"] = sent if sent in SENTIMENTS else "neutral"
    f["phone"] = _text(f["phone"], 20)
    f["email"] = _text(f["email"], 120)
    f["location"] = _text(f["location"], 80)
    f["motivation"] = _text(f["motivation"], 80)
    f["summary"] = _text(f["summary"], 300) or ""
    return f


# ---------- Contact details and other facts read by fixed patterns ----------

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_CANDIDATE = re.compile(r"(?<![\w₹$])\+?\d[\d \t().-]{8,18}\d(?!\s?(?:k|m|l|cr|lakhs?|lacs?|crores?|million|thousand)\b)",
                              re.I)
_NAME = re.compile(r"\b(?i:i am|i'm|im|this is|my name is|name's|name is)\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)")
_NOT_NAMES = {"Looking", "Interested", "Planning", "Moving", "Selling", "Buying", "Hoping", "Ready", "Not",
              "Just", "Searching", "Here", "Writing", "Reaching", "Relocating", "Currently", "From"}


def _person_name(value):
    s = _text(value, 60)
    if not s or any(ch.isdigit() for ch in s) or "@" in s:
        return None
    return s


def find_phone(text):
    """A phone number: 10 to 13 digits, possibly spaced, with no comma (money uses commas)."""
    for m in _PHONE_CANDIDATE.finditer(text or ""):
        raw = m.group(0).strip()
        digits = re.sub(r"\D", "", raw)
        if "," in raw or not 10 <= len(digits) <= 13:
            continue
        return ("+" if raw.startswith("+") else "") + digits
    return None


def find_email(text):
    m = _EMAIL.search(text or "")
    return m.group(0).lower()[:120] if m else None


def strip_contacts(text):
    """Remove emails and phone numbers so they can never be read as a budget."""
    text = _EMAIL.sub(" ", text or "")
    def drop(m):
        digits = re.sub(r"\D", "", m.group(0))
        return " " if "," not in m.group(0) and 10 <= len(digits) <= 13 else m.group(0)
    return _PHONE_CANDIDATE.sub(drop, text)


def find_name(text):
    m = _NAME.search(text or "")
    if not m:
        return None
    first = m.group(1).split()[0]
    return None if first in _NOT_NAMES else _person_name(m.group(1))


def find_property(text):
    t = (text or "").lower()
    ptype = None
    for pat, label in [(r"\b(apartment|flat|condo|condominium|penthouse|studio)\b", "Apartment"),
                       (r"\bvilla\b", "Villa"),
                       (r"\b(house|home|bungalow|townhouse|duplex)\b", "House"),
                       (r"\b(plot|land|site)\b", "Plot"),
                       (r"\b(office|shop|showroom|commercial|warehouse)\b", "Commercial")]:
        if re.search(pat, t):
            ptype = label
            break
    beds = re.search(r"\b(\d{1,2})\s?(?:bhk|bed(?:room)?s?|br)\b", t)
    bedrooms = int(beds.group(1)) if beds and 0 < int(beds.group(1)) <= 20 else None
    if bedrooms and not ptype and re.search(r"\d\s?bhk\b", t):
        ptype = "Apartment"
    return ptype, bedrooms


_POSITIVE = r"\b(excited|love|eager|keen|ready|definitely|really want|can't wait|happy|thrilled|asap|urgent(ly)?)\b"
_NEGATIVE = r"\b(frustrat\w*|annoy\w*|disappoint\w*|unhappy|angry|upset|bad experience|tired of|fed up)\b"


def sentiment_with_rules(text):
    t = (text or "").lower()
    if re.search(_NEGATIVE, t):
        return "negative"
    if re.search(_POSITIVE, t):
        return "positive"
    return "neutral"


def add_pattern_facts(fields, message):
    """Facts that fixed patterns read better than any model: contact details, and missing details
    the model skipped. Also drops a budget that is really a phone number."""
    f = dict(fields)
    f["phone"] = find_phone(message)
    f["email"] = find_email(message)
    if f["budget"] and f["phone"] and re.sub(r"\D", "", f["phone"]).endswith(str(int(f["budget"]))):
        f["budget"], f["currency"] = None, None
    if not f.get("customer_name"):
        f["customer_name"] = find_name(message)
    ptype, beds = find_property(message)
    f["property_type"] = f.get("property_type") or ptype
    f["bedrooms"] = f.get("bedrooms") or beds
    return f


# ---------- Extraction: LLM ----------

def _model_kwargs(model):
    """Reasoning models (e.g. openai/gpt-oss-*) are faster with low reasoning effort."""
    return {"reasoning_effort": "low"} if "gpt-oss" in (model or "") else {}


def extract_with_llm(message, client, model):
    last_error = None
    for _ in range(2):  # one retry if the JSON comes back broken
        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            max_tokens=700,  # Groq reserves max_tokens against the per-minute quota
            **_model_kwargs(model),
            messages=[
                {"role": "system", "content": EXTRACT_PROMPT},
                # Phone numbers and emails never leave LeadScout: rules read them from the original text
                {"role": "user", "content": strip_contacts(message)},
            ],
        )
        try:
            return clean_fields(add_pattern_facts(clean_fields(parse_json(resp.choices[0].message.content)),
                                                  message))
        except (ValueError, json.JSONDecodeError) as e:
            last_error = e
    raise RuntimeError(f"Model did not return valid JSON: {last_error}")


# ---------- Extraction: offline rules (fallback, no API key needed) ----------

WORD_NUMBERS = {"one": 1, "a": 1, "an": 1, "two": 2, "couple of": 2, "three": 3, "few": 3,
                "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                "ten": 10, "eleven": 11, "twelve": 12}

_MONEY = re.compile(
    r"(?P<pre>\$|₹|rs\.?|inr|usd)?\s?(?P<num>\d[\d,]*(?:\.\d+)?)\s?"
    r"(?P<unit>k|m|million|thousand|lakhs?|lacs?|l|crores?|cr)?\b(?P<post>\s?(?:dollars|rupees))?",
    re.I)


def _money(text):
    """Return (amount, currency) for the largest plausible budget, else (None, None)."""
    best = (None, None)
    for m in _MONEY.finditer(text):
        try:
            value = float(m.group("num").replace(",", ""))
        except ValueError:
            continue
        pre = (m.group("pre") or "").lower().rstrip(".")
        post = (m.group("post") or "").strip().lower()
        unit = (m.group("unit") or "").lower()
        currency = None
        if pre in ("$", "usd") or post == "dollars":
            currency = "USD"
        if pre in ("₹", "rs", "inr") or post == "rupees":
            currency = "INR"
        if unit in ("k", "thousand"):
            value *= 1_000
        elif unit in ("m", "million"):
            value *= 1_000_000
        elif unit.startswith(("lakh", "lac")) or unit == "l":
            value *= 100_000
            currency = "INR"
        elif unit.startswith("cr"):
            value *= 10_000_000
            currency = "INR"
        # explicit money (symbol or unit) can be small, e.g. rent of $2,400/month;
        # a bare number must be big to avoid matching "3 bed" or "2 weeks"
        explicit = bool(currency or unit)
        if (explicit and value >= 100) or value >= 50_000:
            if best[0] is None or value > best[0]:
                best = (value, currency or "USD")
    return best


def _timeline(text):
    """Months until they want to move. Rules, so the same words always give the same number:
    a range ("3 to 6 months") counts by its later end, so urgency is never overstated;
    when several timelines are mentioned ("ASAP, at the latest 6 months") the soonest one counts."""
    t = text.lower()
    found = []
    if re.search(r"\b(asap|right away|immediately|this week|urgent(ly)?|right now)\b", t):
        found.append(0)
    if re.search(r"\b(next|this|within a|in a|within one) month\b", t):
        found.append(1)
    if re.search(r"\b(next|within a|in a|within one) year\b", t):
        found.append(12)
    words = "|".join(sorted(map(re.escape, WORD_NUMBERS), key=len, reverse=True))
    num = rf"(?:\d+(?:\.\d+)?|\b(?:{words}))"
    for m in re.finditer(rf"({num})(?:\s*(?:-|to|or)\s*({num}))?\s*(months?|weeks?|years?)\b", t):
        raw = m.group(2) or m.group(1)
        n = float(WORD_NUMBERS[raw]) if raw in WORD_NUMBERS else float(raw)
        unit = m.group(3)
        found.append(round(n / 4, 2) if unit.startswith("week") else n * 12 if unit.startswith("year") else n)
    return min(found) if found else None


MOTIVATIONS = [
    (r"relocat|transfer(red)?|new job|job (move|change)", "job relocation"),
    (r"baby|growing family|upsiz|more space|bigger (home|house|place)", "growing family / upsizing"),
    (r"downsiz|empty nest", "downsizing"),
    (r"retir", "retirement"),
    (r"divorc|separat", "life change"),
    (r"investment|rental income|returns?\b", "investment returns"),
    (r"closer to (work|school|family)|commute", "closer to work or family"),
    (r"lease (is )?(ending|ends|up)|current lease", "current lease ending"),
]


def extract_with_rules(message):
    t = message.lower()
    intent = "unknown"
    if re.search(r"\b(sell|selling|list my|listing my)\b", t):
        intent = "sell"
    elif re.search(r"\b(rent|renting|lease)\b", t):
        intent = "rent"
    elif re.search(r"\b(buy|buying|purchase|\d\s?(bed|bhk)|(need|want|looking for) an? "
                   r"(home|house|condo|flat|apartment|villa|plot))\b", t):
        intent = "buy"
    elif "invest" in t:
        intent = "invest"

    financing = "unknown"
    if re.search(r"\bcash\b", t):
        financing = "cash"
    elif re.search(r"\bnot (yet )?(pre-?)?approved\b", t):
        financing = "mortgage"
    elif re.search(r"pre-?approved|loan (is )?approved|approved for", t):
        financing = "preapproved"
    elif re.search(r"\b(mortgage|loan|financing)\b", t):
        financing = "mortgage"

    loc = re.search(r"\b(?:in|at|near|around) ([A-Z][a-z]+(?:[ ,]+[A-Z][a-z]+)*)", message)
    flags = []
    if re.search(r"just (looking|browsing|curious)|curious what", t):
        flags.append("just browsing")
    if re.search(r"not sure|maybe|someday|no idea", t):
        flags.append("unsure")

    motivation = next((label for pat, label in MOTIVATIONS if re.search(pat, t)), None)
    budget, currency = _money(strip_contacts(message))

    return clean_fields(add_pattern_facts({
        "intent": intent,
        "timeline_months": _timeline(message),
        "budget": budget,
        "currency": currency,
        "location": loc.group(1).strip(", ") if loc else None,
        "financing": financing,
        "motivation": motivation,
        "red_flags": flags,
        "summary": "Extracted with offline rules (no AI).",
        "sentiment": sentiment_with_rules(message),
    }, message))


# ---------- Formatting helpers ----------

def format_money(amount, currency):
    if amount is None:
        return None
    if currency == "INR":
        if amount >= 10_000_000:
            return f"₹{amount / 10_000_000:,.2f}".rstrip("0").rstrip(".") + " crore"
        if amount >= 100_000:
            return f"₹{amount / 100_000:,.2f}".rstrip("0").rstrip(".") + " lakh"
        return f"₹{amount:,.0f}"
    return f"${amount:,.0f}"


def format_timeline(months):
    if months is None:
        return None
    if months == 0:
        return "Immediately"
    if months < 1:
        weeks = max(1, round(months * 4))
        return f"Within {weeks} week{'s' if weeks > 1 else ''}"
    if months >= 12 and months % 12 == 0:
        y = int(months // 12)
        return f"Within {y} year{'s' if y > 1 else ''}"
    m = int(months) if float(months).is_integer() else months
    return f"Within {m} month{'s' if m != 1 else ''}"


# ---------- Scoring: deterministic, explainable ----------

HOT_MIN, WARM_MIN = 70, 40  # Hot is 70 to 100, Warm 40 to 69, Cold 0 to 39


def tier_for(score):
    return "Hot" if score >= HOT_MIN else "Warm" if score >= WARM_MIN else "Cold"


def score_lead(f):
    points = []  # (factor, reason, points)

    tl = f["timeline_months"]
    if tl is None:
        points.append(("Timeline", "No timeline given", 5))
    elif tl <= 1:
        points.append(("Timeline", "Moving within a month", 30))
    elif tl <= 3:
        points.append(("Timeline", "Moving within 3 months", 25))
    elif tl <= 6:
        points.append(("Timeline", "Moving within 6 months", 15))
    else:
        points.append(("Timeline", "Timeline over 6 months", 5))

    fin = f["financing"]
    if fin in ("cash", "preapproved"):
        label = "cash buyer" if fin == "cash" else "pre-approved"
        points.append(("Financing", f"Financing ready ({label})", 25))
    elif fin == "mortgage":
        points.append(("Financing", "Needs a mortgage, not yet approved", 10))
    else:
        points.append(("Financing", "Financing unknown", 0))

    if f["budget"]:
        points.append(("Budget", f"Budget stated ({format_money(f['budget'], f['currency'])})", 15))
    if f["location"]:
        points.append(("Location", f"Location stated ({f['location']})", 10))

    if f["intent"] in ("buy", "sell", "invest"):
        points.append(("Intent", f"Clear intent to {f['intent']}", 10))
    elif f["intent"] == "rent":
        points.append(("Intent", "Renting (lower commission)", 3))

    if f["motivation"]:
        points.append(("Motivation", f"Motivation: {f['motivation']}", 10))

    for flag in f["red_flags"]:
        points.append(("Red flag", f"Red flag: {flag}", -10))

    raw_total = sum(p for _, _, p in points)
    score = max(0, min(100, raw_total))
    return score, tier_for(score), points


# ---------- Confidence and interest (shown next to the score, never part of it) ----------

KEY_FACTS = ("intent", "timeline", "budget", "location", "financing")


def facts_stated(f):
    return {
        "intent": f["intent"] != "unknown",
        "timeline": f["timeline_months"] is not None,
        "budget": bool(f["budget"]),
        "location": bool(f["location"]),
        "financing": f["financing"] != "unknown",
    }


def confidence(f, engine):
    """How much of the score rests on facts the customer actually stated, as a percentage.
    Five key facts; rules mode (no AI) reads less reliably, so it counts for a little less."""
    stated = sum(facts_stated(f).values())
    pct = round(stated / len(KEY_FACTS) * 100)
    if engine != "ai":
        pct = round(pct * 0.85)
    return max(5, pct)


def interest_level(f, tier):
    tl = f["timeline_months"]
    if f["red_flags"] or tier == "Cold":
        return "Low"
    if tier == "Hot" or (tl is not None and tl <= 3 and f["sentiment"] == "positive"):
        return "High"
    return "Medium"


def describe_want(f):
    """"a 3 bedroom apartment" / "a villa" / "a home" """
    ptype = (f.get("property_type") or "").lower()
    beds = f.get("bedrooms")
    if beds and ptype:
        return f"a {beds} bedroom {ptype}"
    if beds:
        return f"a {beds} bedroom home"
    if ptype:
        return f"an {ptype}" if ptype[0] in "aeiou" else f"a {ptype}"
    return None


# ---------- Next step for the agent ----------

def missing_question(f):
    """The single most useful thing to ask next, given what is missing."""
    if f["intent"] == "unknown":
        return "intent", "Are you looking to buy, sell, rent or invest?"
    if f["timeline_months"] is None:
        verb = "sell" if f["intent"] == "sell" else "move"
        return "timeline", f"When are you hoping to {verb}?"
    if f["financing"] == "unknown" and f["intent"] in ("buy", "invest"):
        return "financing", "Will you be paying cash, or are you pre-approved for a loan?"
    if not f["budget"] and f["intent"] != "sell":
        return "budget", "What budget range are you working with?"
    if not f["location"]:
        return "location", "Which areas or neighbourhoods are you considering?"
    if f["financing"] == "mortgage":
        return "financing", "Have you spoken to a lender about pre-approval yet?"
    if f["intent"] == "sell":
        return None, "Would you like me to prepare a free market valuation for your home?"
    if f["intent"] == "rent":
        return None, "When would you like to see a few places?"
    return None, None


def next_action(f, tier):
    if tier == "Hot":
        if f["intent"] == "sell":
            return "Call this seller today and offer a free valuation visit."
        return "Contact this lead today and arrange a property consultation or viewing."
    if tier == "Warm":
        key, _ = missing_question(f)
        topic = {"intent": "what they want to do", "timeline": "their timeline",
                 "financing": "the financing situation", "budget": "their budget",
                 "location": "the preferred location"}.get(key)
        if topic:
            return f"Clarify {topic} before scheduling a visit."
        if f["intent"] == "sell":
            return "Offer a free valuation and agree a listing timeline."
        return "Follow up within 48 hours and offer two or three matching options."
    if f["intent"] == "sell":
        return "Share recent sales nearby and check back in a few weeks, no pressure."
    return "Send relevant listings and keep a low-pressure follow-up every few weeks."


# ---------- Follow-up draft ----------

def template_followup(name, f, tier, matches=0):
    """matches: how many of the agent's own listings fit this lead (never claim listings otherwise)."""
    name = (f.get("customer_name") or name or "").strip() or "there"
    place = f" in {f['location']}" if f["location"] else ""
    want = describe_want(f)
    budget = format_money(f["budget"], f["currency"])
    if matches:
        found = (f"I have {matches} listing{'s' if matches > 1 else ''}{place} that match"
                 f"{'' if matches > 1 else 'es'} what you're looking for")
    else:
        found = f"I'd love to help you find {want or 'the right place'}{place}"
        if budget and f["intent"] != "sell":
            found += f" around {budget}"
    if tier == "Hot":
        if f["intent"] == "sell":
            return (f"Hi {name}, thanks for reaching out! I'd love to help you sell{place}. "
                    f"Are you free for a quick call today or tomorrow to talk about pricing?")
        return f"Hi {name}, thanks for reaching out! {found}. Are you free for a quick call today or tomorrow?"
    if tier == "Warm":
        _, question = missing_question(f)
        question = question or "Would a quick call this week work to go over options?"
        if f["intent"] == "sell":
            return f"Hi {name}, great to hear from you! To help you get the best result{place}: {question}"
        return f"Hi {name}, great to hear from you! {found}. {question}"
    if f["intent"] == "sell":
        return (f"Hi {name}, thanks for reaching out! Happy to share what homes{place} are "
                f"selling for right now, no pressure. Reply anytime if you'd like a valuation.")
    return (f"Hi {name}, thanks for your interest! I'll send you a few options{place} "
            f"to browse. Reply anytime if you'd like to talk.")


def llm_followup(name, message, f, tier, client, model, matches=None):
    _, question = missing_question(f)
    name = f.get("customer_name") or name
    listing_note = (
        f"The agent has these matching listings you may mention by title and price: "
        f"{json.dumps(matches, ensure_ascii=False)}. " if matches else
        "Never claim you already have specific listings or properties; offer to share or look for "
        "options instead. ")
    facts = {k: v for k, v in f.items() if k not in ("phone", "email")}
    prompt = (
        f"Write a short, friendly SMS (max 3 sentences, under 320 characters) from a real "
        f"estate agent to a {tier} lead named {name or 'the lead'}. Their message: "
        f"\"{message}\". Known facts: {json.dumps(facts, ensure_ascii=False)}. "
        f"Hot leads: suggest a call today. Warm: ask this one missing question: "
        f"\"{question or 'their availability for a call'}\". Cold: offer listings, no pressure. "
        f"{listing_note}Mention the property type and size they want if known. "
        f"Do not invent prices or facts. Use the same currency as the lead. "
        f"Return only the SMS text, no quotes, no sign-off placeholder."
    )
    resp = client.chat.completions.create(
        model=model, temperature=0.4, max_tokens=300, **_model_kwargs(model),
        messages=[{"role": "user", "content": prompt}],
    )
    text = (resp.choices[0].message.content or "").strip().strip('"').strip()
    text = text.translate(str.maketrans({"\u2011": "-", "\u2010": "-", "\u2013": "-", "\u2014": ", ",
                                         "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'}))
    if not text:
        raise ValueError("Empty SMS from model")
    return text[:480]
