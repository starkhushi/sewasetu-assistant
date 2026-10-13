# -*- coding: utf-8 -*-
"""
Rule checks against the portal API, no model involved. Run before the persona evals:
if one of these fails, a conversation that "passes" would be passing for the wrong
reason.

    python evals/smoke.py
"""

import os
import sys
import base64

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from signin import sign_in, PORTAL  # noqa: E402
import db  # noqa: E402

CITIZEN = os.environ.get("MCP_CITIZEN_URL", "http://localhost:8101/mcp/citizen")
OFFICER = os.environ.get("MCP_OFFICER_URL", "http://localhost:8102/mcp/officer")
API = PORTAL + "/api/v1"
HERE = os.path.dirname(os.path.abspath(__file__))
failures = []


def check(name, cond, detail=""):
    print(("  ok    " if cond else "  FAIL  ") + name + ("" if cond else "  -> " + str(detail)[:300]))
    if not cond:
        failures.append(name)


def client(token):
    s = requests.Session()
    s.headers["Authorization"] = "Bearer " + token
    return s


def main():
    conn = db.connect()
    cur = conn.cursor()
    for m in ("9800000090", "9800000091", "9800000092"):
        db.clear_mobile(cur, m)

    print("citizen: apply end to end")
    c = client(sign_in(CITIZEN, "citizen", mobile="9800000090")["access_token"])
    r = c.put(API + "/citizen/draft", json={"applicant_name": "Smoke Test", "dob": "1950-01-01", "gender": "female",
                                            "marital_status": "widowed", "village": "Lakhipur", "block": "sonari",
                                            "bank_account": "1234 5678 9012", "ifsc": "sbin0003077"})
    d = r.json()
    check("draft normalises dob, block, ifsc, account", d["draft"]["dob"] == "01/01/1950" and d["draft"]["block"] == "Sonari"
          and d["draft"]["ifsc"] == "SBIN0003077" and d["draft"]["bank_account"] == "123456789012", d)
    check("draft says the document is missing", "age_proof_document" in d["still_missing"], d)
    r = c.post(API + "/citizen/draft/review")
    check("review refuses without a document", r.json()["can_submit"] is False, r.json())
    link = c.post(API + "/citizen/draft/upload-link").json()["upload_url"]
    with open(os.path.join(HERE, "fixtures", "voter_id.png"), "rb") as fh:
        up = requests.post(link, files={"document": ("voter_id.png", fh, "image/png")})
    check("upload link accepts the file", up.status_code == 200 and "attached" in up.text, up.status_code)
    check("upload link is single-use", requests.get(link).status_code == 410)
    rv = c.post(API + "/citizen/draft/review").json()
    check("review gives a token once complete", rv.get("can_submit") and rv.get("review_token"), rv)
    c.put(API + "/citizen/draft", json={"village": "Lakhipur Gaon"})
    r = c.post(API + "/citizen/draft/submit", json={"review_token": rv["review_token"], "declaration_accepted": True})
    check("submit refuses a stale review", r.status_code == 409, r.text)
    rv = c.post(API + "/citizen/draft/review").json()
    r = c.post(API + "/citizen/draft/submit", json={"review_token": rv["review_token"], "declaration_accepted": False})
    check("submit refuses without the declaration", r.status_code == 400, r.text)
    r = c.post(API + "/citizen/draft/submit", json={"review_token": rv["review_token"], "declaration_accepted": True})
    check("submit creates the application", r.status_code == 201, r.text)
    a = db.latest_application(cur, "9800000090")
    check("stored as PENDING via AGENT with the edited village", a and a["status"] == "PENDING" and a["channel"] == "AGENT"
          and a["village"] == "Lakhipur Gaon", a)
    c.put(API + "/citizen/draft", json={"applicant_name": "Second Try", "dob": "01/01/1950", "village": "X", "block": "Sonari",
                                        "bank_account": "123456789012", "ifsc": "SBIN0003077"})
    rv = c.post(API + "/citizen/draft/review").json()
    check("a second live application is refused", rv["can_submit"] is False and any("already has" in p for p in rv["problems"]), rv)
    c.delete(API + "/citizen/draft")
    me = c.get(API + "/citizen/me").json()
    check("status explains deemed approval date", "automatically" in me["live_application"]["in_plain_words"], me)
    r = c.post(API + "/citizen/applications/%s/grievance" % a["application_no"], json={"detail": "no money has come yet at all"})
    check("payment complaint refused while pending", r.status_code == 409, r.text)

    print("citizen: under 60 cannot apply")
    y = client(sign_in(CITIZEN, "citizen", mobile="9800000091")["access_token"])
    y.put(API + "/citizen/draft", json={"applicant_name": "Young Man", "dob": "12/08/1969", "village": "Teliagaon",
                                        "block": "Rajapara", "bank_account": "112233445566", "ifsc": "UTBI0PUR2819"})
    y.post(API + "/citizen/draft/document", json={"filename": "id.png",
                                                  "content_base64": base64.b64encode(open(os.path.join(HERE, "fixtures", "voter_id.png"), "rb").read()).decode()})
    rv = y.post(API + "/citizen/draft/review").json()
    check("under-60 refused at review", rv["can_submit"] is False and any("60" in p for p in rv["problems"]), rv)
    r = y.post(API + "/citizen/draft/submit", json={"review_token": "x", "declaration_accepted": True})
    check("under-60 refused at submit", r.status_code in (409, 422), r.text)
    check("no application stored", db.latest_application(cur, "9800000091") is None)

    print("citizen: approved, money never came, life certificate")
    no = db.seed_application(cur, "9800000092", {"status": "APPROVED", "applicant_name": "Paid Person", "dob": "1950-01-01",
                                                 "village": "Kathoni", "block": "Dhemaji Pathar", "bank_account": "5544332214321",
                                                 "ifsc": "CNRB0004417", "submitted_days_ago": 160, "decided_days_ago": 150,
                                                 "life_certificate_due_days_ago": 10}, "fixtures/voter_id.png")
    p = client(sign_in(CITIZEN, "citizen", mobile="9800000092")["access_token"])
    st = p.get(API + "/citizen/applications/" + no).json()
    check("status names account ending and IFSC", "4321" in st["in_plain_words"] and "CNRB0004417" in st["in_plain_words"], st)
    check("status says life certificate overdue", "was due" in st["in_plain_words"], st["in_plain_words"])
    check("pension runs from month of application", st["months_payable_so_far"] >= 5, st)
    r = p.post(API + "/citizen/applications/%s/grievance" % no, json={"detail": "approved in May, no credit, bank says KYC not done"})
    check("payment complaint recorded", r.status_code == 201, r.text)
    r2 = p.post(API + "/citizen/applications/%s/grievance" % no, json={"detail": "approved in May, no credit, bank says KYC"})
    check("same complaint not duplicated", r2.json().get("already_open") is True, r2.text)
    r = p.post(API + "/citizen/applications/%s/life-certificate" % no)
    check("life certificate recorded", r.status_code == 200 and db.latest_application(cur, "9800000092")["life_certified_at"], r.text)
    r = p.get(API + "/citizen/applications/" + a["application_no"])
    check("cannot read someone else's application", r.status_code == 404, r.status_code)

    print("officer")
    o = client(sign_in(OFFICER, "officer")["access_token"])
    check("citizen token refused on officer API", c.get(API + "/officer/summary").status_code == 403)
    check("officer token refused on citizen API", o.get(API + "/citizen/me").status_code == 403)
    s = o.get(API + "/officer/summary").json()
    check("summary counts pending and complaints", s["pending"] >= 1 and s["open_complaints"] >= 1, s)
    q = o.get(API + "/officer/queue", params={"block": "sonari", "limit": 50}).json()
    ages = [x["days_pending"] for x in q["queue"]]
    check("queue is oldest first", ages == sorted(ages, reverse=True), ages[:10])
    case = o.get(API + "/officer/case/" + a["application_no"]).json()
    check("case carries flags and audit", "flags" in case and case["audit_trail"], case.get("flags"))
    r = o.post(API + "/officer/case/%s/decision" % a["application_no"], json={"decision": "reject", "reason": "no"})
    check("reject without a real reason refused", r.status_code == 400, r.text)
    r = o.post(API + "/officer/case/%s/decision" % a["application_no"], json={"decision": "approve"})
    check("approve recorded in officer's name", r.status_code == 200 and r.json()["decided_by"] == "admin", r.text)
    a2 = db.latest_application(cur, "9800000090")
    check("effective_from is the month of application", a2["effective_from"] == a2["submitted_at"].date().replace(day=1), a2)
    r = o.post(API + "/officer/case/%s/decision" % a["application_no"], json={"decision": "reject", "reason": "changed my mind about it"})
    check("a decided case cannot be decided again", r.status_code == 409, r.text)
    g = o.get(API + "/officer/grievances").json()
    gid = [x for x in g["grievances"] if x["mobile"] == "9800000092"][0]["grievance_id"]
    r = o.post(API + "/officer/grievances/%d/resolve" % gid, json={"resolution": "Account was frozen for KYC; bank updated it, arrears released"})
    check("complaint closed in officer's name", r.status_code == 200, r.text)

    for m in ("9800000090", "9800000091", "9800000092"):
        db.clear_mobile(cur, m)
    print("\n%d failed" % len(failures) if failures else "\nall passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
