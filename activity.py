"""Activity log: who did what, and when. Details never include lead messages or customer contact data."""

import logging
import time
import uuid

log = logging.getLogger("leadscout.activity")

ACTIONS = {
    "register": "Account created", "login": "Signed in", "login_failed": "Wrong password",
    "account_locked": "Account paused after wrong passwords", "logout": "Signed out",
    "password_changed": "Password changed", "password_reset": "Password reset",
    "lead_scored": "Lead analysed", "batch_uploaded": "File uploaded", "lead_updated": "Lead edited",
    "lead_status": "Lead status changed", "lead_deleted": "Lead deleted", "leads_cleared": "All leads deleted",
    "leads_exported": "Leads exported", "listing_added": "Listing added", "listing_updated": "Listing edited",
    "listing_deleted": "Listing deleted", "listings_imported": "Listings imported",
}


class ActivityLog:
    def __init__(self, db):
        self.db = db

    def add(self, user_id, action, detail=None, ip=None):
        try:
            self.db.execute("INSERT INTO activity_log (id, user_id, action, detail, ip, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (uuid.uuid4().hex, user_id, action, (detail or "")[:300] or None,
                             (ip or "")[:64] or None, time.time_ns() // 1_000_000))  # milliseconds, so order is exact
        except Exception as exc:  # logging must never break the request
            log.warning("activity log write failed: %s", type(exc).__name__)

    def _rows(self, rows, with_user=False):
        out = []
        for r in rows:
            item = {"action": r[0], "label": ACTIONS.get(r[0], r[0]), "detail": r[1], "ip": r[2], "at": r[3] / 1000}
            if with_user:
                item["user"] = r[4]
            out.append(item)
        return out

    def for_user(self, user_id, limit=100):
        rows = self.db.fetchall("SELECT action, detail, ip, created_at FROM activity_log WHERE user_id = ? "
                                "ORDER BY created_at DESC LIMIT ?", (user_id, max(1, min(int(limit), 500))))
        return self._rows(rows)

    def all(self, limit=200):
        rows = self.db.fetchall("SELECT a.action, a.detail, a.ip, a.created_at, u.email FROM activity_log a "
                                "LEFT JOIN users u ON u.id = a.user_id ORDER BY a.created_at DESC LIMIT ?",
                                (max(1, min(int(limit), 1000)),))
        return self._rows(rows, with_user=True)

    def prune(self, days=180):
        self.db.execute("DELETE FROM activity_log WHERE created_at < ?", ((int(time.time()) - days * 86400) * 1000,))
