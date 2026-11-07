"""Version 4: extraction fixes, flexible uploads, lead pipeline, listings, security and the dashboard API."""
import io
import time

import openpyxl
import pytest
from test_leadscout import PASSWORD, anon, new_email, signed_in  # noqa: F401  (also sets up the test database)

import main
from listings import match_listings
from scorer import confidence, extract_with_rules, find_phone, score_lead

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture(autouse=True)
def reset_rate_limit():
    main._hits.clear()
    yield


def score(c, message, **kw):
    r = c.post("/api/score", json={"message": message, **kw})
    assert r.status_code == 200, r.text
    return r.json()


# ---------- extraction ----------

def test_phone_number_is_never_a_budget():
    f = extract_with_rules("This is Priya, call me at 9876543210. Want a villa in Hyderabad, 1.2 crore, cash, ASAP.")
    assert f["budget"] == 12_000_000 and f["currency"] == "INR"
    assert f["phone"] == "9876543210" and f["customer_name"] == "Priya"
    assert f["property_type"] == "Villa"


def test_phone_only_message_has_no_budget():
    f = extract_with_rules("Interested in a flat in Pune, my number is +91 98765 43210")
    assert f["budget"] is None and f["phone"] == "+919876543210"


@pytest.mark.parametrize("text,phone", [
    ("call (512) 555-0199", "5125550199"), ("price 80,00,000", None), ("budget 8000000 rupees", None),
])
def test_find_phone(text, phone):
    assert find_phone(text) == phone


def test_email_bedrooms_and_sentiment():
    f = extract_with_rules("I'm Sara Lee, really excited! Need a 3BHK in Pune, mail sara@x.com")
    assert (f["email"], f["bedrooms"], f["property_type"], f["sentiment"], f["customer_name"]) == (
        "sara@x.com", 3, "Apartment", "positive", "Sara Lee")


def test_tier_always_matches_score():
    for msg in ["just browsing maybe someday not sure", "cash buyer, 3 bed in Austin, $500k, ASAP, relocating",
                "Selling my house in Tampa in 3 months"]:
        s, tier, _ = score_lead(extract_with_rules(msg))
        assert tier == ("Hot" if s >= 70 else "Warm" if s >= 40 else "Cold")


def test_confidence_reflects_stated_facts():
    full = extract_with_rules("Pre-approved, buying a 3 bed in Austin for $500k within a month")
    empty = extract_with_rules("hello there, call me")
    assert confidence(full, "ai") == 100 and confidence(full, "fallback") == 85
    assert confidence(empty, "ai") == 5


def test_result_has_explanations_contact_and_interest():
    c = signed_in()
    r = score(c, "This is Priya, 9876543210. Cash buyer, villa in Hyderabad, 1.2 crore, ASAP.")
    assert r["name"] == "Priya" and r["phone"] == "9876543210"
    assert r["display"]["budget"] == "₹1.2 crore" and r["display"]["property"] == "Villa"
    assert r["confidence"] and r["interest"] == "High" and r["breakdown"]


# ---------- uploads ----------

def up(c, content, name="leads.csv"):
    return c.post("/api/batch?use_ai=false", files={"file": (name, io.BytesIO(content), "text/csv")})


@pytest.mark.parametrize("content", [
    b'Customer Name,Inquiry\nRavi,"Buying in Pune 50 lakhs asap"\n',
    b"name;message\nRavi;Buying in Pune 50 lakhs asap\n",
    b"name\tmessage\nRavi\tBuying in Pune 50 lakhs asap\n",
    b'name,message\nRavi,"Buying in Pune 50 lakhs asap"\n,,,\nbroken,row,with,extra\n',
])
def test_flexible_csv(content):
    r = up(signed_in(), content)
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["name"] == "Ravi"


def test_phone_and_email_columns_win():
    r = up(signed_in(), b'name,phone,email,message\nRavi,99887 76655,ravi@x.com,"Buying in Pune 50 lakhs asap"\n')
    res = r.json()["results"][0]
    assert res["phone"] == "99887 76655" and res["email"] == "ravi@x.com"


def test_excel_upload():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Name", "Message"])
    ws.append(["Meera", "Need a 2 bed flat in Hyderabad around 60 lakhs within 3 months"])
    buf = io.BytesIO()
    wb.save(buf)
    r = up(signed_in(), buf.getvalue(), "leads.xlsx")
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["name"] == "Meera"


def test_mismatched_extension_rejected():
    r = up(signed_in(), b"name,message\nA,hello there", "leads.xlsx")
    assert r.status_code == 400 and "extension" in r.json()["detail"]


# ---------- lead management ----------

def test_edit_rescores_and_logs():
    c = signed_in()
    r = score(c, "Looking to buy a home in Austin, maybe within 6 months")
    before = r["score"]
    e = c.patch(f"/api/leads/{r['id']}", json={"budget": 500000, "currency": "USD", "financing": "cash"},
                headers=ORIGIN).json()
    assert e["score"] == before + 15 + 25 and e["tier"] in ("Warm", "Hot") and e["rescored_at"]
    assert e["display"]["budget"] == "$500,000"
    acts = c.get("/api/activity").json()["items"]
    assert acts[0]["action"] == "lead_updated" and "re-scored" in acts[0]["detail"]


def test_status_pipeline_and_conversion_stats():
    c = signed_in()
    a = score(c, "Cash buyer, 3 bed in Austin, $500k, ASAP")
    score(c, "Renting a 1 bed in Denver next month, $1,800 a month")
    assert c.patch(f"/api/leads/{a['id']}", json={"status": "Converted"}, headers=ORIGIN).json()["status"] == "Converted"
    st = c.get("/api/leads/stats").json()
    assert st["by_status"]["Converted"] == 1 and st["by_status"]["New"] == 1 and st["conversion_rate"] == 50.0
    assert st["monthly"][-1]["leads"] == 2 and st["monthly"][-1]["converted"] == 1
    assert len(st["monthly"]) == 6
    assert c.patch(f"/api/leads/{a['id']}", json={"status": "Won"}, headers=ORIGIN).status_code == 422


def test_search_and_filters():
    c = signed_in()
    score(c, "I'm Anita, buying a 2BHK flat in Hyderabad, 60 lakhs, within 3 months")
    score(c, "I'm Bob, buying a house in Austin for $450,000 within 3 months")
    get = lambda q: c.get("/api/leads?" + q).json()["total"]  # noqa: E731
    assert get("q=anita") == 1 and get("location=austin") == 1
    assert get("budget_min=1000000&currency=INR") == 1 and get("budget_max=500000&currency=USD") == 1
    assert get("property_type=House") == 1 and get("status=New") == 2
    assert c.get("/api/leads?budget_min=abc").status_code == 422


def test_duplicate_inquiries_are_flagged():
    c = signed_in()
    a = score(c, "Call me on 9876543210, need a flat in Pune")
    b = score(c, "Hi again, flat in Pune please. 98765 43210")
    other = score(c, "Totally different lead in Austin")
    assert a["duplicate_of"] is None and b["duplicate_of"] == a["id"] and other["duplicate_of"] is None
    assert c.get("/api/leads?duplicates=true").json()["total"] == 1
    c.delete(f"/api/leads/{a['id']}", headers=ORIGIN)
    assert c.get(f"/api/leads/{b['id']}").json()["duplicate_of"] is None


def test_export_neutralises_formulas():
    c = signed_in()
    r = score(c, "=HYPERLINK(\"http://evil\") buying a flat in Pune", name="=cmd|' /C calc'!A0")
    assert r
    csv = c.get("/api/leads/export.csv").text
    assert "\"'=cmd" in csv and "\"'=HYPERLINK" in csv


# ---------- listings ----------

def test_listings_crud_and_matching_in_score():
    c = signed_in()
    ok = c.post("/api/listings", json={"title": "3BHK in Benz Circle", "location": "Vijayawada", "deal": "sale",
                                       "price": "78,00,000", "currency": "INR", "property_type": "Apartment",
                                       "bedrooms": 3}, headers=ORIGIN)
    assert ok.status_code == 201, ok.text
    bad = c.post("/api/listings", json={"title": "x", "location": "", "price": 0}, headers=ORIGIN)
    assert bad.status_code == 422
    r = score(c, "Looking for a 3BHK apartment in Vijayawada around ₹80 lakhs, pre-approved, within 2 months")
    assert [m["title"] for m in r["matches"]] == ["3BHK in Benz Circle"] and "Within budget" in r["matches"][0]["reasons"]
    assert "1 listing" in r["sms"]
    lid = ok.json()["id"]
    assert c.patch(f"/api/listings/{lid}", json={**ok.json(), "status": "sold"}, headers=ORIGIN).json()["status"] == "sold"
    assert score(c, "Looking for a 3BHK apartment in Vijayawada around ₹80 lakhs")["matches"] == []
    assert c.delete(f"/api/listings/{lid}", headers=ORIGIN).json() == {"ok": True}


def test_sample_and_imported_listings():
    c = signed_in()
    assert c.post("/api/listings/sample", headers=ORIGIN).json()["added"] == 9
    r = c.post("/api/listings/import", headers=ORIGIN, files={"file": ("l.csv", io.BytesIO(
        b"title,location,price,deal,type,beds\nNice flat,Pune,5000000,sale,flat,2\nBad,,x,sale,flat,2\n"), "text/csv")}).json()
    assert r["added"] == 1 and r["errors"]


def test_matching_rules():
    lst = [{"id": "1", "title": "A", "location": "Austin", "deal": "sale", "price": 560000, "currency": "USD",
            "property_type": "House", "bedrooms": 3, "status": "available"}]
    base = {"intent": "buy", "budget": 550000, "currency": "USD", "location": "Austin", "property_type": None,
            "bedrooms": 3}
    assert match_listings(base, lst)[0]["reasons"][1] == "Just above budget (within 10%)"
    assert match_listings({**base, "budget": 400000}, lst) == []
    assert match_listings({**base, "location": "Denver"}, lst) == []
    assert match_listings({**base, "intent": "rent"}, lst) == []
    assert match_listings({**base, "bedrooms": 4}, lst) == []


# ---------- security ----------

def test_other_users_cannot_touch_my_data():
    me, them = signed_in(), signed_in()
    lead = score(me, "Buying a flat in Pune for 50 lakhs")
    lst = me.post("/api/listings", json={"title": "Flat", "location": "Pune", "price": 5000000}, headers=ORIGIN).json()
    assert them.get(f"/api/leads/{lead['id']}").status_code == 404
    assert them.patch(f"/api/leads/{lead['id']}", json={"status": "Lost"}, headers=ORIGIN).status_code == 404
    assert them.delete(f"/api/leads/{lead['id']}", headers=ORIGIN).status_code == 404
    assert them.patch(f"/api/listings/{lst['id']}", json={"title": "Mine", "location": "X", "price": 1},
                      headers=ORIGIN).status_code == 404
    assert them.delete(f"/api/listings/{lst['id']}", headers=ORIGIN).status_code == 404
    assert them.get("/api/listings").json()["items"] == []
    assert me.get(f"/api/leads/{lead['id']}").json()["status"] == "New"


def test_account_locks_after_wrong_passwords_and_unlocks():
    c, email = anon(), new_email()
    c.post("/api/auth/register", json={"name": "Lock Test", "email": email, "password": PASSWORD})
    c.post("/api/auth/logout", headers=ORIGIN)
    codes = [anon().post("/api/auth/login", json={"email": email, "password": "Wrong12345"}).status_code
             for _ in range(5)]
    assert codes == [401, 401, 401, 401, 429]
    main._hits.clear()
    locked = anon().post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert locked.status_code == 429 and "paused" in locked.json()["detail"]
    main.db.execute("UPDATE users SET locked_until = ? WHERE email = ?", (int(time.time()) - 1, email))
    assert anon().post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200


def test_admin_only_routes():
    agent = signed_in()
    assert agent.get("/api/admin/users").status_code == 403
    assert agent.get("/api/admin/activity").status_code == 403
    assert anon().get("/api/activity").status_code == 401
    email = new_email()
    main.accounts.admin_emails = {email}
    admin = anon()
    admin.post("/api/auth/register", json={"name": "Boss", "email": email, "password": PASSWORD})
    assert admin.get("/api/auth/me").json()["user"]["role"] == "admin"
    users = admin.get("/api/admin/users").json()["items"]
    assert any(u["email"] == email for u in users) and all("password" not in str(u).lower() for u in users)
    assert admin.get("/api/admin/activity").status_code == 200
    main.accounts.admin_emails = set()


def test_activity_log_has_no_customer_content():
    c = signed_in()
    score(c, "Secret message about a flat in Pune, call 9876543210")
    items = c.get("/api/activity").json()["items"]
    assert items[0]["action"] == "lead_scored"
    assert "9876543210" not in str(items) and "Secret" not in str(items)
