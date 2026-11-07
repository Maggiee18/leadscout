"""
Database access for LeadScout: SQLite by default, PostgreSQL when DATABASE_URL is set.

SQL is written once with `?` placeholders and converted for Postgres. Schema changes
are applied as numbered migrations recorded in `schema_version`, so upgrading an
existing database is safe and repeatable.
"""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _big(pg):
    return "BIGINT" if pg else "INTEGER"


def _real(pg):
    return "DOUBLE PRECISION" if pg else "REAL"


# Each migration: (version, description, function(pg) -> list of SQL statements)
MIGRATIONS = [
    (1, "users and sessions", lambda pg: [
        f"""CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at {_big(pg)} NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at {_big(pg)} NOT NULL,
            expires_at {_big(pg)} NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)",
    ]),
    (2, "saved lead history", lambda pg: [
        f"""CREATE TABLE IF NOT EXISTS leads (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT,
            message TEXT NOT NULL,
            score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
            tier TEXT NOT NULL CHECK (tier IN ('Hot', 'Warm', 'Cold')),
            intent TEXT,
            location TEXT,
            engine TEXT NOT NULL CHECK (engine IN ('ai', 'fallback')),
            source TEXT NOT NULL CHECK (source IN ('single', 'batch')),
            result TEXT NOT NULL,
            created_at {_big(pg)} NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_leads_user_created ON leads(user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_leads_user_tier ON leads(user_id, tier)",
    ]),
    (3, "password reset tokens", lambda pg: [
        f"""CREATE TABLE IF NOT EXISTS password_resets (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at {_big(pg)} NOT NULL,
            expires_at {_big(pg)} NOT NULL,
            used_at {_big(pg)}
        )""",
        "CREATE INDEX IF NOT EXISTS idx_resets_user ON password_resets(user_id)",
    ]),
    (4, "lead pipeline, listings, activity log, roles and lockout", lambda pg: [
        "ALTER TABLE leads ADD COLUMN status TEXT NOT NULL DEFAULT 'New'",
        "ALTER TABLE leads ADD COLUMN phone TEXT",
        "ALTER TABLE leads ADD COLUMN email TEXT",
        f"ALTER TABLE leads ADD COLUMN budget {_real(pg)}",
        "ALTER TABLE leads ADD COLUMN currency TEXT",
        "ALTER TABLE leads ADD COLUMN property_type TEXT",
        "ALTER TABLE leads ADD COLUMN dedupe_key TEXT",
        "ALTER TABLE leads ADD COLUMN duplicate_of TEXT",
        "ALTER TABLE leads ADD COLUMN notes TEXT",
        f"ALTER TABLE leads ADD COLUMN updated_at {_big(pg)}",
        f"ALTER TABLE leads ADD COLUMN status_changed_at {_big(pg)}",
        "CREATE INDEX IF NOT EXISTS idx_leads_user_status ON leads(user_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_leads_user_dedupe ON leads(user_id, dedupe_key)",
        f"""CREATE TABLE IF NOT EXISTS listings (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            location TEXT NOT NULL,
            deal TEXT NOT NULL CHECK (deal IN ('sale', 'rent')),
            price {_real(pg)} NOT NULL CHECK (price > 0),
            currency TEXT NOT NULL CHECK (currency IN ('INR', 'USD')),
            property_type TEXT,
            bedrooms INTEGER,
            status TEXT NOT NULL DEFAULT 'available',
            url TEXT,
            created_at {_big(pg)} NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_listings_user ON listings(user_id, created_at DESC)",
        f"""CREATE TABLE IF NOT EXISTS activity_log (
            id TEXT PRIMARY KEY,
            user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            detail TEXT,
            ip TEXT,
            created_at {_big(pg)} NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_activity_user ON activity_log(user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_log(created_at DESC)",
        "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'agent'",
        "ALTER TABLE users ADD COLUMN failed_logins INTEGER NOT NULL DEFAULT 0",
        f"ALTER TABLE users ADD COLUMN locked_until {_big(pg)}",
    ]),
]


class Database:
    def __init__(self, url=None, path=None):
        self.url = url or ""
        self.is_pg = self.url.startswith(("postgres://", "postgresql://"))
        self.path = str(path or BASE_DIR / "leadscout.db")
        self._lock = threading.Lock()
        if self.is_pg:
            import psycopg  # noqa: F401  (fail fast with a clear error if missing)

    @property
    def kind(self):
        return "postgres" if self.is_pg else "sqlite"

    @contextmanager
    def connect(self):
        if self.is_pg:
            import psycopg
            conn = psycopg.connect(self.url, connect_timeout=10)
        else:
            conn = sqlite3.connect(self.path, timeout=10)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def q(self, sql):
        return sql.replace("?", "%s") if self.is_pg else sql

    @contextmanager
    def transaction(self):
        """Run several statements atomically: `with db.transaction() as run: run(sql, params)`."""
        with self.connect() as conn:
            cur = conn.cursor()

            def run(sql, params=()):
                cur.execute(self.q(sql), params)
                return cur
            yield run

    def execute(self, sql, params=()):
        with self.transaction() as run:
            return run(sql, params).rowcount

    def fetchone(self, sql, params=()):
        with self.transaction() as run:
            return run(sql, params).fetchone()

    def fetchall(self, sql, params=()):
        with self.transaction() as run:
            return run(sql, params).fetchall()

    def ping(self):
        return self.fetchone("SELECT 1")[0] == 1

    def migrate(self):
        """Apply any migrations newer than the recorded schema version. Returns the version."""
        with self._lock, self.transaction() as run:
            run("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = run("SELECT MAX(version) FROM schema_version").fetchone()
            current = row[0] or 0
            for version, _desc, stmts in MIGRATIONS:
                if version <= current:
                    continue
                for sql in stmts(self.is_pg):
                    run(sql)
                run("INSERT INTO schema_version (version) VALUES (?)", (version,))
                current = version
        return current

    # backwards compatible name used by older code/tests
    init = migrate
