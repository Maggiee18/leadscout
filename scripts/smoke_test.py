"""End to end smoke test against a running LeadScout server, including the real AI path.

    python scripts/smoke_test.py                      # http://localhost:8000
    python scripts/smoke_test.py https://your-app.onrender.com

It creates a throwaway account, scores one lead and the sample batch, checks the
results, then deletes the account again. Exit code 0 means everything passed.
Only the standard library is used.
"""

import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
failures = []


def call(method, path, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(ok, label, detail=""):
    print(("PASS " if ok else "FAIL ") + label + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(label)


code, st = call("GET", "/api/status")
check(code == 200, "server is up", BASE)
ai = bool(st.get("ai_configured"))
print(f"     AI configured: {ai}  model: {st.get('model')}")
check(call("GET", "/readyz")[0] == 200, "database ready")

email, password = f"smoke-{secrets.token_hex(4)}@example.com", "Smoke" + secrets.token_hex(4) + "9"
check(call("POST", "/api/auth/register", {"name": "Smoke Test", "email": email, "password": password})[0] == 201,
      "register throwaway account")

msg = ("Hi, I'm looking for a 3BHK apartment in Vijayawada around ₹80 lakhs. I'm pre-approved for a "
       "home loan and would like to move within the next two months.")
t = time.time()
code, r = call("POST", "/api/score", {"name": "Ravi", "message": msg})
check(code == 200, "score one lead", f"{time.time() - t:.1f}s")
if code == 200:
    print(f"     engine={r['engine']} sms={r['sms_engine']} score={r['score']} tier={r['tier']}")
    print(f"     facts={json.dumps(r['display'], ensure_ascii=False)}")
    print(f"     sms={r['sms']}")
    if ai:
        check(r["engine"] == "ai", "lead was read by the AI", r.get("fallback_reason") or "")
        check(r["sms_engine"] == "ai", "reply was written by the AI")
    check(sum(b["points"] for b in r["breakdown"]) == r["raw_total"], "breakdown adds up to the total")
    check(r["tier"] == ("Hot" if r["score"] >= 70 else "Warm" if r["score"] >= 40 else "Cold"),
          "tier matches the score")
    check(r["display"]["location"] == "Vijayawada" and r["display"]["financing"] == "Pre-approved",
          "location and financing extracted")

t = time.time()
code, b = call("POST", "/api/batch/sample", timeout=300)
check(code == 200 and b.get("count") == 6, "score the 6 sample leads", f"{time.time() - t:.1f}s")
if code == 200:
    print(f"     engines={b['engines']} summary={b['summary']}")
    for x in b["results"]:
        print(f"     {x['score']:>3} {x['tier']:<4} {x['name']:<8} engine={x['engine']}")
    scores = [x["score"] for x in b["results"]]
    check(scores == sorted(scores, reverse=True), "results sorted by score")

code, h = call("GET", "/api/leads?limit=100")
check(code == 200 and h.get("total") == 7, "all 7 leads saved to history")
check(call("POST", "/api/account/delete", {"password": password})[1] == {"ok": True}, "delete throwaway account")

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED: {', '.join(failures)}")
sys.exit(1 if failures else 0)
