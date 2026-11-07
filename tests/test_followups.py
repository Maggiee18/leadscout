"""Version 5: follow ups, priority, Negotiating, weekly trends, scoring boundaries and CSV edge cases."""
import io
import time

import pytest
from test_leadscout import PASSWORD, anon, new_email, signed_in  # noqa: F401  (also sets up the test database)

import main
from leads import day_bounds
from scorer import EMPTY_FIELDS, clean_fields, extract_with_rules, score_lead, tier_for

ORIGIN = {"Origin": "http://testserver"}
HOT = "Cash buyer, 3 bed house in Austin, $500k, moving ASAP"
WARM = "Looking to buy a home in Austin within 3 months, budget $400,000"
COLD = "Just browsing, maybe a house someday"


@pytest.fixture(autouse=True)
def reset_rate_limit():
    main._hits.clear()
    yield


def score(c, message, **kw):
    r = c.post("/api/score", json={"message": message, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def patch(c, lead_id, **body):
    return c.patch(f"/api/leads/{lead_id}", json=body, headers=ORIGIN)


# ---------- score boundaries ----------

@pytest.mark.parametrize("value,tier", [(0, "Cold"), (39, "Cold"), (40, "Warm"), (69, "Warm"), (70, "Hot"), (100, "Hot")])
def test_tier_boundaries(value, tier):
    assert tier_for(value) == tier


def fields(**kw):
    return clean_fields({**EMPTY_FIELDS, **kw})


def test_exactly_70_is_hot_and_exactly_40_is_warm():
    # 30 (within a month) + 25 (cash) + 15 (budget) = 70
    s, t, _ = score_lead(fields(timeline_months=1, financing="cash", budget=500000, currency="USD"))
    assert (s, t) == (70, "Hot")
    # 25 (within 3 months) + 15 (budget) = 40
    s, t, _ = score_lead(fields(timeline_months=3, budget=500000, currency="USD"))
    assert (s, t) == (40, "Warm")
    # 25 + 10 (location) = 35, plus 3 for renting = 38: Cold
    s, t, _ = score_lead(fields(timeline_months=3, location="Denver", intent="rent"))
    assert (s, t) == (38, "Cold")


def test_score_is_clamped_and_never_disagrees_with_tier():
    s, t, pts = score_lead(fields(timeline_months=None, red_flags=["just browsing", "unsure", "no budget"]))
    assert s == 0 and t == "Cold" and sum(p for _, _, p in pts) < 0
    s, t, _ = score_lead(fields(timeline_months=0, financing="cash", budget=1, currency="USD", location="Austin",
                                intent="buy", motivation="relocating for work"))
    assert s == 100 and t == "Hot"


@pytest.mark.parametrize("message,months", [
    ("Want to buy in 2 to 3 months", 3),            # a range counts by its later end
    ("Need a flat in 3-6 months", 6),
    ("Buying ASAP, at the latest within 6 months", 0),  # several timelines: the soonest counts
    ("Within 1 year, ideally 3 months", 3),
    ("Moving next month or maybe in a year", 1),
    ("in two weeks", 0.5),
    ("next year", 12),
    ("no rush at all", None),
])
def test_overlapping_and_range_timelines(message, months):
    assert extract_with_rules(message)["timeline_months"] == months


@pytest.mark.parametrize("message,budget,currency", [
    ("Budget ₹80 lakhs", 8_000_000, "INR"), ("Budget Rs. 1.2 crore", 12_000_000, "INR"),
    ("budget 80L", 8_000_000, "INR"), ("budget INR 75,00,000", 7_500_000, "INR"), ("budget 2.5 cr", 25_000_000, "INR"),
    ("Budget $450k", 450_000, "USD"), ("450,000 USD budget", 450_000, "USD"), ("budget $1.5M", 1_500_000, "USD"),
])
def test_currency_formats(message, budget, currency):
    f = extract_with_rules(message)
    assert (f["budget"], f["currency"]) == (budget, currency)


def test_landing_example_shows_its_real_score():
    html = anon().get("/").text
    assert '<b data-count="85">85</b>' in html and 'data-score="85"' in html and "Hot lead" in html


# ---------- follow ups and priority ----------

def test_new_leads_get_priority_and_follow_up_from_tier():
    c = signed_in()
    now = int(time.time())
    hot, warm, cold = score(c, HOT), score(c, WARM), score(c, COLD)
    assert (hot["tier"], warm["tier"], cold["tier"]) == ("Hot", "Warm", "Cold")
    assert (hot["priority"], warm["priority"], cold["priority"]) == ("High", "Medium", "Low")
    assert abs(hot["follow_up_at"] - now) < 5 and abs(warm["follow_up_at"] - (now + 3 * 86400)) < 5
    assert cold["follow_up_at"] is None
    saved = c.get(f"/api/leads/{hot['id']}").json()
    assert saved["priority"] == "High" and saved["follow_up_at"] == hot["follow_up_at"]


def test_follow_up_buckets_and_filters():
    c = signed_in()
    a, b, d = score(c, COLD, name="Overdue"), score(c, COLD, name="Today"), score(c, COLD, name="Later")
    now = int(time.time())
    start, end = day_bounds(now, 0)
    assert patch(c, a["id"], follow_up_at=start - 86400).status_code == 200
    assert patch(c, b["id"], follow_up_at=start + 60).status_code == 200
    assert patch(c, d["id"], follow_up_at=end + 2 * 86400).status_code == 200
    f = c.get("/api/leads/followups?tz=0").json()
    assert [r["name"] for r in f["overdue"]["items"]] == ["Overdue"]
    assert [r["name"] for r in f["today"]["items"]] == ["Today"]
    assert [r["name"] for r in f["upcoming"]["items"]] == ["Later"]
    assert f["due"] == 2
    total = lambda q: c.get("/api/leads?" + q).json()["total"]  # noqa: E731
    assert total("followup=due&tz=0") == 2 and total("followup=overdue&tz=0") == 1
    assert total("followup=upcoming&tz=0") == 1 and total("followup=none") == 0
    assert c.get("/api/leads?followup=soon").status_code == 422
    order = [r["name"] for r in c.get("/api/leads?sort=followup").json()["items"]]
    assert order == ["Overdue", "Today", "Later"]
    # closed leads drop out of reminders
    patch(c, a["id"], status="Lost")
    assert c.get("/api/leads/followups?tz=0").json()["due"] == 1


def test_day_bounds_follow_the_viewers_timezone():
    # 20:00 UTC is already the next day in India (UTC+5:30, getTimezoneOffset() = -330)
    ts = 1_700_000_000 - 1_700_000_000 % 86400 + 20 * 3600
    start_utc, _ = day_bounds(ts, 0)
    start_ist, end_ist = day_bounds(ts, -330)
    assert start_ist == start_utc + 86400 - 330 * 60 and end_ist - start_ist == 86400
    assert start_ist <= ts < end_ist


def test_clearing_follow_up_priority_and_logging_contact():
    c = signed_in()
    r = score(c, HOT)
    e = patch(c, r["id"], follow_up_at=None, priority="Low").json()
    assert e["follow_up_at"] is None and e["priority"] == "Low" and e["last_contacted_at"] is None
    e = patch(c, r["id"], contacted=True).json()
    assert abs(e["last_contacted_at"] - time.time()) < 5
    assert patch(c, r["id"], priority="Urgent").status_code == 422
    assert patch(c, r["id"], follow_up_at=5).status_code == 422
    actions = [a["action"] for a in c.get("/api/activity").json()["items"][:3]]
    assert "lead_contacted" in actions and "lead_follow_up" in actions


def test_negotiating_status_counts_as_contact_and_appears_in_stats():
    c = signed_in()
    r = score(c, WARM)
    e = patch(c, r["id"], status="Negotiating").json()
    assert e["status"] == "Negotiating" and e["last_contacted_at"]
    st = c.get("/api/leads/stats").json()
    assert st["by_status"]["Negotiating"] == 1
    assert c.get("/api/leads?status=Negotiating").json()["total"] == 1


def test_priority_filter_and_sort():
    c = signed_in()
    score(c, COLD, name="C")
    score(c, HOT, name="H")
    score(c, WARM, name="W")
    assert c.get("/api/leads?priority=High").json()["total"] == 1
    assert [x["name"] for x in c.get("/api/leads?sort=priority").json()["items"]] == ["H", "W", "C"]


def test_follow_ups_are_private():
    a, b = signed_in(), signed_in()
    r = score(a, HOT)
    assert b.get("/api/leads/followups").json()["due"] == 0
    assert patch(b, r["id"], follow_up_at=None).status_code == 404
    assert anon().get("/api/leads/followups").status_code == 401


def test_weekly_and_monthly_trends():
    c = signed_in()
    r = score(c, HOT)
    score(c, COLD)
    patch(c, r["id"], status="Converted")
    st = c.get("/api/leads/stats").json()
    assert len(st["weekly"]) == 8 and len(st["monthly"]) == 6
    this_week = st["weekly"][-1]
    assert this_week["leads"] == 2 and this_week["converted"] == 1 and this_week["hot"] == 1
    assert this_week["conversion_rate"] == 50.0
    assert all(w["leads"] == 0 for w in st["weekly"][:-1])
    assert st["monthly"][-1]["converted"] == 1


def test_export_includes_follow_up_columns():
    c = signed_in()
    score(c, HOT)
    head = c.get("/api/leads/export.csv").text.lstrip("﻿").splitlines()[0]
    assert "Priority" in head and "Next follow up (UTC)" in head and "Last contacted (UTC)" in head


# ---------- CSV edge cases ----------

def up(c, content, name="leads.csv"):
    return c.post("/api/batch", headers=ORIGIN, files={"file": (name, io.BytesIO(content), "text/csv")})


def test_csv_missing_names_and_empty_messages():
    c = signed_in()
    r = up(c, b"name,message\n,Need a 2 bed flat in Pune within 3 months\nAsha,\nRavi,Buying a villa in Goa, cash\n").json()
    assert r["count"] == 2
    names = sorted(x["name"] for x in r["results"])
    assert names[0].startswith("Lead") and "Ravi" in names


def test_csv_duplicates_within_one_file_are_flagged():
    c = signed_in()
    rows = (b"name,message\nA,Call me on 9876543210 for a flat in Pune\n"
            b"B,Flat in Pune please. My number 98765 43210\nC,House in Austin for $400k\n")
    r = up(c, rows).json()
    dups = [x for x in r["results"] if x["duplicate_of"]]
    assert len(dups) == 1 and c.get("/api/leads?duplicates=true").json()["total"] == 1


def test_csv_mixed_currency_formats():
    c = signed_in()
    rows = ("name,message\n"
            'A,"Budget ₹80 lakhs, flat in Pune"\n'
            'B,"Budget $450k, house in Austin"\n'
            'C,"Budget Rs 1.2 crore, villa in Goa"\n').encode()
    got = {x["name"]: (x["fields"]["budget"], x["fields"]["currency"]) for x in up(c, rows).json()["results"]}
    assert got == {"A": (8_000_000, "INR"), "B": (450_000, "USD"), "C": (12_000_000, "INR")}


def test_csv_over_50_rows_is_rejected_with_a_clear_message():
    rows = "name,message\n" + "".join(f"L{i},Need to rent in Denver ASAP for $2000\n" for i in range(51))
    r = up(signed_in(), rows.encode())
    assert r.status_code == 400 and "limit is 50" in r.json()["detail"]


@pytest.mark.parametrize("content,name", [(b"", "a.csv"), (b"\n\n\n", "a.csv"), (b"\x00\x01binary", "a.csv"),
                                          (b"name,message\nA,hi", "a.exe")])
def test_csv_empty_and_invalid_files(content, name):
    r = up(signed_in(), content, name)
    assert r.status_code == 400 and isinstance(r.json()["detail"], str)


# ---------- account deletion removes everything ----------

def test_account_delete_removes_listings_and_activity():
    c = signed_in()
    uid = c.get("/api/auth/me").json()["user"]["id"]
    c.post("/api/listings/sample", headers=ORIGIN)
    score(c, HOT)
    assert c.post("/api/account/delete", json={"password": PASSWORD}, headers=ORIGIN).status_code == 200
    for table in ("leads", "listings", "activity_log"):
        assert main.db.fetchone(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (uid,))[0] == 0
