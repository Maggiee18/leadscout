"""Saved leads: every analysis is stored against the user who ran it, then managed as a pipeline.

Every query is scoped by user_id, so one user can never read, change or delete another's leads.
"""

import hashlib
import json
import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from analysis import display_fields
from db import Database
from scorer import (
    PROPERTY_TYPES,
    clean_fields,
    confidence,
    interest_level,
    next_action,
    score_lead,
)

TIERS = ("Hot", "Warm", "Cold")
STATUSES = ("New", "Contacted", "Follow-up", "Negotiating", "Converted", "Lost")
OPEN_STATUSES = ("New", "Contacted", "Follow-up", "Negotiating")
CONTACT_STATUSES = ("Contacted", "Follow-up", "Negotiating", "Converted")  # moving here means you spoke to them
PRIORITIES = ("High", "Medium", "Low")
TIER_PRIORITY = {"Hot": "High", "Warm": "Medium", "Cold": "Low"}
# First follow up suggested when a lead is saved: Hot today, Warm in three days, Cold not scheduled.
TIER_FOLLOW_UP = {"Hot": 0, "Warm": 3 * 86400}
DAY = 86400
SORTS = {
    "newest": "created_at DESC",
    "oldest": "created_at ASC",
    "score-desc": "score DESC, created_at DESC",
    "score-asc": "score ASC, created_at DESC",
    "updated": "COALESCE(updated_at, created_at) DESC",
    "followup": "CASE WHEN follow_up_at IS NULL THEN 1 ELSE 0 END, follow_up_at ASC, score DESC",
    "priority": "CASE priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END, score DESC, created_at DESC",
}
FOLLOW_UP_FILTERS = ("due", "overdue", "today", "upcoming", "scheduled", "none")
# Fields an agent may correct. Changing any of the scoring facts re-scores the lead with the same rules.
EDITABLE = ("name", "phone", "email", "location", "budget", "currency", "property_type", "bedrooms", "intent",
            "timeline_months", "financing", "notes", "status", "follow_up_at", "priority", "contacted")
SCORING_FACTS = ("location", "budget", "currency", "intent", "timeline_months", "financing")


def _escape_like(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def day_bounds(now, tz_minutes=0):
    """Start and end (UTC seconds) of the viewer's local day. tz_minutes is JavaScript's getTimezoneOffset()."""
    shift = int(tz_minutes) * 60
    start = (now - shift) // DAY * DAY + shift
    return start, start + DAY


def _digits(text):
    return re.sub(r"\D", "", text or "")


def dedupe_keys(r):
    """Ways two inquiries can be the same customer: phone (last 10 digits), email, or the same message."""
    keys = []
    phone = _digits(r.get("phone"))
    if len(phone) >= 10:
        keys.append("p:" + phone[-10:])
    if r.get("email"):
        keys.append("e:" + r["email"].strip().lower())
    msg = re.sub(r"\s+", " ", (r.get("message") or "").strip().lower())
    if msg:
        keys.append("m:" + hashlib.sha256(msg.encode()).hexdigest()[:20])
    return keys


class LeadStore:
    COLS = ("result, created_at, source, status, notes, duplicate_of, updated_at, status_changed_at, "
            "follow_up_at, priority, last_contacted_at")
    EXTRA = ("saved_at", "source", "status", "notes", "duplicate_of", "updated_at", "status_changed_at",
             "follow_up_at", "priority", "last_contacted_at")

    def __init__(self, db: Database):
        self.db = db

    # ---------- write ----------

    def _key_index(self, run, user_id, before=None, exclude_id=None):
        """key -> id of the earliest saved lead with that phone, email or message."""
        sql = "SELECT id, dedupe_key FROM leads WHERE user_id = ? AND dedupe_key IS NOT NULL"
        params = [user_id]
        if before is not None:
            sql += " AND created_at <= ?"
            params.append(before)
        index = {}
        for lead_id, joined in run(sql + " ORDER BY created_at ASC, id ASC", params).fetchall():
            if lead_id == exclude_id:
                continue
            for k in joined.split("|"):
                index.setdefault(k, lead_id)
        return index

    def save_many(self, user_id, results, source):
        """Save results; mark each one that repeats an earlier inquiry (same phone, email or message)."""
        now = int(time.time())
        with self.db.transaction() as run:
            seen = self._key_index(run, user_id)
            for r in results:
                keys = dedupe_keys(r)
                dup = next((seen[k] for k in keys if k in seen), None)
                for k in keys:
                    seen.setdefault(k, r["id"])
                r["duplicate_of"] = dup
                r["status"] = "New"
                r["priority"] = TIER_PRIORITY.get(r["tier"], "Low")
                gap = TIER_FOLLOW_UP.get(r["tier"])
                r["follow_up_at"] = now + gap if gap is not None else None
                f = r["fields"]
                stored = {k: v for k, v in r.items() if k not in self.EXTRA}
                run("INSERT INTO leads (id, user_id, name, message, score, tier, intent, location, engine, source, "
                    "result, created_at, status, phone, email, budget, currency, property_type, dedupe_key, "
                    "duplicate_of, updated_at, status_changed_at, priority, follow_up_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (r["id"], user_id, r.get("name"), r["message"], r["score"], r["tier"],
                     r["display"].get("intent"), r["display"].get("location"), r["engine"], source,
                     json.dumps(stored, ensure_ascii=False), now, "New", r.get("phone"), r.get("email"),
                     f.get("budget"), f.get("currency"), f.get("property_type"), "|".join(keys) or None,
                     dup, now, now, r["priority"], r["follow_up_at"]))
        return results

    def update(self, user_id, lead_id, changes, listings=None):
        """Apply an agent's edits. Returns the updated lead, or None if it isn't theirs."""
        current = self.get(user_id, lead_id)
        if not current:
            return None
        changes = {k: v for k, v in changes.items() if k in EDITABLE}
        r = dict(current)
        f = dict(r["fields"])
        rescored = False
        for key in SCORING_FACTS + ("property_type", "bedrooms"):
            if key in changes and changes[key] != f.get(key):
                f[key] = changes[key]
                rescored = rescored or key in SCORING_FACTS
        f = clean_fields(f)
        if "name" in changes:
            r["name"] = (changes["name"] or "").strip()[:80] or None
        for key in ("phone", "email"):
            if key in changes:
                r[key] = f[key] = (changes[key] or "").strip()[:120] or None
        r["fields"] = f
        r["display"] = display_fields(f)
        if rescored:
            score, tier, points = score_lead(f)
            r.update(score=score, tier=tier, raw_total=sum(p for _, _, p in points),
                     breakdown=[{"factor": fa, "reason": rs, "points": p} for fa, rs, p in points],
                     next_action=next_action(f, tier))
            r["rescored_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        r["confidence"] = confidence(f, r["engine"])
        r["interest"] = interest_level(f, r["tier"])
        if listings is not None:
            from listings import match_listings
            r["matches"] = match_listings(f, listings)
        status = changes.get("status") or r.get("status", "New")
        notes = changes["notes"] if "notes" in changes else r.get("notes")
        now = int(time.time())
        follow_up = changes["follow_up_at"] if "follow_up_at" in changes else r.get("follow_up_at")
        priority = changes.get("priority") or r.get("priority") or TIER_PRIORITY.get(r["tier"], "Low")
        contacted = r.get("last_contacted_at")
        if changes.get("contacted") or (status != r.get("status") and status in CONTACT_STATUSES):
            contacted = now
        keys = dedupe_keys(r)
        for k in self.EXTRA:
            r.pop(k, None)
        with self.db.transaction() as run:
            index = self._key_index(run, user_id, before=current["saved_at"], exclude_id=lead_id)
            dup = next((index[k] for k in keys if k in index), None)
            run("UPDATE leads SET name = ?, score = ?, tier = ?, intent = ?, location = ?, result = ?, phone = ?, "
                "email = ?, budget = ?, currency = ?, property_type = ?, dedupe_key = ?, duplicate_of = ?, notes = ?, "
                "updated_at = ?, status_changed_at = CASE WHEN status != ? THEN ? ELSE status_changed_at END, "
                "status = ?, follow_up_at = ?, priority = ?, last_contacted_at = ? WHERE user_id = ? AND id = ?",
                (r.get("name"), r["score"], r["tier"], r["display"].get("intent"), r["display"].get("location"),
                 json.dumps(r, ensure_ascii=False), r.get("phone"), r.get("email"), f.get("budget"),
                 f.get("currency"), f.get("property_type"), "|".join(keys) or None, dup,
                 (notes or "")[:1000] or None, now, status, now, status, follow_up, priority, contacted,
                 user_id, lead_id))
        return self.get(user_id, lead_id)

    def delete(self, user_id, lead_id):
        n = self.db.execute("DELETE FROM leads WHERE user_id = ? AND id = ?", (user_id, lead_id))
        if n:  # leads that pointed at it as their original are no longer duplicates of anything
            self.db.execute("UPDATE leads SET duplicate_of = NULL WHERE user_id = ? AND duplicate_of = ?",
                            (user_id, lead_id))
        return n

    def clear(self, user_id):
        return self.db.execute("DELETE FROM leads WHERE user_id = ?", (user_id,))

    # ---------- read ----------

    def _row(self, row):
        result, created_at, source, status, notes, dup, updated_at, status_at, follow_up, priority, contacted = row
        r = json.loads(result)
        r.update(saved_at=created_at, source=source, status=status or "New", notes=notes, duplicate_of=dup,
                 updated_at=updated_at, status_changed_at=status_at, follow_up_at=follow_up,
                 priority=priority or TIER_PRIORITY.get(r.get("tier"), "Low"), last_contacted_at=contacted)
        return r

    def _where(self, user_id, q="", tier="all", status="all", location="", budget_min=None, budget_max=None,
               currency="", duplicates=False, property_type="", priority="", followup="", tz=0, now=None):
        sql, params = ["user_id = ?"], [user_id]
        if priority in PRIORITIES:
            sql.append("priority = ?")
            params.append(priority)
        if followup in FOLLOW_UP_FILTERS:
            start, end = day_bounds(now or int(time.time()), tz)
            open_only = "status IN ('" + "', '".join(OPEN_STATUSES) + "')"
            cond = {"due": ("follow_up_at < ?", [end]), "overdue": ("follow_up_at < ?", [start]),
                    "today": ("follow_up_at >= ? AND follow_up_at < ?", [start, end]),
                    "upcoming": ("follow_up_at >= ?", [end]), "scheduled": ("follow_up_at IS NOT NULL", []),
                    "none": ("follow_up_at IS NULL", [])}[followup]
            sql.append(f"{cond[0]} AND {open_only}")
            params += cond[1]
        if tier in TIERS:
            sql.append("tier = ?")
            params.append(tier)
        if status in STATUSES:
            sql.append("status = ?")
            params.append(status)
        if property_type in PROPERTY_TYPES:
            sql.append("property_type = ?")
            params.append(property_type)
        q = (q or "").strip().lower()[:100]
        if q:
            like = f"%{_escape_like(q)}%"
            sql.append("(LOWER(COALESCE(name, '')) LIKE ? ESCAPE '\\' OR LOWER(message) LIKE ? ESCAPE '\\' "
                       "OR LOWER(COALESCE(location, '')) LIKE ? ESCAPE '\\' OR COALESCE(phone, '') LIKE ? ESCAPE '\\' "
                       "OR LOWER(COALESCE(email, '')) LIKE ? ESCAPE '\\')")
            params += [like] * 5
        location = (location or "").strip().lower()[:80]
        if location:
            sql.append("LOWER(COALESCE(location, '')) LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(location)}%")
        if currency in ("INR", "USD"):
            sql.append("currency = ?")
            params.append(currency)
        if budget_min is not None:
            sql.append("budget >= ?")
            params.append(float(budget_min))
        if budget_max is not None:
            sql.append("budget <= ?")
            params.append(float(budget_max))
        if duplicates:
            sql.append("duplicate_of IS NOT NULL")
        return " AND ".join(sql), params

    def list(self, user_id, sort="newest", limit=20, offset=0, **filters):
        where, params = self._where(user_id, **filters)
        order = SORTS.get(sort, SORTS["newest"])
        limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
        total = self.db.fetchone(f"SELECT COUNT(*) FROM leads WHERE {where}", params)[0]
        rows = self.db.fetchall(f"SELECT {self.COLS} FROM leads WHERE {where} "
                                f"ORDER BY {order} LIMIT ? OFFSET ?", params + [limit, offset])
        return {"items": [self._row(r) for r in rows], "total": total, "limit": limit, "offset": offset}

    def get(self, user_id, lead_id):
        row = self.db.fetchone(f"SELECT {self.COLS} FROM leads WHERE user_id = ? AND id = ?", (user_id, lead_id))
        return self._row(row) if row else None

    def locations(self, user_id):
        rows = self.db.fetchall("SELECT location, COUNT(*) FROM leads WHERE user_id = ? AND location IS NOT NULL "
                                "GROUP BY location ORDER BY COUNT(*) DESC LIMIT 30", (user_id,))
        return [{"location": loc, "count": n} for loc, n in rows]

    def _series(self, user_id, keys, key_of, label_of, first_ts):
        """Leads, Hot leads and conversions per period. Conversions count in the period they happened."""
        series = OrderedDict((k, {"leads": 0, "converted": 0, "hot": 0}) for k in keys)
        for created_at, status, tier, status_at in self.db.fetchall(
                "SELECT created_at, status, tier, status_changed_at FROM leads WHERE user_id = ? AND "
                "(created_at >= ? OR (status = 'Converted' AND status_changed_at >= ?))",
                (user_id, first_ts, first_ts)):
            key = key_of(created_at)
            if key in series:
                series[key]["leads"] += 1
                series[key]["hot"] += tier == "Hot"
            if status == "Converted":
                ckey = key_of(status_at or created_at)
                if ckey in series:
                    series[ckey]["converted"] += 1
        out = []
        for key, v in series.items():
            rate = round(100 * v["converted"] / v["leads"], 1) if v["leads"] else None
            out.append({"period": key, "label": label_of(key), **v, "conversion_rate": rate})
        return out

    def monthly(self, user_id, months=6, now=None):
        now = now or datetime.now(timezone.utc)
        keys, y, m = [], now.year, now.month
        for _ in range(months):
            keys.append(f"{y:04d}-{m:02d}")
            y, m = (y, m - 1) if m > 1 else (y - 1, 12)
        keys.reverse()
        first = datetime(int(keys[0][:4]), int(keys[0][5:]), 1, tzinfo=timezone.utc)
        out = self._series(user_id, keys, lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m"),
                           lambda k: datetime(int(k[:4]), int(k[5:]), 1).strftime("%b %Y"), int(first.timestamp()))
        for row in out:
            row["month"] = row["period"]
        return out

    def weekly(self, user_id, weeks=8, now=None):
        """Weeks start on Monday (UTC). Labelled by the Monday they start on."""
        now = now or datetime.now(timezone.utc)
        monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        starts = [monday - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
        keys = [d.strftime("%Y-%m-%d") for d in starts]

        def key_of(ts):
            d = datetime.fromtimestamp(ts, timezone.utc)
            return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")

        def label_of(k):
            d = datetime.strptime(k, "%Y-%m-%d")
            return f"{d.strftime('%b')} {d.day}"

        return self._series(user_id, keys, key_of, label_of, int(starts[0].timestamp()))

    def follow_ups(self, user_id, tz=0, now=None, limit=20):
        """Open leads with a follow up: overdue, due today, and the next seven days."""
        now = now or int(time.time())
        start, end = day_bounds(now, tz)
        open_only = "status IN ('" + "', '".join(OPEN_STATUSES) + "')"
        out = {}
        for name, cond, params in (("overdue", "follow_up_at < ?", [start]),
                                   ("today", "follow_up_at >= ? AND follow_up_at < ?", [start, end]),
                                   ("upcoming", "follow_up_at >= ? AND follow_up_at < ?", [end, end + 7 * DAY])):
            where = f"user_id = ? AND {open_only} AND {cond}"
            total = self.db.fetchone(f"SELECT COUNT(*) FROM leads WHERE {where}", [user_id] + params)[0]
            rows = self.db.fetchall(f"SELECT {self.COLS} FROM leads WHERE {where} ORDER BY follow_up_at ASC, "
                                    "score DESC LIMIT ?", [user_id] + params + [limit])
            out[name] = {"total": total, "items": [self._row(r) for r in rows]}
        out["due"] = out["overdue"]["total"] + out["today"]["total"]
        return out

    def stats(self, user_id, months=6, now=None):
        """Dashboard numbers: totals, tiers, pipeline, and leads and conversions per week and month."""
        row = self.db.fetchone(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN tier = 'Hot' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN tier = 'Warm' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN tier = 'Cold' THEN 1 ELSE 0 END), "
            "AVG(score), SUM(CASE WHEN duplicate_of IS NOT NULL THEN 1 ELSE 0 END) FROM leads WHERE user_id = ?",
            (user_id,))
        total = row[0] or 0
        by_status = dict.fromkeys(STATUSES, 0)
        for status, n in self.db.fetchall("SELECT status, COUNT(*) FROM leads WHERE user_id = ? GROUP BY status",
                                          (user_id,)):
            by_status[status if status in by_status else "New"] += n
        closed = by_status["Converted"] + by_status["Lost"]
        return {
            "total": total, "hot": int(row[1] or 0), "warm": int(row[2] or 0), "cold": int(row[3] or 0),
            "average_score": round(float(row[4]), 1) if total else None,
            "duplicates": int(row[5] or 0),
            "by_status": by_status,
            "conversion_rate": round(100 * by_status["Converted"] / total, 1) if total else None,
            "win_rate": round(100 * by_status["Converted"] / closed, 1) if closed else None,
            "monthly": self.monthly(user_id, months, now),
            "weekly": self.weekly(user_id, 8, now),
            "recent": self.list(user_id, sort="newest", limit=6)["items"],
        }

    def backfill(self):
        """Leads saved before version 4 get their budget, contact and type columns filled from the stored result."""
        rows = self.db.fetchall("SELECT id, result FROM leads WHERE updated_at IS NULL LIMIT 5000")
        if not rows:
            return 0
        with self.db.transaction() as run:
            for lead_id, result in rows:
                r = json.loads(result)
                f = r.get("fields") or {}
                keys = dedupe_keys(r)
                run("UPDATE leads SET budget = ?, currency = ?, property_type = ?, phone = ?, email = ?, "
                    "dedupe_key = ?, updated_at = created_at, status_changed_at = created_at WHERE id = ?",
                    (f.get("budget"), f.get("currency"), f.get("property_type"), r.get("phone"), r.get("email"),
                     "|".join(keys) or None, lead_id))
        return len(rows)
