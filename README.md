# LeadScout

**Find your hottest leads. Close your next deal.**

LeadScout is an AI lead qualifier for real estate agents. Paste a property inquiry (or upload a CSV of up to 50) and LeadScout pulls out the facts, scores the lead from 0 to 100, labels it Hot, Warm or Cold, shows exactly where every point came from, recommends the next step and drafts the first SMS reply. Every lead is saved privately to the agent's account.

> The AI reads. The rules score. The agent decides.

![CI](https://github.com/Maggiee18/leadscout/actions/workflows/ci.yml/badge.svg)

## The problem

Agents get a steady stream of inquiries from buyers, sellers, renters and investors. Many aren't serious, the good ones get buried, and slow replies lose deals. Reading and judging every message by hand takes time agents don't have.

## Features

- **3D landing page** at `/`: a poster style homepage where a studio-lit "sorting machine" (built with Three.js) shows the product: inquiries drop into a funnel, pass the glowing scoring core and roll into Hot, Warm and Cold bowls. Drag to turn it, pause it, or highlight a tier. Falls back to an illustration without WebGL and respects reduced motion. The app itself lives at `/app`.
- **Single lead analysis**: score ring, Hot/Warm/Cold badge, extracted facts ("Not provided" when missing, never guessed), a point by point breakdown, a recommended next action and a copyable SMS draft.
- **Bulk CSV analysis**: drag and drop up to 50 leads, then search, filter, sort, open any lead and download the scored CSV.
- **Lead history**: every analysis is saved to your account, with search, tier filter, sorting, pagination, CSV export, and delete one or all.
- **Dashboard**: totals, Hot/Warm/Cold split, average score and recent activity, calculated on the server from your saved leads.
- **Accounts**: registration, login, "keep me logged in", forgot/reset password by email, change name, change password (signs out other devices), delete account with all data.
- **Privacy policy and terms** pages, linked from signup.
- **Honest AI status**: every result says whether the AI or offline rules read it, and why it fell back.
- **Works without AI**: no key, an outage or a rate limit switches to regex extraction and template replies. Scoring is identical.
- Responsive from 320px phones to 1920px monitors, keyboard accessible, WCAG AA colour contrast, respects reduced motion.

## Architecture

```
Browser  static/landing.* + landing3d.js              3D landing page at /   (Three.js, bundled locally)
         static/index.html · styles.css · app.js      the app at /app, vanilla JS, strict CSP
   │  JSON / multipart, HttpOnly session cookie
   ▼
FastAPI  main.py      routes, sessions, validation, rate limits, security headers, JSON logs
   ├─ analysis.py     1. extract  (Groq LLM, temperature 0 → fallback: regex rules)
   │  scorer.py       2. score    (plain Python rules, deterministic)
   │                  3. advise   (tier + most useful missing fact)
   │                  4. reply    (AI SMS → fallback: templates)
   ├─ auth.py         users, scrypt hashes, sessions, password reset tokens
   ├─ leads.py        saved lead history, scoped to the signed-in user
   ├─ mailer.py       SMTP email (dev: prints to the log)
   ├─ config.py       every setting, from environment variables
   └─ db.py           SQLite or PostgreSQL, versioned migrations
```

The browser never computes a score and never sees the API key. The backend is the single source of truth.

### AI extraction

The model returns JSON with `intent`, `timeline_months`, `budget`, `currency`, `location`, `financing`, `motivation`, `red_flags` and `summary`. The backend strips code fences and chatter, fixes wrong types (`"450,000"` → `450000`, `"Pre-Approved"` → `preapproved`), fills missing keys, drops unknown keys and retries once if the JSON can't be parsed. Rupees (lakh, crore, ₹, Rs) and dollars are both understood. Missing information is never reported as a red flag, so a lead isn't penalised twice for it.

**Model:** Groq retired Llama 3.3 70B, so the default is `openai/gpt-oss-120b`, the strongest model on Groq's free tier (verified end to end). `qwen/qwen3.8-27b` is faster and also works well. Change it with `LLM_MODEL`.

### Deterministic scoring

| Factor | Points |
|---|---:|
| Moving within one month | +30 |
| Moving within three months | +25 |
| Moving within six months | +15 |
| Moving later, or no timeline given | +5 |
| Cash or pre-approved financing | +25 |
| Needs a mortgage | +10 |
| Financing unknown | 0 |
| Budget stated | +15 |
| Location stated | +10 |
| Clear buy, sell or invest intent | +10 |
| Renting intent | +3 |
| Motivation given | +10 |
| Each red flag | −10 |

The total is kept between 0 and 100. **Hot** is 70+, **Warm** 40–69, **Cold** below 40. The same facts always give the same score.

### Groq free tier limits

Groq's free tier allows about **8,000 tokens per minute**, and each lead uses roughly 600 to 1,500. LeadScout is built around that:

- `max_tokens` is capped so Groq reserves less quota per call, and short "retry after" waits are honoured.
- Batches read leads with the AI for up to `BATCH_AI_SECONDS` (60s), draft batch replies from templates (`BATCH_AI_SMS=false`), and pause the AI after two failures. The remaining leads use offline rules, each labelled with the reason.
- In testing, a 50 lead CSV finished in about 30 seconds: 21 leads by AI, 29 by offline rules once the minute's quota ran out. On Groq's paid Dev tier, raise `BATCH_AI_SECONDS` and every lead gets the AI.

## API

All errors are JSON: `{"detail": "readable message"}`, plus `"field"` for form errors. Interactive docs: `/api/docs`.

| Method | Path | Auth | Description |
|---|---|:-:|---|
| GET | `/` | | 3D landing page |
| GET | `/app` | | The web app |
| GET | `/healthz` | | Liveness |
| GET | `/readyz` | | Readiness (checks the database) |
| GET | `/api/status` | | AI configured or fallback, model, last AI error, limits |
| GET | `/api/samples` | | The six sample leads |
| POST | `/api/auth/register` | | `name`, `email`, `password` → signs in |
| POST | `/api/auth/login` | | `email`, `password`, `remember` |
| POST | `/api/auth/logout` | | Ends the session on the server |
| GET | `/api/auth/me` | ✓ | The signed-in user |
| GET | `/api/auth/session` | | The signed-in user or `null` (always 200, used by the landing page) |
| POST | `/api/auth/forgot` | | `email` → emails a 30 minute, single use link (same reply for unknown emails) |
| POST | `/api/auth/reset` | | `token`, `password` → signs out every device, then signs in |
| PATCH | `/api/account` | ✓ | `name` |
| POST | `/api/account/password` | ✓ | `current_password`, `new_password` → ends other sessions |
| POST | `/api/account/delete` | ✓ | `password` → deletes the account and all its data |
| POST | `/api/score` | ✓ | `name?`, `message`, `use_ai?` → scored lead, saved |
| POST | `/api/batch` | ✓ | multipart `file` (CSV, `message` + optional `name`, ≤50 rows, ≤512 KB) |
| POST | `/api/batch/sample` | ✓ | Scores `sample_leads.csv` |
| GET | `/api/leads` | ✓ | `q`, `tier`, `sort` (`newest`, `oldest`, `score-desc`, `score-asc`), `limit`, `offset` |
| GET | `/api/leads/stats` | ✓ | Dashboard numbers and the 6 most recent leads |
| GET | `/api/leads/{id}` | ✓ | One saved lead |
| DELETE | `/api/leads/{id}` | ✓ | Delete one |
| DELETE | `/api/leads` | ✓ | Delete all |

Example `POST /api/score` response (trimmed):

```json
{
  "score": 85, "tier": "Hot", "raw_total": 85,
  "display": { "intent": "Buy", "timeline": "Within 2 months", "budget": "₹80 lakh",
               "location": "Vijayawada", "financing": "Pre-approved", "motivation": null },
  "breakdown": [ { "factor": "Timeline", "reason": "Moving within 3 months", "points": 25 } ],
  "next_action": "Contact this lead today and arrange a property consultation or viewing.",
  "sms": "Hi Ravi, ...",
  "engine": "ai", "sms_engine": "ai", "model": "openai/gpt-oss-120b", "fallback_reason": null
}
```

## Run locally

```bash
git clone https://github.com/Maggiee18/leadscout.git
cd leadscout
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env        # macOS/Linux: cp .env.example .env
# add GROQ_API_KEY=... to .env (or leave it empty for offline mode)
uvicorn main:app --reload
```

Open http://localhost:8000 for the landing page, or http://localhost:8000/app to sign up. Python 3.10 or newer.

### Rebuilding the 3D scene (only if you change it)

The built scene (`static/landing3d.js`) is committed, so Node isn't needed to run LeadScout. To edit the scene, change `frontend/landing3d.js`, then:

```bash
cd frontend
npm install
npm run build      # bundles only the Three.js parts used into ../static/landing3d.js
```

**Password reset locally:** without SMTP settings, the reset email (with its link) is printed in the terminal running uvicorn.

**Docker** (app + PostgreSQL): `docker compose up --build`, then open http://localhost:8000.

## Environment variables

See `.env.example` for every setting with comments. The important ones:

| Name | Default | Purpose |
|---|---|---|
| `APP_ENV` | `development` | `production` turns on JSON logs, HSTS, Secure cookies and disables dev mail |
| `APP_URL` | empty | Public URL, used in reset emails. Required in production for password reset |
| `GROQ_API_KEY` | empty | Groq key. Empty means offline rules mode |
| `LLM_MODEL` | `openai/gpt-oss-120b` | Any model on your Groq account |
| `DATABASE_URL` | empty | PostgreSQL URL. Empty means SQLite at `DB_PATH` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_FROM` | empty | Email for password reset (Gmail works with an App Password) |
| `BATCH_AI_SECONDS` | `60` | AI time budget per CSV batch |
| `RATE_LIMIT_*_PER_MIN` | 30 / 6 / 10 | Per IP limits for scoring, batches and auth |

## Tests and quality checks

```bash
pip install -r requirements-dev.txt
ruff check .
pytest -q                                   # 75 tests on SQLite
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/leadscout_test pytest -q   # same suite on Postgres
python scripts/smoke_test.py                # end to end against a running server, real AI included
python scripts/smoke_test.py https://your-app.onrender.com
```

The tests cover the scoring table and thresholds, JSON cleaning, the regex fallback, the AI path with a fake client (fenced JSON, retry, timeouts, missing key), validation, CSV limits, rate limits, accounts, sessions, password reset (single use, expiry, voiding, per hour cap), settings, account deletion, history privacy between users, search, pagination, migrations (including upgrading a database from the previous release), cache headers, HSTS and that lead text never reaches the logs.

GitHub Actions (`.github/workflows/ci.yml`) runs lint, tests on Python 3.10 and 3.13, tests on PostgreSQL 16, builds the Docker image and runs the smoke test against the container.

## Deploy on Render (free)

1. Push this repo to GitHub.
2. In Render choose **New → Blueprint** and pick the repo. `render.yaml` creates the web service in production mode.
3. When asked, paste `GROQ_API_KEY` and set `APP_URL` to your Render URL (for example `https://leadscout.onrender.com`). For password reset emails, also fill the `SMTP_*` and `MAIL_FROM` values.
4. Open the site. `/readyz` is the health check; Render logs are JSON with a request id per line.

**Keep data across redeploys:** the free web service disk is wiped on every deploy or restart, so SQLite accounts and history reset there. Uncomment the `databases` block and `DATABASE_URL` in `render.yaml` to use Render Postgres (free for 30 days, then a paid plan).

## Security and privacy

- Passwords are hashed with scrypt and a unique salt. Login errors and timing don't reveal which emails have accounts.
- Sessions are random 256 bit tokens in `HttpOnly`, `SameSite=Lax` cookies (`Secure` in production). Only a SHA-256 of each token is stored. Logging out, changing or resetting a password revokes sessions on the server.
- Reset links expire after 30 minutes, work once, void older links, are capped at 3 per hour, and live in the URL fragment so they never reach server logs.
- Every lead query is scoped to the signed-in user, so one user can never read or delete another's data.
- Strict Content Security Policy (Three.js is served from the app itself, not a CDN), gzip compression, HSTS in production, cross-site POSTs rejected, per IP rate limits, CSV upload validation, CSV export protected against formula injection.
- Lead text is never written to the logs. The AI key stays on the server.

## Limitations

- Groq's free tier caps how many leads per minute the AI can read (see above).
- Rate limiting is in memory: fine for one instance, and it resets on restart.
- There's no email verification at signup, and email addresses can't be changed.
- The offline regex rules are simpler than the AI and can miss unusual phrasing.

## Future improvements

- Background batch jobs with a progress bar, so large CSVs wait out the AI rate limit instead of falling back
- Email verification and changing the account email
- CRM integrations (HubSpot, Zoho) and sending SMS through Twilio after the agent approves
- Configurable scoring weights per agency, team workspaces with roles
