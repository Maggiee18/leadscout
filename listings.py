"""The agent's own property listings, and matching them to leads.

Matching is plain rules, like scoring: a listing is suggested only when it fits what the lead
actually said (deal type, location, budget, property type, bedrooms), and every suggestion
says why it matched. Nothing is invented: if the agent has no listings, nothing is suggested.
"""

import io
import time
import uuid

import pandas as pd
from fastapi import HTTPException

from scorer import PROPERTY_TYPES, format_money

DEALS = ("sale", "rent")
LISTING_STATUSES = ("available", "under offer", "sold")
MAX_LISTINGS = 500


def _norm_type(value):
    v = str(value or "").strip().lower()
    v = {"flat": "apartment", "condo": "apartment", "bungalow": "house", "land": "plot",
         "office": "commercial", "shop": "commercial"}.get(v, v)
    return next((t for t in PROPERTY_TYPES if t.lower() == v), None)


def clean_listing(data):
    """Validate one listing (from the form or a CSV row). Raises ValueError with a friendly message."""
    title = str(data.get("title") or "").strip()[:120]
    if len(title) < 3:
        raise ValueError("Give the listing a title of at least 3 characters.")
    location = str(data.get("location") or "").strip()[:80]
    if not location:
        raise ValueError("Add the listing's location.")
    deal = str(data.get("deal") or "sale").strip().lower()
    deal = {"buy": "sale", "sell": "sale", "for sale": "sale", "lease": "rent", "rental": "rent"}.get(deal, deal)
    if deal not in DEALS:
        raise ValueError("Deal must be 'sale' or 'rent'.")
    try:
        price = float(str(data.get("price") or "").replace(",", "").strip())
    except ValueError:
        raise ValueError("Price must be a number, like 8000000 or 450000.") from None
    if not 0 < price < 1e12:
        raise ValueError("Price must be more than zero.")
    currency = str(data.get("currency") or "INR").strip().upper()
    if currency not in ("INR", "USD"):
        raise ValueError("Currency must be INR or USD.")
    beds = data.get("bedrooms")
    try:
        beds = int(float(beds)) if beds not in (None, "") else None
    except ValueError:
        raise ValueError("Bedrooms must be a whole number.") from None
    if beds is not None and not 0 <= beds <= 20:
        raise ValueError("Bedrooms must be between 0 and 20.")
    status = str(data.get("status") or "available").strip().lower()
    if status not in LISTING_STATUSES:
        raise ValueError("Status must be available, under offer or sold.")
    url = str(data.get("url") or "").strip()[:300]
    if url and not url.lower().startswith(("https://", "http://")):
        raise ValueError("The link must start with https://")
    return {"title": title, "location": location, "deal": deal, "price": price, "currency": currency,
            "property_type": _norm_type(data.get("property_type")), "bedrooms": beds, "status": status,
            "url": url or None}


def _public(row):
    d = dict(row)
    d["price_label"] = format_money(d["price"], d["currency"]) + (" a month" if d["deal"] == "rent" else "")
    return d


def _places(text):
    return {w for w in str(text or "").lower().replace(",", " ").split() if len(w) > 2}


def match_listings(fields, listings, limit=3):
    """Listings that fit the lead, best first, each with the reasons it matched."""
    intent = fields.get("intent")
    if intent not in ("buy", "invest", "rent") or not listings:
        return []
    want_deal = "rent" if intent == "rent" else "sale"
    budget, currency = fields.get("budget"), fields.get("currency")
    lead_places = _places(fields.get("location"))
    ptype, beds = fields.get("property_type"), fields.get("bedrooms")
    out = []
    for item in listings:
        if item.get("status") != "available" or item["deal"] != want_deal:
            continue
        reasons, points = [], 0
        if lead_places:
            if not lead_places & _places(item["location"]):
                continue
            reasons.append(f"In {item['location']}")
            points += 3
        if budget:
            if item["currency"] != currency or item["price"] > budget * 1.1:
                continue
            reasons.append("Within budget" if item["price"] <= budget else "Just above budget (within 10%)")
            points += 3 if item["price"] <= budget else 1
        if ptype:
            if item.get("property_type") and item["property_type"] != ptype:
                continue
            if item.get("property_type") == ptype:
                reasons.append(ptype)
                points += 2
        if beds and item.get("bedrooms") is not None:
            if item["bedrooms"] < beds:
                continue
            reasons.append(f"{item['bedrooms']} bedrooms")
            points += 2 if item["bedrooms"] == beds else 1
        if not reasons:
            continue
        out.append((points, -item["price"], {**_public(item), "reasons": reasons}))
    out.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [m for _, _, m in out[:limit]]


class ListingStore:
    COLS = ("id", "title", "location", "deal", "price", "currency", "property_type", "bedrooms",
            "status", "url", "created_at")

    def __init__(self, db):
        self.db = db

    def all(self, user_id, available_only=False):
        sql = f"SELECT {', '.join(self.COLS)} FROM listings WHERE user_id = ?"
        if available_only:
            sql += " AND status = 'available'"
        rows = self.db.fetchall(sql + " ORDER BY created_at DESC LIMIT 1000", (user_id,))
        return [_public(dict(zip(self.COLS, r, strict=True))) for r in rows]

    def count(self, user_id):
        return self.db.fetchone("SELECT COUNT(*) FROM listings WHERE user_id = ?", (user_id,))[0]

    def add_many(self, user_id, items):
        if self.count(user_id) + len(items) > MAX_LISTINGS:
            raise HTTPException(400, f"You can keep up to {MAX_LISTINGS} listings.")
        now = int(time.time())
        ids = []
        with self.db.transaction() as run:
            for it in items:
                lid = uuid.uuid4().hex
                ids.append(lid)
                run("INSERT INTO listings (id, user_id, title, location, deal, price, currency, property_type, "
                    "bedrooms, status, url, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (lid, user_id, it["title"], it["location"], it["deal"], it["price"], it["currency"],
                     it["property_type"], it["bedrooms"], it["status"], it["url"], now))
        return ids

    def get(self, user_id, listing_id):
        row = self.db.fetchone(f"SELECT {', '.join(self.COLS)} FROM listings WHERE user_id = ? AND id = ?",
                               (user_id, listing_id))
        return _public(dict(zip(self.COLS, row, strict=True))) if row else None

    def update(self, user_id, listing_id, item):
        return self.db.execute(
            "UPDATE listings SET title = ?, location = ?, deal = ?, price = ?, currency = ?, property_type = ?, "
            "bedrooms = ?, status = ?, url = ? WHERE user_id = ? AND id = ?",
            (item["title"], item["location"], item["deal"], item["price"], item["currency"], item["property_type"],
             item["bedrooms"], item["status"], item["url"], user_id, listing_id))

    def delete(self, user_id, listing_id):
        return self.db.execute("DELETE FROM listings WHERE user_id = ? AND id = ?", (user_id, listing_id))


def read_listings_csv(text):
    """Parse a listings CSV (title, location, deal, price, currency, property_type, bedrooms, status, url)."""
    try:
        df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False, sep=",", engine="python",
                         skipinitialspace=True, on_bad_lines="skip")
    except Exception:
        raise HTTPException(400, "The listings file couldn't be read. Check commas and quotes.") from None
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    aliases = {"name": "title", "property": "title", "city": "location", "area": "location", "type": "property_type",
               "beds": "bedrooms", "bhk": "bedrooms", "for": "deal", "link": "url"}
    df = df.rename(columns={k: v for k, v in aliases.items() if k in df.columns and v not in df.columns})
    missing = [c for c in ("title", "location", "price") if c not in df.columns]
    if missing:
        raise HTTPException(400, f"The listings file needs these columns: {', '.join(missing)}.")
    items, errors = [], []
    for i, r in df.iterrows():
        try:
            items.append(clean_listing(r.to_dict()))
        except ValueError as e:
            errors.append(f"Row {i + 2}: {e}")
    if not items:
        raise HTTPException(400, "No valid listings found. " + (errors[0] if errors else ""))
    return items, errors
