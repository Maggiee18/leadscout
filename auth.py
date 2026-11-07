"""
LeadScout accounts: users, password hashing, cookie sessions, password resets.

Security notes
- Passwords: scrypt (N=2^14, r=8, p=1) with a random 16 byte salt per user.
- Session and reset tokens are random 256-bit values. The database stores only their
  SHA-256, so a leaked database can't be replayed as live sessions or reset links.
- Changing or resetting a password signs out every other session.
"""

import hashlib
import hmac
import re
import secrets
import time
import uuid

from db import Database

SESSION_DAYS = 7
REMEMBER_DAYS = 30
RESET_MINUTES = 30
MAX_FAILED_LOGINS = 5      # wrong passwords in a row before the account pauses
LOCK_MINUTES = 15
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
COMMON_PASSWORDS = {"password1", "password123", "12345678a", "qwerty123", "leadscout1",
                    "abc12345", "iloveyou1", "welcome1", "admin123", "letmein1"}

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
_DUMMY_HASH = None


class AuthError(Exception):
    """A user-facing problem (bad input, taken email, wrong password)."""

    def __init__(self, message, status=400, field=None, user_id=None):
        super().__init__(message)
        self.status, self.field, self.user_id = status, field, user_id


# ---------- Passwords ----------

def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${dk.hex()}"


def verify_password(password, stored):
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r),
                            p=int(p), dklen=len(bytes.fromhex(digest)))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


def _dummy_hash():
    """A real hash used to spend the same time when an email has no account."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_hex(16))
    return _DUMMY_HASH


# ---------- Validation ----------

def clean_email(email):
    email = (email or "").strip().lower()
    if not email:
        raise AuthError("Enter your email address.", field="email")
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise AuthError("Enter a valid email address, like name@example.com.", field="email")
    return email


def clean_name(name):
    name = re.sub(r"\s+", " ", (name or "").strip())
    if len(name) < 2:
        raise AuthError("Enter your full name.", field="name")
    if len(name) > 80:
        raise AuthError("Keep your name under 80 characters.", field="name")
    if re.search(r"[<>]", name):
        raise AuthError("Your name can't contain < or >.", field="name")
    return name


def check_password_rules(password, email=None, field="password"):
    password = password or ""
    if len(password) < 8:
        raise AuthError("Use at least 8 characters for your password.", field=field)
    if len(password) > 128:
        raise AuthError("Keep your password under 128 characters.", field=field)
    if not (re.search(r"[A-Za-z]", password) and re.search(r"\d", password)):
        raise AuthError("Use at least one letter and one number.", field=field)
    low = password.lower()
    if email and low == email.split("@")[0].lower():
        raise AuthError("Your password can't be the same as your email name.", field=field)
    if low in COMMON_PASSWORDS:
        raise AuthError("That password is too common. Pick something harder to guess.", field=field)
    return password


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _public(row):
    return {"id": row[0], "name": row[1], "email": row[2], "created_at": row[3], "role": row[4] or "agent"}


USER_COLS = "u.id, u.name, u.email, u.created_at, u.role"
ROLES = ("agent", "admin")


class Accounts:
    def __init__(self, db: Database, admin_emails=()):
        self.db = db
        self.admin_emails = {e.strip().lower() for e in admin_emails if e.strip()}

    def sync_admins(self):
        """Accounts listed in ADMIN_EMAILS are admins; everyone else is an agent."""
        if not self.admin_emails:
            return
        marks = ", ".join("?" * len(self.admin_emails))
        self.db.execute(f"UPDATE users SET role = 'admin' WHERE email IN ({marks})", tuple(self.admin_emails))

    def list_users(self):
        rows = self.db.fetchall(
            f"SELECT {USER_COLS}, (SELECT COUNT(*) FROM leads l WHERE l.user_id = u.id), u.locked_until "
            "FROM users u ORDER BY u.created_at DESC LIMIT 500")
        now = int(time.time())
        return [{**_public(r), "leads": r[5], "locked": bool(r[6] and r[6] > now)} for r in rows]

    # ----- registration & login -----

    def register(self, name, email, password):
        name, email = clean_name(name), clean_email(email)
        check_password_rules(password, email)
        if self.db.fetchone("SELECT 1 FROM users WHERE email = ?", (email,)):
            raise AuthError("An account with this email already exists. Try logging in.",
                            status=409, field="email")
        uid, now = uuid.uuid4().hex, int(time.time())
        role = "admin" if email in self.admin_emails else "agent"
        try:
            self.db.execute("INSERT INTO users (id, name, email, password_hash, created_at, role) "
                            "VALUES (?, ?, ?, ?, ?, ?)", (uid, name, email, hash_password(password), now, role))
        except Exception as exc:  # lost a race on the unique email index
            if "unique" in str(exc).lower() or "integrity" in type(exc).__name__.lower():
                raise AuthError("An account with this email already exists. Try logging in.",
                                status=409, field="email") from exc
            raise
        return {"id": uid, "name": name, "email": email, "created_at": now, "role": role}

    def authenticate(self, email, password):
        generic = AuthError("That email and password don't match. Please try again.", status=401)
        try:
            email = clean_email(email)
        except AuthError:
            verify_password(password or "", _dummy_hash())
            raise generic from None
        row = self.db.fetchone(f"SELECT {USER_COLS}, u.password_hash, u.locked_until FROM users u WHERE u.email = ?",
                               (email,))
        if not row:
            verify_password(password or "", _dummy_hash())
            raise generic
        now = int(time.time())
        if row[6] and row[6] > now:
            verify_password(password or "", _dummy_hash())
            minutes = max(1, -(-(row[6] - now) // 60))
            raise AuthError(f"This account is paused after too many wrong passwords. Try again in {minutes} "
                            f"minute{'s' if minutes > 1 else ''}, or reset your password.", status=429)
        if not verify_password(password or "", row[5]):
            with self.db.transaction() as run:
                fails = run("UPDATE users SET failed_logins = failed_logins + 1 WHERE id = ? "
                            "RETURNING failed_logins", (row[0],)).fetchone()[0]
                locked = fails >= MAX_FAILED_LOGINS
                if locked:
                    run("UPDATE users SET failed_logins = 0, locked_until = ? WHERE id = ?",
                        (now + LOCK_MINUTES * 60, row[0]))
            if locked:
                raise AuthError(f"Too many wrong passwords. This account is paused for {LOCK_MINUTES} "
                                "minutes. You can reset your password to get back in sooner.",
                                status=429, user_id=row[0])
            raise AuthError(str(generic), status=401, user_id=row[0])
        self.db.execute("UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = ? "
                        "AND (failed_logins > 0 OR locked_until IS NOT NULL)", (row[0],))
        return _public(row)

    def find_by_email(self, email):
        try:
            email = clean_email(email)
        except AuthError:
            return None
        row = self.db.fetchone(f"SELECT {USER_COLS} FROM users u WHERE u.email = ?", (email,))
        return _public(row) if row else None

    # ----- sessions -----

    def create_session(self, user_id, remember=False):
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        days = REMEMBER_DAYS if remember else SESSION_DAYS
        self.db.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
                        "VALUES (?, ?, ?, ?)", (_token_hash(token), user_id, now, now + days * 86400))
        if secrets.randbelow(20) == 0:  # occasional cleanup of expired rows
            with self.db.transaction() as run:
                run("DELETE FROM sessions WHERE expires_at < ?", (now,))
                run("DELETE FROM password_resets WHERE expires_at < ?", (now - 86400,))
        return token, days * 86400

    def user_for_token(self, token):
        if not token or len(token) > 200:
            return None
        row = self.db.fetchone(
            f"SELECT {USER_COLS} FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?", (_token_hash(token), int(time.time())))
        return _public(row) if row else None

    def end_session(self, token):
        if token:
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    def end_other_sessions(self, user_id, keep_token=None):
        if keep_token:
            return self.db.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash <> ?",
                                   (user_id, _token_hash(keep_token)))
        return self.db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    # ----- account settings -----

    def _check_current_password(self, user_id, password, field="current_password"):
        row = self.db.fetchone("SELECT password_hash FROM users WHERE id = ?", (user_id,))
        if not row or not verify_password(password or "", row[0]):
            raise AuthError("Your current password isn't correct.", status=400, field=field)

    def update_name(self, user_id, name):
        name = clean_name(name)
        self.db.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))
        row = self.db.fetchone(f"SELECT {USER_COLS} FROM users u WHERE u.id = ?", (user_id,))
        return _public(row)

    def change_password(self, user_id, current, new, keep_token):
        self._check_current_password(user_id, current)
        email = self.db.fetchone("SELECT email FROM users WHERE id = ?", (user_id,))[0]
        check_password_rules(new, email, field="new_password")
        if current == new:
            raise AuthError("Pick a password that's different from your current one.",
                            field="new_password")
        self.db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new), user_id))
        return self.end_other_sessions(user_id, keep_token)

    def delete_account(self, user_id, password):
        self._check_current_password(user_id, password, field="password")
        with self.db.transaction() as run:  # explicit deletes: don't rely only on cascades
            for table in ("leads", "password_resets", "sessions"):
                run(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
            run("DELETE FROM users WHERE id = ?", (user_id,))

    # ----- password reset -----

    def create_reset(self, user_id):
        """Return a single-use reset token valid for RESET_MINUTES. Older unused ones are voided."""
        token, now = secrets.token_urlsafe(32), int(time.time())
        with self.db.transaction() as run:
            run("UPDATE password_resets SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
                (now, user_id))
            run("INSERT INTO password_resets (token_hash, user_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)", (_token_hash(token), user_id, now, now + RESET_MINUTES * 60))
        return token

    def recent_reset_count(self, user_id, window=3600):
        row = self.db.fetchone("SELECT COUNT(*) FROM password_resets WHERE user_id = ? AND created_at > ?",
                               (user_id, int(time.time()) - window))
        return row[0]

    def reset_password(self, token, new_password):
        invalid = AuthError("This reset link is invalid or has expired. Request a new one.",
                            status=400, field="token")
        if not token or len(token) > 200:
            raise invalid
        now = int(time.time())
        row = self.db.fetchone(
            "SELECT r.user_id, u.email FROM password_resets r JOIN users u ON u.id = r.user_id "
            "WHERE r.token_hash = ? AND r.used_at IS NULL AND r.expires_at > ?",
            (_token_hash(token), now))
        if not row:
            raise invalid
        user_id, email = row
        check_password_rules(new_password, email)
        with self.db.transaction() as run:
            # mark used first, guarded, so two concurrent submits can't both succeed
            cur = run("UPDATE password_resets SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
                      (now, _token_hash(token)))
            if cur.rowcount != 1:
                raise invalid
            run("UPDATE users SET password_hash = ?, failed_logins = 0, locked_until = NULL WHERE id = ?",
                (hash_password(new_password), user_id))
            run("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        row = self.db.fetchone(f"SELECT {USER_COLS} FROM users u WHERE u.id = ?", (user_id,))
        return _public(row)
