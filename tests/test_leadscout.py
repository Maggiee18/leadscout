"""LeadScout tests: scoring rules, fallback extraction, AI path (faked client), API, accounts.

Runs on a throwaway SQLite file by default. Set TEST_DATABASE_URL to a PostgreSQL URL
to run the same suite against Postgres.
"""
import io
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Configure an isolated database BEFORE the app is imported
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL", "")
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["GROQ_API_KEY"] = ""
os.environ["LLM_API_KEY"] = ""

import analysis  # noqa: E402
import config  # noqa: E402
import mailer  # noqa: E402
import main  # noqa: E402
from auth import hash_password, verify_password  # noqa: E402
from scorer import clean_fields, extract_with_rules, parse_json, score_lead  # noqa: E402

if main.db.is_pg:  # start clean
    for table in ("activity_log", "listings", "leads", "password_resets", "sessions", "users"):
        main.db.execute(f"DELETE FROM {table}")

PASSWORD = "Sunrise2026"
client = TestClient(main.app)  # signed in, used by the scoring tests
assert client.post("/api/auth/register", json={"name": "Test Agent", "email": "agent@example.com",
                                               "password": PASSWORD}).status_code == 201


def anon():
    return TestClient(main.app)


def new_email():
    return f"user{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture(autouse=True)
def reset_rate_limit():
    main._hits.clear()
    yield


def facts(**kw):
    return clean_fields(kw)


# ---------- deterministic scoring ----------

def test_hot_buyer_scores_100():
    s, t, b = score_lead(facts(intent="buy", timeline_months=1, budget=550000, currency="USD",
                               location="Austin", financing="preapproved", motivation="relocation"))
    assert (s, t) == (100, "Hot")
    assert sum(p for *_, p in b) == 100


@pytest.mark.parametrize("months,pts", [(0, 30), (1, 30), (3, 25), (6, 15), (9, 5), (None, 5)])
def test_timeline_points(months, pts):
    _, _, b = score_lead(facts(timeline_months=months))
    assert b[0][2] == pts


@pytest.mark.parametrize("fin,pts", [("cash", 25), ("preapproved", 25), ("mortgage", 10), ("unknown", 0)])
def test_financing_points(fin, pts):
    _, _, b = score_lead(facts(financing=fin))
    assert [p for f, _, p in b if f == "Financing"] == [pts]


def test_missing_budget_and_location_give_no_points():
    _, _, b = score_lead(facts(intent="buy"))
    assert not [x for x in b if x[0] in ("Budget", "Location")]


def test_red_flags_deduct_10_each_and_clamp():
    s, t, b = score_lead(facts(red_flags=["just browsing", "unsure", "no budget"]))
    assert [p for f, _, p in b if f == "Red flag"] == [-10, -10, -10]
    assert s == 0 and t == "Cold"


def test_rent_intent_3_points_and_thresholds():
    _, _, b = score_lead(facts(intent="rent"))
    assert ("Intent", "Renting (lower commission)", 3) in b
    assert score_lead(facts(intent="buy", timeline_months=1, financing="cash", location="X"))[1] == "Hot"  # 75
    assert score_lead(facts(intent="buy", timeline_months=3, location="X"))[1] == "Warm"  # 45


def test_same_facts_same_score():
    f = facts(intent="sell", timeline_months=3, location="Tampa", motivation="upsizing")
    assert len({score_lead(dict(f))[0] for _ in range(20)}) == 1


# ---------- JSON cleaning ----------

def test_parse_json_strips_fences_and_chatter():
    assert parse_json('Sure!\n```json\n{"intent": "buy"}\n```')["intent"] == "buy"


def test_clean_fields_fixes_types():
    f = clean_fields({"intent": "Buying", "timeline_months": "3", "budget": "450,000",
                      "financing": "Pre-Approved", "red_flags": "unsure", "location": "", "extra": 1})
    assert f["intent"] == "buy" and f["timeline_months"] == 3 and f["budget"] == 450000
    assert f["financing"] == "preapproved" and f["red_flags"] == ["unsure"]
    assert f["location"] is None and "extra" not in f and f["currency"] == "USD"


# ---------- regex fallback ----------

def test_fallback_inr_lakhs_and_word_timeline():
    f = extract_with_rules("Looking for a 3BHK in Vijayawada around ₹80 lakhs, pre-approved, "
                           "move within the next two months.")
    assert f["budget"] == 8_000_000 and f["currency"] == "INR"
    assert f["timeline_months"] == 2 and f["financing"] == "preapproved"
    assert f["location"] == "Vijayawada" and f["intent"] == "buy"


def test_fallback_rental_budget_is_detected():
    f = extract_with_rules("Need to rent a 2 bed in Denver ASAP, budget $2,400/month")
    assert f["intent"] == "rent" and f["budget"] == 2400 and f["timeline_months"] == 0


def test_fallback_seller_and_vague():
    seller = extract_with_rules("Thinking about selling my condo in Tampa in about 3 months, upsizing")
    assert seller["intent"] == "sell" and seller["motivation"]
    vague = extract_with_rules("just looking at what's out there, maybe buy someday, not sure")
    assert vague["red_flags"] and score_lead(vague)[1] == "Cold"


def test_not_preapproved_is_mortgage():
    assert extract_with_rules("We are not pre-approved yet, need a loan")["financing"] == "mortgage"


# ---------- fake AI client ----------

class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls += 1
        r = self.replies.pop(0) if self.replies else "Hi! Free for a call today?"
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=r))])


AI_JSON = json.dumps({"intent": "buy", "timeline_months": 1, "budget": 550000, "currency": "USD",
                      "location": "Austin", "financing": "preapproved", "motivation": "job relocation",
                      "red_flags": [], "summary": "Pre-approved family relocating to Austin."})


@pytest.fixture
def ai(monkeypatch):
    def install(replies):
        fake = FakeClient(replies)
        monkeypatch.setattr(config, "API_KEY", "test-key")
        monkeypatch.setattr(analysis, "get_client", lambda: fake)
        return fake
    return install


def test_ai_path_used_and_reported(ai):
    fake = ai([f"```json\n{AI_JSON}\n```", "Hi Jess, can we talk today?"])
    r = client.post("/api/score", json={"name": "Jess", "message": "pre-approved 550k Austin next month"}).json()
    assert r["engine"] == "ai" and r["sms_engine"] == "ai" and r["score"] == 100
    assert r["sms"] == "Hi Jess, can we talk today?" and fake.calls == 2


def test_ai_bad_json_retries_once(ai):
    fake = ai(["not json at all", AI_JSON, "sms"])
    r = client.post("/api/score", json={"message": "pre-approved 550k Austin next month"}).json()
    assert r["engine"] == "ai" and fake.calls == 3


def test_ai_failure_falls_back_honestly(ai):
    class APITimeoutError(Exception):
        pass
    ai([APITimeoutError("boom")])
    r = client.post("/api/score", json={"message": "Need to rent a 2 bed in Denver ASAP, budget $2,400/month"}).json()
    assert r["engine"] == "fallback" and r["fallback_reason"] == "The AI service timed out"
    assert r["sms_engine"] == "template" and r["score"] == 58
    assert client.get("/api/status").json()["last_ai_error"] == "The AI service timed out"


def test_missing_key_uses_fallback(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "")
    r = client.post("/api/score", json={"message": "Cash buyer, duplex in Phoenix, close in 2 weeks, $700k"}).json()
    assert r["engine"] == "fallback" and r["fallback_reason"] == "No AI API key configured"
    assert client.get("/api/status").json()["mode"] == "fallback"


# ---------- API validation ----------

@pytest.mark.parametrize("body", [{}, {"message": ""}, {"message": "   hi   "}, {"message": "x" * 2001}])
def test_score_rejects_bad_input(body):
    r = client.post("/api/score", json=body)
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)


def test_score_rejects_invalid_json():
    r = client.post("/api/score", content="{bad", headers={"content-type": "application/json"})
    assert r.status_code == 422


def upload(content, name="leads.csv"):
    return client.post("/api/batch", files={"file": (name, io.BytesIO(content), "text/csv")})


def test_batch_sorted_with_summary():
    r = client.post("/api/batch/sample").json()
    scores = [x["score"] for x in r["results"]]
    assert r["count"] == 6 and scores == sorted(scores, reverse=True)
    s = r["summary"]
    assert s["hot"] + s["warm"] + s["cold"] == 6
    assert s["average_score"] == round(sum(scores) / 6, 1)


def test_batch_50_ok_51_rejected():
    rows = "name,message\n" + "".join(f"L{i},Need to rent in Denver ASAP for $2000\n" for i in range(50))
    assert upload(rows.encode()).json()["count"] == 50
    r = upload((rows + "L50,one more lead here\n").encode())
    assert r.status_code == 400 and "limit is 50" in r.json()["detail"]


@pytest.mark.parametrize("content,name,msg", [
    (b"", "a.csv", "empty"),
    (b"foo,bar\n1,2\n", "a.csv", "'message'"),
    (b"name,message\nA,\nB,  \n", "a.csv", "No leads"),
    (b"name,message\nA,hello there", "a.pdf", ".csv"),
    (b"\x00\x01\x02binary", "a.csv", "CSV"),
])
def test_batch_invalid_files(content, name, msg):
    r = upload(content, name)
    assert r.status_code == 400 and msg in r.json()["detail"]


def test_batch_too_large():
    r = upload(b"name,message\n" + b"a,b" * 200_000)
    assert r.status_code == 413


def test_message_column_only_and_case_insensitive():
    r = upload(b'MESSAGE\n"Need to rent a 2 bed in Denver ASAP, budget $2,400/month"\n').json()
    assert r["count"] == 1 and r["results"][0]["name"] == "Lead 1"


def test_security_headers_and_index():
    r = client.get("/")
    assert r.status_code == 200 and "LeadScout" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "api_key" not in client.get("/static/app.js").text.lower()


def test_rate_limit_returns_429():
    codes = [client.post("/api/batch/sample").status_code for _ in range(7)]
    assert codes[:6] == [200] * 6 and codes[6] == 429


# ---------- accounts ----------

def test_scoring_requires_login():
    c = anon()
    assert c.post("/api/score", json={"message": "Need to rent in Denver ASAP"}).status_code == 401
    assert c.post("/api/batch/sample").status_code == 401
    r = c.post("/api/batch", files={"file": ("a.csv", io.BytesIO(b"message\nhello there"), "text/csv")})
    assert r.status_code == 401
    assert c.get("/api/auth/me").status_code == 401
    assert c.get("/api/status").status_code == 200  # public


def test_register_logs_in_with_secure_cookie_flags():
    c = anon()
    r = c.post("/api/auth/register", json={"name": "  Maggie   A ", "email": "  New@Example.COM ",
                                           "password": PASSWORD})
    assert r.status_code == 201
    assert r.json()["user"]["email"] == "new@example.com" and r.json()["user"]["name"] == "Maggie A"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=604800" in cookie
    assert c.get("/api/auth/me").json()["user"]["email"] == "new@example.com"
    assert c.post("/api/score", json={"message": "Need to rent a 2 bed in Denver ASAP"}).status_code == 200


def test_duplicate_email_rejected():
    r = anon().post("/api/auth/register", json={"name": "Someone", "email": "AGENT@example.com",
                                                "password": PASSWORD})
    assert r.status_code == 409 and r.json()["field"] == "email"


@pytest.mark.parametrize("body,field", [
    ({"name": "A", "email": "x@example.com", "password": "Sunrise2026"}, "name"),
    ({"name": "Al Smith", "email": "not-an-email", "password": "Sunrise2026"}, "email"),
    ({"name": "Al Smith", "email": "a@example.com", "password": "short1"}, "password"),
    ({"name": "Al Smith", "email": "a@example.com", "password": "onlyletters"}, "password"),
    ({"name": "Al Smith", "email": "a@example.com", "password": "password123"}, "password"),
    ({"name": "<script>", "email": "a@example.com", "password": "Sunrise2026"}, "name"),
])
def test_register_validation(body, field):
    r = anon().post("/api/auth/register", json=body)
    assert r.status_code == 400 and r.json()["field"] == field


def test_login_wrong_password_and_unknown_email_look_identical():
    a = anon().post("/api/auth/login", json={"email": "agent@example.com", "password": "Wrong12345"})
    b = anon().post("/api/auth/login", json={"email": "nobody@example.com", "password": "Wrong12345"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_login_remember_me_and_logout_revokes_session():
    c = anon()
    r = c.post("/api/auth/login", json={"email": "Agent@Example.com", "password": PASSWORD, "remember": True})
    assert r.status_code == 200 and "max-age=2592000" in r.headers["set-cookie"].lower()
    token = c.cookies.get("ls_session")
    assert c.post("/api/auth/logout").json() == {"ok": True}
    stolen = anon()
    stolen.cookies.set("ls_session", token)
    assert stolen.get("/api/auth/me").status_code == 401  # token no longer valid server side


def test_passwords_and_tokens_are_not_stored_in_plain_text():
    c = anon()
    c.post("/api/auth/login", json={"email": "agent@example.com", "password": PASSWORD})
    token = c.cookies.get("ls_session")
    db = main.db
    row = db.fetchone("SELECT password_hash FROM users WHERE email = ?", ("agent@example.com",))
    assert PASSWORD not in row[0] and row[0].startswith("scrypt$")
    assert not db.fetchone("SELECT 1 FROM sessions WHERE token_hash = ?", (token,))
    assert verify_password(PASSWORD, hash_password(PASSWORD)) and not verify_password("x", row[0])


def test_expired_session_rejected():
    c = anon()
    c.post("/api/auth/login", json={"email": "agent@example.com", "password": PASSWORD})
    main.db.execute("UPDATE sessions SET expires_at = 1")
    assert c.get("/api/auth/me").status_code == 401
    client.post("/api/auth/login", json={"email": "agent@example.com", "password": PASSWORD})


def test_cross_site_post_blocked():
    r = anon().post("/api/auth/login", json={"email": "agent@example.com", "password": PASSWORD},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    ok = anon().post("/api/auth/login", json={"email": "agent@example.com", "password": PASSWORD},
                     headers={"Origin": "http://testserver"})
    assert ok.status_code == 200


def test_login_rate_limited():
    c = anon()
    codes = [c.post("/api/auth/login", json={"email": "nobody@example.com", "password": "Nope12345"}).status_code
             for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429


def test_missing_fields_message():
    r = anon().post("/api/auth/login", json={"email": 5})
    assert r.status_code in (400, 401, 422) and isinstance(r.json()["detail"], str)


# ---------- saved lead history ----------

def signed_in(name="History User"):
    c = anon()
    email = new_email()
    assert c.post("/api/auth/register", json={"name": name, "email": email, "password": PASSWORD}).status_code == 201
    return c


def test_scoring_saves_to_history_with_stats():
    c = signed_in()
    r = c.post("/api/score", json={"name": "Priya", "message": "Need to rent a 2 bed in Denver ASAP, budget $2,400/month"}).json()
    c.post("/api/batch/sample")
    page = c.get("/api/leads?limit=100").json()
    assert page["total"] == 7 and any(x["id"] == r["id"] and x["source"] == "single" for x in page["items"])
    st = c.get("/api/leads/stats").json()
    assert st["total"] == 7 and st["hot"] + st["warm"] + st["cold"] == 7
    scores = [x["score"] for x in page["items"]]
    assert st["average_score"] == round(sum(scores) / 7, 1) and len(st["recent"]) == 6
    assert c.get(f"/api/leads/{r['id']}").json()["message"].startswith("Need to rent")


def test_history_search_filter_sort_paginate():
    c = signed_in()
    c.post("/api/batch/sample")
    assert c.get("/api/leads?q=denver").json()["total"] == 1
    assert c.get("/api/leads?q=100%25").json()["total"] == 0  # LIKE wildcards are escaped
    hot = c.get("/api/leads?tier=Hot").json()
    assert hot["total"] == 2 and {x["tier"] for x in hot["items"]} == {"Hot"}
    by_score = [x["score"] for x in c.get("/api/leads?sort=score-asc").json()["items"]]
    assert by_score == sorted(by_score)
    p1 = c.get("/api/leads?limit=4&offset=0").json()["items"]
    p2 = c.get("/api/leads?limit=4&offset=4").json()["items"]
    assert len(p1) == 4 and len(p2) == 2 and not {x["id"] for x in p1} & {x["id"] for x in p2}


def test_history_is_private_to_each_user():
    a, b = signed_in("Owner A"), signed_in("Other B")
    lead = a.post("/api/score", json={"message": "Cash buyer, duplex in Phoenix, close in 2 weeks, $700k"}).json()
    assert b.get(f"/api/leads/{lead['id']}").status_code == 404
    assert b.delete(f"/api/leads/{lead['id']}").status_code == 404
    assert b.get("/api/leads").json()["total"] == 0
    b.delete("/api/leads")
    assert a.get(f"/api/leads/{lead['id']}").status_code == 200
    assert anon().get("/api/leads").status_code == 401


def test_delete_one_and_clear_all():
    c = signed_in()
    c.post("/api/batch/sample")
    first = c.get("/api/leads").json()["items"][0]["id"]
    assert c.delete(f"/api/leads/{first}").json() == {"ok": True}
    assert c.get(f"/api/leads/{first}").status_code == 404
    assert c.delete("/api/leads").json()["deleted"] == 5
    stats = c.get("/api/leads/stats").json()
    assert {k: stats[k] for k in ("total", "hot", "warm", "cold", "average_score", "recent")} == {
        "total": 0, "hot": 0, "warm": 0, "cold": 0, "average_score": None, "recent": []}


# ---------- password reset ----------

@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, text, html=None: sent.append((to, text)) or True)
    return sent


def _token(text):
    import re
    return re.search(r"#/reset\?token=([A-Za-z0-9_\-]+)", text).group(1)


def test_forgot_same_reply_for_unknown_email(outbox):
    a = anon().post("/api/auth/forgot", json={"email": "agent@example.com"})
    b = anon().post("/api/auth/forgot", json={"email": "ghost@example.com"})
    assert a.status_code == b.status_code == 200 and a.json() == b.json()
    assert len(outbox) == 1 and outbox[0][0] == "agent@example.com"


def test_reset_flow_single_use_and_signs_out_everywhere(outbox):
    email = new_email()
    old = anon()
    old.post("/api/auth/register", json={"name": "Reset Me", "email": email, "password": PASSWORD})
    anon().post("/api/auth/forgot", json={"email": email})
    token = _token(outbox[-1][1])
    weak = anon().post("/api/auth/reset", json={"token": token, "password": "short"})
    assert weak.status_code == 400 and weak.json()["field"] == "password"
    c = anon()
    r = c.post("/api/auth/reset", json={"token": token, "password": "NewSunrise2027"})
    assert r.status_code == 200 and c.get("/api/auth/me").status_code == 200  # signed in
    assert old.get("/api/auth/me").status_code == 401  # old session revoked
    again = anon().post("/api/auth/reset", json={"token": token, "password": "Another2028x"})
    assert again.status_code == 400 and again.json()["field"] == "token"
    assert anon().post("/api/auth/login", json={"email": email, "password": "NewSunrise2027"}).status_code == 200
    assert anon().post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 401


def test_new_reset_link_voids_old_one_and_expiry(outbox):
    email = new_email()
    anon().post("/api/auth/register", json={"name": "Two Links", "email": email, "password": PASSWORD})
    anon().post("/api/auth/forgot", json={"email": email})
    first = _token(outbox[-1][1])
    anon().post("/api/auth/forgot", json={"email": email})
    second = _token(outbox[-1][1])
    assert anon().post("/api/auth/reset", json={"token": first, "password": "Fresh2026ab"}).status_code == 400
    main.db.execute("UPDATE password_resets SET expires_at = 1")
    assert anon().post("/api/auth/reset", json={"token": second, "password": "Fresh2026ab"}).status_code == 400


def test_reset_requests_capped_per_hour(outbox):
    email = new_email()
    anon().post("/api/auth/register", json={"name": "Spam Check", "email": email, "password": PASSWORD})
    for _ in range(5):
        main._hits.clear()
        anon().post("/api/auth/forgot", json={"email": email})
    assert len([o for o in outbox if o[0] == email]) == 3


def test_dev_mail_logs_link_when_smtp_missing(caplog, monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "")
    with caplog.at_level("WARNING"):
        assert mailer.send("x@example.com", "Subject", "link here") is False
    assert "DEV MAIL" in caplog.text
    monkeypatch.setattr(config, "IS_PROD", True)
    caplog.clear()
    with caplog.at_level("WARNING"):
        mailer.send("x@example.com", "Subject", "secret link")
    assert "secret link" not in caplog.text


# ---------- account settings ----------

def test_change_name():
    c = signed_in()
    r = c.patch("/api/account", json={"name": "  New   Name "})
    assert r.json()["user"]["name"] == "New Name" and c.get("/api/auth/me").json()["user"]["name"] == "New Name"
    assert c.patch("/api/account", json={"name": "x"}).status_code == 400


def test_change_password_keeps_this_session_ends_others():
    email = new_email()
    here, other = anon(), anon()
    here.post("/api/auth/register", json={"name": "Two Devices", "email": email, "password": PASSWORD})
    other.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    bad = here.post("/api/account/password", json={"current_password": "wrong", "new_password": "Better2027x"})
    assert bad.status_code == 400 and bad.json()["field"] == "current_password"
    same = here.post("/api/account/password", json={"current_password": PASSWORD, "new_password": PASSWORD})
    assert same.status_code == 400 and same.json()["field"] == "new_password"
    ok = here.post("/api/account/password", json={"current_password": PASSWORD, "new_password": "Better2027x"})
    assert ok.json() == {"ok": True, "other_sessions_ended": 1}
    assert here.get("/api/auth/me").status_code == 200 and other.get("/api/auth/me").status_code == 401


def test_delete_account_removes_everything():
    email = new_email()
    c = anon()
    uid = c.post("/api/auth/register", json={"name": "Leaving Soon", "email": email, "password": PASSWORD}).json()["user"]["id"]
    c.post("/api/batch/sample")
    assert c.post("/api/account/delete", json={"password": "wrong"}).status_code == 400
    assert c.post("/api/account/delete", json={"password": PASSWORD}).json() == {"ok": True}
    for table in ("users WHERE id", "leads WHERE user_id", "sessions WHERE user_id"):
        assert main.db.fetchone(f"SELECT COUNT(*) FROM {table} = ?", (uid,))[0] == 0
    assert anon().post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 401


# ---------- infrastructure ----------

def test_migrations_are_idempotent():
    assert main.db.migrate() == 4 and main.db.migrate() == 4


def test_health_ready_and_pages():
    assert client.get("/healthz").json()["ok"] is True
    assert client.get("/readyz").json() == {"ok": True, "database": main.db.kind}
    for path in ("/privacy", "/terms"):
        r = client.get(path)
        assert r.status_code == 200 and "LeadScout" in r.text
    assert "Disallow: /api/" in client.get("/robots.txt").text


def test_versioned_assets_are_cached_and_request_ids():
    html = client.get("/app").text
    assert f"/static/app.js?v={main.ASSET_VERSION}" in html
    landing = client.get("/").text
    assert f"/static/landing.js?v={main.ASSET_VERSION}" in landing and "LeadScout" in landing
    assert "immutable" in client.get(f"/static/app.js?v={main.ASSET_VERSION}").headers["cache-control"]
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"
    r = client.get("/api/status", headers={"X-Request-ID": "abc12345-test"})
    assert r.headers["x-request-id"] == "abc12345-test"
    assert len(client.get("/api/status", headers={"X-Request-ID": "bad id!"}).headers["x-request-id"]) == 16


def test_hsts_only_in_production(monkeypatch):
    assert "strict-transport-security" not in client.get("/healthz").headers
    monkeypatch.setattr(config, "IS_PROD", True)
    assert "max-age" in client.get("/healthz").headers["strict-transport-security"]


def test_logs_never_contain_lead_text(caplog):
    with caplog.at_level("INFO"):
        client.post("/api/score", json={"message": "Secret Banana Street inquiry, cash buyer in Austin"})
    assert "Banana" not in caplog.text


def test_upgrade_from_previous_release_keeps_accounts(tmp_path):
    """A v2.x database (users + sessions, no schema_version table) upgrades in place."""
    import sqlite3

    from db import Database
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
                            password_hash TEXT NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id)
                               ON DELETE CASCADE, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
    """)
    con.execute("INSERT INTO users VALUES ('u1', 'Old User', 'old@example.com', ?, 1)", (hash_password(PASSWORD),))
    con.commit()
    con.close()
    db = Database(path=path)
    assert db.migrate() == 4
    from auth import Accounts
    assert Accounts(db).authenticate("old@example.com", PASSWORD)["name"] == "Old User"
    assert db.fetchone("SELECT COUNT(*) FROM leads")[0] == 0


def test_landing_and_app_routes_and_gzip():
    landing = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert landing.status_code == 200 and 'id="scene"' in landing.text
    assert landing.headers.get("content-encoding") == "gzip"
    app_page = client.get("/app")
    assert app_page.status_code == 200 and 'id="authScreen"' in app_page.text
    bundle = client.get("/static/landing3d.js", headers={"Accept-Encoding": "gzip"})
    assert bundle.status_code == 200 and bundle.headers.get("content-encoding") == "gzip"
    assert "Disallow: /app" in client.get("/robots.txt").text


def test_reset_link_points_to_app(outbox):
    email = new_email()
    anon().post("/api/auth/register", json={"name": "Link Check", "email": email, "password": PASSWORD})
    anon().post("/api/auth/forgot", json={"email": email})
    assert "/app#/reset?token=" in outbox[-1][1]
