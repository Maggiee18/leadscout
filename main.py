"""
LeadScout API: FastAPI service that serves the web app, scores leads and manages accounts.

    The AI reads. The rules score. The agent decides.

Public
    GET  /                         3D landing page
    GET  /app                      the web app
    GET  /privacy  /terms          legal pages
    GET  /healthz  /readyz         liveness / readiness (database)
    GET  /api/status               AI or fallback status and limits
    GET  /api/samples              sample leads

Accounts
    POST /api/auth/register | login | logout | forgot | reset
    GET  /api/auth/me
    PATCH /api/account             change name
    POST /api/account/password     change password (signs out other devices)
    POST /api/account/delete       delete the account and all its data

Leads (signed in)
    POST /api/score                score one lead (saved to history)
    POST /api/batch                score a CSV upload (saved to history)
    POST /api/batch/sample         score sample_leads.csv (saved to history)
    GET  /api/leads                history: search, filter, sort, paginate
    GET  /api/leads/stats          dashboard numbers
    GET  /api/leads/{id}           one saved lead
    DELETE /api/leads/{id}         delete one
    DELETE /api/leads              delete all
    PATCH  /api/leads/{id}         edit details or status (re-scores when scoring facts change)
    GET    /api/leads/export.csv   download leads as CSV (spreadsheet formulas neutralised)
    GET    /api/leads/locations    locations seen, for the filter

Listings (signed in)
    GET/POST /api/listings         list / add a property listing
    POST /api/listings/import      import listings from CSV or Excel
    POST /api/listings/sample      add example listings to try matching
    PATCH/DELETE /api/listings/{id}

Activity and admin
    GET /api/activity              your own activity log
    GET /api/admin/users           admins only: all users
    GET /api/admin/activity        admins only: everyone's activity
"""

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from contextvars import ContextVar
from datetime import datetime, timezone
from urllib.parse import urlparse

import pandas as pd
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

import analysis
import config
import mailer
from activity import ActivityLog
from auth import RESET_MINUTES, Accounts, AuthError
from db import Database
from leads import EDITABLE, STATUSES, LeadStore
from listings import ListingStore, clean_listing, read_listings_csv

STATIC_DIR = config.BASE_DIR / "static"
SAMPLE_CSV = config.BASE_DIR / "sample_leads.csv"
SAMPLE_LISTINGS = config.BASE_DIR / "sample_listings.csv"
MAX_UPLOAD_BYTES = 512 * 1024
SESSION_COOKIE = "ls_session"
VERSION = "4.0.0"

# ---------- Logging (JSON in production, never any lead content) ----------

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
                "level": record.levelname, "logger": record.name, "msg": record.getMessage(),
                "request_id": request_id_var.get()}
        for key in ("event", "method", "path", "status", "ms", "user"):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        if record.exc_info:
            data["error"] = record.exc_info[0].__name__
        return json.dumps(data)


def _setup_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if config.LOG_FORMAT == "json"
                         else logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(config.LOG_LEVEL)
    logging.getLogger("uvicorn.access").disabled = True  # replaced by our access log (no query strings)


_setup_logging()
log = logging.getLogger("leadscout")

# ---------- Storage ----------

db = Database(url=config.DATABASE_URL, path=config.DB_PATH)
schema = db.migrate()
accounts = Accounts(db, admin_emails=config.ADMIN_EMAILS)
accounts.sync_admins()
lead_store = LeadStore(db)
lead_store.backfill()
listing_store = ListingStore(db)
activity = ActivityLog(db)
activity.prune()
log.info("database ready: %s, schema v%s", db.kind, schema)
if config.IS_PROD and not db.is_pg:
    log.warning("production is using SQLite; data is lost if the disk is ephemeral (set DATABASE_URL)")
if config.IS_PROD and not config.APP_URL:
    log.warning("APP_URL is not set; password reset emails are disabled")

app = FastAPI(title="LeadScout", version=VERSION, docs_url="/api/docs", redoc_url=None)

# ---------- Versioned static assets (cache busting) ----------


def _asset_version():
    h = hashlib.sha256()  # only a cache-busting fingerprint
    for name in ("app.js", "styles.css", "landing.js", "landing.css", "landing3d.js"):
        h.update((STATIC_DIR / name).read_bytes())
    return h.hexdigest()[:10]


ASSET_VERSION = _asset_version()


def _render_page(name):
    html = (STATIC_DIR / name).read_text(encoding="utf-8")
    for asset in ("/static/app.js", "/static/styles.css", "/static/landing.js", "/static/landing.css"):
        html = html.replace(f'"{asset}"', f'"{asset}?v={ASSET_VERSION}"')
    return html


PAGES = {name: _render_page(name) for name in ("landing.html", "index.html", "privacy.html", "terms.html")}

# ---------- Middleware ----------

app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")
_RID = re.compile(r"^[A-Za-z0-9\-]{8,64}$")


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Request id, same-origin guard, security headers, caching rules and access log."""
    incoming = request.headers.get("x-request-id", "")
    rid = incoming if _RID.match(incoming) else uuid.uuid4().hex[:16]
    token = request_id_var.set(rid)
    start = time.perf_counter()
    try:
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if origin and origin != "null":
                host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
                if urlparse(origin).netloc != host:
                    blocked = JSONResponse({"detail": "Cross-site request blocked."}, status_code=403)
                    return _finish(request, blocked, rid, start)
        response = await call_next(request)
        return _finish(request, response, rid, start)
    finally:
        request_id_var.reset(token)


def _finish(request, response, rid, start):
    path = request.url.path
    h = response.headers
    h["X-Request-ID"] = rid
    h["X-Content-Type-Options"] = "nosniff"
    h["Referrer-Policy"] = "no-referrer"
    h["X-Frame-Options"] = "DENY"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    if config.IS_PROD:
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if not path.startswith("/api/docs") and path != "/openapi.json":
        h["Content-Security-Policy"] = CSP
    if path.startswith("/api/"):
        h["Cache-Control"] = "no-store"
    elif path.startswith("/static/"):
        h["Cache-Control"] = ("public, max-age=31536000, immutable" if request.query_params.get("v")
                              else "no-cache")
    if not path.startswith("/static/") and path not in ("/healthz", "/favicon.ico"):
        ms = round((time.perf_counter() - start) * 1000, 1)
        log.info("%s %s %s %sms", request.method, path, response.status_code, ms,
                 extra={"event": "request", "method": request.method, "path": path,
                        "status": response.status_code, "ms": ms})
    return response


# ---------- Per-IP rate limit (in memory, single instance) ----------

_hits = defaultdict(deque)
_hits_lock = threading.Lock()


def client_ip(request: Request):
    # Behind Render's proxy the right-most X-Forwarded-For entry is the one it added
    return request.headers.get("x-forwarded-for", "").split(",")[-1].strip() or (
        request.client.host if request.client else "unknown")


def rate_limit(request: Request, bucket: str, limit: int, window: int = 60):
    ip, now = client_ip(request), time.monotonic()
    with _hits_lock:
        q = _hits[(bucket, ip)]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(429, "Too many requests. Please wait a minute and try again.",
                                headers={"Retry-After": str(window)})
        q.append(now)


# ---------- Errors: always JSON, never a stack trace ----------

@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    msg = str(first.get("msg", "Invalid request")).removeprefix("Value error, ")
    field = first.get("loc", [""])[-1]
    if first.get("type") in ("missing", "json_invalid", "model_attributes_type"):
        if request.url.path.startswith(("/api/auth", "/api/account")):
            msg = "Please fill in all the fields."
        else:
            msg = "Send JSON with a 'message' field." if field in ("message", "body") else msg
    return JSONResponse({"detail": msg}, status_code=422)


@app.exception_handler(AuthError)
async def auth_error(_: Request, exc: AuthError):
    return JSONResponse({"detail": str(exc), "field": exc.field}, status_code=exc.status)


@app.exception_handler(Exception)
async def unexpected_error(_: Request, exc: Exception):
    log.error("unhandled error: %s", type(exc).__name__, extra={"event": "error"})
    return JSONResponse({"detail": "Something went wrong on our side. Please try again."},
                        status_code=500)


# ---------- Request models ----------

class LeadIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    message: str
    phone: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=120)
    use_ai: bool = True

    @field_validator("message")
    @classmethod
    def check_message(cls, v):
        v = (v or "").strip()
        if len(v) < 10:
            raise ValueError("The message is too short. Paste the full inquiry (10+ characters).")
        if len(v) > analysis.MAX_MESSAGE_CHARS:
            raise ValueError(f"The message is too long (max {analysis.MAX_MESSAGE_CHARS} characters).")
        return v

    @field_validator("name")
    @classmethod
    def check_name(cls, v):
        v = (v or "").strip()
        return v[:analysis.MAX_NAME_CHARS] or None


class RegisterIn(BaseModel):
    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=320)
    password: str = Field(default="", max_length=256)


class LoginIn(BaseModel):
    email: str = Field(default="", max_length=320)
    password: str = Field(default="", max_length=256)
    remember: bool = False


class ForgotIn(BaseModel):
    email: str = Field(default="", max_length=320)


class ResetIn(BaseModel):
    token: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=256)


class NameIn(BaseModel):
    name: str = Field(default="", max_length=200)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(default="", max_length=256)
    new_password: str = Field(default="", max_length=256)


class DeleteAccountIn(BaseModel):
    password: str = Field(default="", max_length=256)


# ---------- Sessions ----------

def current_user(request: Request):
    user = accounts.user_for_token(request.cookies.get(SESSION_COOKIE))
    if not user:
        raise HTTPException(401, "Please log in to continue.")
    return user


def _set_session_cookie(request: Request, response: Response, token: str, max_age: int):
    secure = (config.COOKIE_SECURE == "true" or (config.COOKIE_SECURE == "auto" and (
        request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")))
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True, secure=secure,
                        samesite="lax", path="/")


def _start_session(request, response, user, remember=False):
    token, max_age = accounts.create_session(user["id"], remember=remember)
    _set_session_cookie(request, response, token, max_age)


# ---------- Pages ----------

def _page(name):
    return HTMLResponse(PAGES[name], headers={"Cache-Control": "no-cache"})


@app.get("/", include_in_schema=False)
def landing():
    return _page("landing.html")


@app.get("/app", include_in_schema=False)
def web_app():
    return _page("index.html")


@app.get("/privacy", include_in_schema=False)
def privacy():
    return _page("privacy.html")


@app.get("/terms", include_in_schema=False)
def terms():
    return _page("terms.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse("User-agent: *\nAllow: /$\nAllow: /privacy\nAllow: /terms\n"
                             "Disallow: /api/\nDisallow: /app\n")


@app.get("/healthz", include_in_schema=False)
def health():
    return {"ok": True, "version": VERSION}


@app.get("/readyz", include_in_schema=False)
def ready():
    try:
        db.ping()
    except Exception as exc:
        log.error("readiness check failed: %s", type(exc).__name__)
        return JSONResponse({"ok": False, "database": "unavailable"}, status_code=503)
    return {"ok": True, "database": db.kind}


@app.get("/sample_leads.csv", include_in_schema=False)
def sample_csv():
    return FileResponse(SAMPLE_CSV, media_type="text/csv", filename="sample_leads.csv")


# ---------- Public API ----------

@app.get("/api/status")
def status():
    configured = bool(config.API_KEY)
    st = analysis._ai_state
    return {
        "ai_configured": configured,
        "mode": "ai" if configured else "fallback",
        "provider": "Groq" if "groq" in config.BASE_URL else "OpenAI-compatible",
        "model": config.MODEL if configured else None,
        "last_ai_success": st["last_ok"],
        "last_ai_error": st["last_error"],
        "last_ai_error_at": st["last_error_at"],
        "email_configured": mailer.configured(),
        "limits": {"max_batch": analysis.MAX_BATCH, "max_message_chars": analysis.MAX_MESSAGE_CHARS,
                   "max_upload_kb": MAX_UPLOAD_BYTES // 1024},
    }


@app.get("/api/samples")
def samples():
    df = pd.read_csv(SAMPLE_CSV, dtype=str, keep_default_na=False)
    return {"samples": df[["name", "message"]].to_dict(orient="records")}


# ---------- Auth ----------

@app.post("/api/auth/register", status_code=201)
def register(body: RegisterIn, request: Request, response: Response):
    rate_limit(request, "auth", config.RATE_AUTH)
    user = accounts.register(body.name, body.email, body.password)
    _start_session(request, response, user)
    activity.add(user["id"], "register", ip=client_ip(request))
    log.info("account created", extra={"event": "register", "user": user["id"]})
    return {"user": user}


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response):
    rate_limit(request, "auth", config.RATE_AUTH)
    try:
        user = accounts.authenticate(body.email, body.password)
    except AuthError as exc:
        log.info("login failed", extra={"event": "login_failed"})
        if exc.user_id:
            activity.add(exc.user_id, "account_locked" if exc.status == 429 else "login_failed",
                         ip=client_ip(request))
        raise
    _start_session(request, response, user, remember=body.remember)
    activity.add(user["id"], "login", ip=client_ip(request))
    return {"user": user}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    user = accounts.user_for_token(request.cookies.get(SESSION_COOKIE))
    if user:
        activity.add(user["id"], "logout", ip=client_ip(request))
    accounts.end_session(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/session")
def session(request: Request):
    """Who is signed in, if anyone. Always 200, for public pages that adapt to the visitor."""
    return {"user": accounts.user_for_token(request.cookies.get(SESSION_COOKIE))}


@app.get("/api/auth/me")
def me(user: dict = Depends(current_user)):
    return {"user": user}


FORGOT_REPLY = {"ok": True, "detail": "If an account exists for that email, we've sent a link to reset "
                                      "the password. It expires in 30 minutes."}


def _public_base_url(request: Request):
    if config.APP_URL:
        return config.APP_URL
    if config.IS_PROD:
        return None  # never trust the Host header for links in production
    return str(request.base_url).rstrip("/")


@app.post("/api/auth/forgot")
def forgot(body: ForgotIn, request: Request):
    rate_limit(request, "forgot", 5)
    user = accounts.find_by_email(body.email)
    base = _public_base_url(request)
    # Same reply whether or not the account exists, so emails can't be discovered here.
    if not user or not base or accounts.recent_reset_count(user["id"]) >= 3:
        return FORGOT_REPLY
    token = accounts.create_reset(user["id"])
    link = f"{base}/app#/reset?token={token}"  # in the fragment: never sent to servers or logs
    first = user["name"].split(" ")[0]
    text = (f"Hi {first},\n\nSomeone (hopefully you) asked to reset your LeadScout password.\n"
            f"Open this link within {RESET_MINUTES} minutes to choose a new one:\n\n{link}\n\n"
            "If you didn't ask for this, you can ignore this email. Your password won't change.\n\n"
            "LeadScout")
    html = (f"<p>Hi {first},</p><p>Someone (hopefully you) asked to reset your LeadScout password.</p>"
            f'<p><a href="{link}" style="background:#F97316;color:#fff;padding:12px 20px;border-radius:10px;'
            f'text-decoration:none;font-weight:700;display:inline-block">Choose a new password</a></p>'
            f"<p>The link expires in {RESET_MINUTES} minutes. If you didn't ask for this, ignore this "
            "email and your password won't change.</p><p>LeadScout</p>")
    mailer.send(user["email"], "Reset your LeadScout password", text, html)
    log.info("password reset requested", extra={"event": "reset_requested", "user": user["id"]})
    return FORGOT_REPLY


@app.post("/api/auth/reset")
def reset(body: ResetIn, request: Request, response: Response):
    rate_limit(request, "auth", config.RATE_AUTH)
    user = accounts.reset_password(body.token, body.password)
    _start_session(request, response, user)
    activity.add(user["id"], "password_reset", ip=client_ip(request))
    log.info("password reset completed", extra={"event": "reset_done", "user": user["id"]})
    return {"user": user}


# ---------- Account settings ----------

@app.patch("/api/account")
def update_account(body: NameIn, user: dict = Depends(current_user)):
    return {"user": accounts.update_name(user["id"], body.name)}


@app.post("/api/account/password")
def change_password(body: PasswordChangeIn, request: Request, user: dict = Depends(current_user)):
    rate_limit(request, "auth", config.RATE_AUTH)
    ended = accounts.change_password(user["id"], body.current_password, body.new_password,
                                     keep_token=request.cookies.get(SESSION_COOKIE))
    log.info("password changed", extra={"event": "password_changed", "user": user["id"]})
    activity.add(user["id"], "password_changed", ip=client_ip(request))
    return {"ok": True, "other_sessions_ended": ended}


@app.post("/api/account/delete")
def delete_account(body: DeleteAccountIn, request: Request, response: Response,
                   user: dict = Depends(current_user)):
    rate_limit(request, "auth", config.RATE_AUTH)
    accounts.delete_account(user["id"], body.password)
    response.delete_cookie(SESSION_COOKIE, path="/")
    log.info("account deleted", extra={"event": "account_deleted", "user": user["id"]})
    return {"ok": True}


# ---------- Scoring ----------

@app.post("/api/score")
def score(lead: LeadIn, request: Request, user: dict = Depends(current_user)):
    rate_limit(request, "score", config.RATE_SCORE)
    result = analysis.analyze(lead.name, lead.message, use_ai=lead.use_ai,
                              listings=listing_store.all(user["id"], available_only=True),
                              contact={"phone": lead.phone, "email": lead.email})
    lead_store.save_many(user["id"], [result], "single")
    activity.add(user["id"], "lead_scored", f"{result['tier']} lead, score {result['score']}", client_ip(request))
    return result


def _batch_and_save(user_id, rows, skipped, use_ai, ip, label):
    out = analysis._run_batch(rows, skipped, use_ai, listings=listing_store.all(user_id, available_only=True))
    lead_store.save_many(user_id, out["results"], "batch")
    out["duplicates"] = sum(1 for r in out["results"] if r.get("duplicate_of"))
    activity.add(user_id, "batch_uploaded", f"{label}: {out['count']} leads", ip)
    return out


async def _read_upload(file: UploadFile):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"The file is larger than {MAX_UPLOAD_BYTES // 1024} KB.")
    return data


@app.post("/api/batch")
async def batch(request: Request, file: UploadFile = File(...), use_ai: bool = True,
                user: dict = Depends(current_user)):
    rate_limit(request, "batch", config.RATE_BATCH)
    data = await _read_upload(file)
    rows, skipped = analysis.read_leads_file(data, file.filename or "")
    return await run_in_threadpool(_batch_and_save, user["id"], rows, skipped, use_ai, client_ip(request),
                                   "File")


@app.post("/api/batch/sample")
def batch_sample(request: Request, use_ai: bool = True, user: dict = Depends(current_user)):
    rate_limit(request, "batch", config.RATE_BATCH)
    rows, skipped = analysis.read_leads_file(SAMPLE_CSV.read_bytes(), "sample_leads.csv")
    return _batch_and_save(user["id"], rows, skipped, use_ai, client_ip(request), "Sample file")


# ---------- Leads ----------

def _num_param(value, name):
    if value in (None, ""):
        return None
    try:
        n = float(value)
    except ValueError:
        raise HTTPException(422, f"{name} must be a number.") from None
    if n < 0:
        raise HTTPException(422, f"{name} can't be negative.")
    return n


def _filters(q, tier, status, location, budget_min, budget_max, currency, duplicates, property_type):
    return {"q": q, "tier": tier, "status": status, "location": location,
            "budget_min": _num_param(budget_min, "Minimum budget"),
            "budget_max": _num_param(budget_max, "Maximum budget"),
            "currency": currency, "duplicates": duplicates, "property_type": property_type}


@app.get("/api/leads")
def list_leads(q: str = "", tier: str = "all", status: str = "all", location: str = "", budget_min: str = "",
               budget_max: str = "", currency: str = "", duplicates: bool = False, property_type: str = "",
               sort: str = "newest", limit: int = 20, offset: int = 0, user: dict = Depends(current_user)):
    return lead_store.list(user["id"], sort=sort, limit=limit, offset=offset,
                           **_filters(q, tier, status, location, budget_min, budget_max, currency, duplicates,
                                      property_type))


@app.get("/api/leads/stats")
def lead_stats(user: dict = Depends(current_user)):
    return lead_store.stats(user["id"])


@app.get("/api/leads/locations")
def lead_locations(user: dict = Depends(current_user)):
    return {"locations": lead_store.locations(user["id"])}


def _csv_cell(value):
    """Neutralise spreadsheet formulas (CSV injection) and quote safely."""
    s = "" if value is None else str(value)
    if s[:1] in ("=", "+", "-", "@", "\t", "\r"):
        s = "'" + s
    return '"' + s.replace('"', '""') + '"'


@app.get("/api/leads/export.csv")
def export_leads(request: Request, q: str = "", tier: str = "all", status: str = "all", location: str = "",
                 budget_min: str = "", budget_max: str = "", currency: str = "", duplicates: bool = False,
                 property_type: str = "", user: dict = Depends(current_user)):
    rate_limit(request, "export", 10)
    filters = _filters(q, tier, status, location, budget_min, budget_max, currency, duplicates, property_type)
    head = ["Name", "Phone", "Email", "Score", "Tier", "Status", "Intent", "Property", "Budget", "Location",
            "Timeline", "Financing", "Sentiment", "Confidence %", "Next action", "Message", "Saved (UTC)"]
    lines, offset = [",".join(head)], 0
    while True:
        page = lead_store.list(user["id"], sort="score-desc", limit=100, offset=offset, **filters)
        for r in page["items"]:
            d = r["display"]
            saved = datetime.fromtimestamp(r["saved_at"], timezone.utc).strftime("%Y-%m-%d %H:%M")
            lines.append(",".join(_csv_cell(v) for v in (
                r.get("name"), r.get("phone"), r.get("email"), r["score"], r["tier"], r.get("status"),
                d.get("intent"), d.get("property"), d.get("budget"), d.get("location"), d.get("timeline"),
                d.get("financing"), d.get("sentiment"), r.get("confidence"), r.get("next_action"),
                r["message"], saved)))
        offset += 100
        if offset >= page["total"] or offset >= 5000:
            break
    activity.add(user["id"], "leads_exported", f"{len(lines) - 1} leads", client_ip(request))
    return Response("\ufeff" + "\r\n".join(lines), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="leadscout_leads.csv"'})


@app.get("/api/leads/{lead_id}")
def get_lead(lead_id: str, user: dict = Depends(current_user)):
    lead = lead_store.get(user["id"], lead_id[:64])
    if not lead:
        raise HTTPException(404, "That lead wasn't found. It may have been deleted.")
    return lead


class LeadEditIn(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    phone: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=120)
    location: str | None = Field(default=None, max_length=80)
    budget: float | None = Field(default=None, ge=0, le=1e12)
    currency: str | None = None
    property_type: str | None = None
    bedrooms: int | None = Field(default=None, ge=0, le=20)
    intent: str | None = None
    timeline_months: float | None = Field(default=None, ge=0, le=120)
    financing: str | None = None
    notes: str | None = Field(default=None, max_length=1000)
    status: str | None = None

    @field_validator("status")
    @classmethod
    def check_status(cls, v):
        if v is not None and v not in STATUSES:
            raise ValueError(f"Status must be one of: {', '.join(STATUSES)}.")
        return v

    @field_validator("phone")
    @classmethod
    def check_phone(cls, v):
        if v and not re.fullmatch(r"\+?[\d\s().-]{7,20}", v.strip()):
            raise ValueError("Enter a valid phone number.")
        return v

    @field_validator("email")
    @classmethod
    def check_email(cls, v):
        if v and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v.strip()):
            raise ValueError("Enter a valid email address.")
        return v


@app.patch("/api/leads/{lead_id}")
def edit_lead(lead_id: str, body: LeadEditIn, request: Request, user: dict = Depends(current_user)):
    rate_limit(request, "edit", 60)
    changes = body.model_dump(exclude_unset=True)
    changes = {k: v for k, v in changes.items() if k in EDITABLE}
    if not changes:
        raise HTTPException(422, "Nothing to change.")
    before = lead_store.get(user["id"], lead_id[:64])
    if not before:
        raise HTTPException(404, "That lead wasn't found. It may have been deleted.")
    lead = lead_store.update(user["id"], lead_id[:64], changes, listings=listing_store.all(user["id"], True))
    ref = f"lead {lead_id[:8]}"
    if "status" in changes and changes["status"] != before.get("status"):
        activity.add(user["id"], "lead_status", f"{ref}: {before.get('status')} to {changes['status']}",
                     client_ip(request))
    edited = sorted(k for k in changes if k != "status")
    if edited:
        note = f"{ref}: {', '.join(edited)}"
        if lead["score"] != before["score"]:
            note += f" (re-scored {before['score']} to {lead['score']})"
        activity.add(user["id"], "lead_updated", note, client_ip(request))
    return lead


@app.delete("/api/leads/{lead_id}")
def delete_lead(lead_id: str, request: Request, user: dict = Depends(current_user)):
    if not lead_store.delete(user["id"], lead_id[:64]):
        raise HTTPException(404, "That lead wasn't found. It may have been deleted.")
    activity.add(user["id"], "lead_deleted", f"lead {lead_id[:8]}", client_ip(request))
    return {"ok": True}


@app.delete("/api/leads")
def clear_leads(request: Request, user: dict = Depends(current_user)):
    n = lead_store.clear(user["id"])
    activity.add(user["id"], "leads_cleared", f"{n} leads", client_ip(request))
    return {"ok": True, "deleted": n}


# ---------- Listings ----------

class ListingIn(BaseModel):
    title: str = Field(default="", max_length=120)
    location: str = Field(default="", max_length=80)
    deal: str = Field(default="sale", max_length=10)
    price: float | str = 0
    currency: str = Field(default="INR", max_length=3)
    property_type: str | None = Field(default=None, max_length=20)
    bedrooms: int | None = Field(default=None, ge=0, le=20)
    status: str = Field(default="available", max_length=20)
    url: str | None = Field(default=None, max_length=300)


def _clean_listing_or_422(body):
    try:
        return clean_listing(body.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


@app.get("/api/listings")
def list_listings(user: dict = Depends(current_user)):
    return {"items": listing_store.all(user["id"])}


@app.post("/api/listings", status_code=201)
def add_listing(body: ListingIn, request: Request, user: dict = Depends(current_user)):
    rate_limit(request, "edit", 60)
    item = _clean_listing_or_422(body)
    lid = listing_store.add_many(user["id"], [item])[0]
    activity.add(user["id"], "listing_added", item["title"][:60], client_ip(request))
    return listing_store.get(user["id"], lid)


@app.post("/api/listings/import")
async def import_listings(request: Request, file: UploadFile = File(...), user: dict = Depends(current_user)):
    rate_limit(request, "batch", config.RATE_BATCH)
    data = await _read_upload(file)
    df, _bad = analysis._table_from_upload(data, file.filename or "")
    items, errors = read_listings_csv(df.to_csv(index=False))
    listing_store.add_many(user["id"], items)
    activity.add(user["id"], "listings_imported", f"{len(items)} listings", client_ip(request))
    return {"added": len(items), "errors": errors[:20], "items": listing_store.all(user["id"])}


@app.post("/api/listings/sample")
def sample_listings(request: Request, user: dict = Depends(current_user)):
    rate_limit(request, "batch", config.RATE_BATCH)
    items, _ = read_listings_csv(SAMPLE_LISTINGS.read_text(encoding="utf-8"))
    listing_store.add_many(user["id"], items)
    activity.add(user["id"], "listings_imported", f"{len(items)} example listings", client_ip(request))
    return {"added": len(items), "items": listing_store.all(user["id"])}


@app.patch("/api/listings/{listing_id}")
def edit_listing(listing_id: str, body: ListingIn, request: Request, user: dict = Depends(current_user)):
    rate_limit(request, "edit", 60)
    item = _clean_listing_or_422(body)
    if not listing_store.update(user["id"], listing_id[:64], item):
        raise HTTPException(404, "That listing wasn't found.")
    activity.add(user["id"], "listing_updated", item["title"][:60], client_ip(request))
    return listing_store.get(user["id"], listing_id[:64])


@app.delete("/api/listings/{listing_id}")
def delete_listing(listing_id: str, request: Request, user: dict = Depends(current_user)):
    if not listing_store.delete(user["id"], listing_id[:64]):
        raise HTTPException(404, "That listing wasn't found.")
    activity.add(user["id"], "listing_deleted", f"listing {listing_id[:8]}", client_ip(request))
    return {"ok": True}


# ---------- Activity and admin ----------

def admin_user(user: dict = Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(403, "Only admins can see this.")
    return user


@app.get("/api/activity")
def my_activity(limit: int = 100, user: dict = Depends(current_user)):
    return {"items": activity.for_user(user["id"], limit)}


@app.get("/api/admin/users")
def admin_users(_: dict = Depends(admin_user)):
    return {"items": accounts.list_users()}


@app.get("/api/admin/activity")
def admin_activity(limit: int = 200, _: dict = Depends(admin_user)):
    return {"items": activity.all(limit)}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
