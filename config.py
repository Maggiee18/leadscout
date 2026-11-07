"""All runtime settings, read once from environment variables (and .env for local runs)."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv():
    """Tiny .env loader for local runs (no extra dependency). Real env vars win."""
    env = BASE_DIR / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _int(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


APP_ENV = os.getenv("APP_ENV", "development").lower()
IS_PROD = APP_ENV == "production"
APP_URL = os.getenv("APP_URL", "").rstrip("/")          # public URL, used in emails
LOG_FORMAT = os.getenv("LOG_FORMAT", "json" if IS_PROD else "text")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# AI
API_KEY = os.getenv("GROQ_API_KEY") or os.getenv("LLM_API_KEY") or ""
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
# Llama 3.3 70B was retired from Groq; gpt-oss-120b is the strongest model on the free tier.
MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "20"))
BATCH_AI_SECONDS = float(os.getenv("BATCH_AI_SECONDS", "60"))   # AI time budget per CSV batch
BATCH_AI_SMS = os.getenv("BATCH_AI_SMS", "false").lower() == "true"  # AI replies in batches use quota

# Database
DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_PATH = os.getenv("DB_PATH") or str(BASE_DIR / "leadscout.db")

# Cookies
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true" if IS_PROD else "auto").lower()

# Limits (per IP per minute)
RATE_SCORE = _int("RATE_LIMIT_SCORE_PER_MIN", 30)
RATE_BATCH = _int("RATE_LIMIT_BATCH_PER_MIN", 6)
RATE_AUTH = _int("RATE_LIMIT_AUTH_PER_MIN", 10)

# Comma-separated emails that get the admin role (can see all users and the full activity log)
ADMIN_EMAILS = [e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()]

# Email (password reset)
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = _int("SMTP_PORT", 587)
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
MAIL_FROM = os.getenv("MAIL_FROM", SMTP_USER)
