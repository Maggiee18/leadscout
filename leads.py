"""Saved lead history: every analysis is stored against the user who ran it.

Every query is scoped by user_id, so one user can never read or delete another's leads.
"""

import json
import time

from db import Database

TIERS = ("Hot", "Warm", "Cold")
SORTS = {
    "newest": "created_at DESC",
    "oldest": "created_at ASC",
    "score-desc": "score DESC, created_at DESC",
    "score-asc": "score ASC, created_at DESC",
}


def _escape_like(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class LeadStore:
    def __init__(self, db: Database):
        self.db = db

    def save_many(self, user_id, results, source):
        now = int(time.time())
        with self.db.transaction() as run:
            for r in results:
                run("INSERT INTO leads (id, user_id, name, message, score, tier, intent, location, "
                    "engine, source, result, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (r["id"], user_id, r.get("name"), r["message"], r["score"], r["tier"],
                     r["display"].get("intent"), r["display"].get("location"), r["engine"], source,
                     json.dumps(r, ensure_ascii=False), now))

    def _where(self, user_id, q="", tier="all"):
        sql, params = ["user_id = ?"], [user_id]
        if tier in TIERS:
            sql.append("tier = ?")
            params.append(tier)
        q = (q or "").strip().lower()[:100]
        if q:
            like = f"%{_escape_like(q)}%"
            sql.append("(LOWER(COALESCE(name, '')) LIKE ? ESCAPE '\\' OR LOWER(message) LIKE ? ESCAPE '\\' "
                       "OR LOWER(COALESCE(location, '')) LIKE ? ESCAPE '\\')")
            params += [like, like, like]
        return " AND ".join(sql), params

    def list(self, user_id, q="", tier="all", sort="newest", limit=20, offset=0):
        where, params = self._where(user_id, q, tier)
        order = SORTS.get(sort, SORTS["newest"])
        limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
        total = self.db.fetchone(f"SELECT COUNT(*) FROM leads WHERE {where}", params)[0]
        rows = self.db.fetchall(f"SELECT result, created_at, source FROM leads WHERE {where} "
                                f"ORDER BY {order} LIMIT ? OFFSET ?", params + [limit, offset])
        items = []
        for result, created_at, source in rows:
            r = json.loads(result)
            r["saved_at"], r["source"] = created_at, source
            items.append(r)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def get(self, user_id, lead_id):
        row = self.db.fetchone("SELECT result, created_at, source FROM leads WHERE user_id = ? AND id = ?",
                               (user_id, lead_id))
        if not row:
            return None
        r = json.loads(row[0])
        r["saved_at"], r["source"] = row[1], row[2]
        return r

    def delete(self, user_id, lead_id):
        return self.db.execute("DELETE FROM leads WHERE user_id = ? AND id = ?", (user_id, lead_id))

    def clear(self, user_id):
        return self.db.execute("DELETE FROM leads WHERE user_id = ?", (user_id,))

    def stats(self, user_id):
        row = self.db.fetchone(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN tier = 'Hot' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN tier = 'Warm' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN tier = 'Cold' THEN 1 ELSE 0 END), "
            "AVG(score) FROM leads WHERE user_id = ?", (user_id,))
        total = row[0] or 0
        recent = self.list(user_id, sort="newest", limit=6)["items"]
        return {"total": total, "hot": int(row[1] or 0), "warm": int(row[2] or 0),
                "cold": int(row[3] or 0),
                "average_score": round(float(row[4]), 1) if total else None,
                "recent": recent}
