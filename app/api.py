# -*- coding: utf-8 -*-
"""
Sewa Setu JSON API for assistants.

This is what the two MCP servers call. Nothing here can do what the web form or the
admin pages cannot: an application is created only through validate_application()
and create_application(), and a decision only through decide_application(), so the
agent channel admits exactly what the counter admits and the record of who did
what reads the same.

Every call carries a bearer token minted by the portal's own OAuth server (oauth.py).
The token's `aud` is the MCP server the person signed in to (RFC 8707), its `role`
says whether they are a citizen or an officer, and `sub` says who.
"""

import os
import re
import io
import json
import base64
import hashlib
import hmac
import secrets
import functools
from datetime import datetime, date, timedelta

from flask import Blueprint, request, jsonify, g

import app as portal
import oauth

api = Blueprint("api", __name__, url_prefix="/api/v1")

# The audiences a token may carry to be accepted here: the portal itself, and the
# two MCP servers. Tokens for any other resource are refused (RFC 8707).
MCP_CITIZEN_URL = os.environ.get("MCP_CITIZEN_URL", "http://localhost:8101/mcp/citizen").rstrip("/")
MCP_OFFICER_URL = os.environ.get("MCP_OFFICER_URL", "http://localhost:8102/mcp/officer").rstrip("/")
ALLOWED_AUDIENCES = [oauth.ISSUER, MCP_CITIZEN_URL, MCP_OFFICER_URL,
                     MCP_CITIZEN_URL + "/", MCP_OFFICER_URL + "/"]

DRAFT_FIELDS = ("applicant_name", "dob", "gender", "marital_status", "husband_name",
                "village", "block", "bank_account", "ifsc")
GRIEVANCE_KINDS = ("PAYMENT_NOT_RECEIVED", "WRONG_BANK_ACCOUNT", "DETAILS_WRONG", "OTHER")


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

def _error(status, code, message):
    return jsonify({"error": code, "message": message}), status


def require_role(role):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            header = request.headers.get("Authorization", "")
            if not header.lower().startswith("bearer "):
                return _error(401, "unauthorized", "a bearer token from the Sewa Setu sign-in is required")
            claims = oauth.verify_token(header[7:].strip(), ALLOWED_AUDIENCES)
            if not claims:
                return _error(401, "invalid_token", "the token is not valid for this portal, or has expired")
            if claims.get("role") != role or role not in (claims.get("scope") or "").split():
                return _error(403, "wrong_role", "this call needs the %s's sign-in" % role)
            g.claims = claims
            g.subject = claims.get("sub", "")
            g.who = g.subject.split(":", 1)[1] if ":" in g.subject else g.subject
            return fn(*a, **kw)
        return wrapper
    return deco


def _body():
    return request.get_json(silent=True) or {}


# ---------------------------------------------------------------------------
# shared shaping
# ---------------------------------------------------------------------------

def _d(v):
    """Dates and timestamps as ISO strings for JSON."""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    return v


def _mask(acct):
    acct = acct or ""
    return ("x" * max(0, len(acct) - 4)) + acct[-4:] if acct else ""


def _decision_note(cur, app_id):
    cur.execute("SELECT note, actor, at FROM audit_log WHERE application_id = %s AND action IN "
                "('APPROVED', 'REJECTED', 'DEEMED_APPROVED') ORDER BY at DESC LIMIT 1", (app_id,))
    return cur.fetchone()


def _grievances(cur, app_id):
    cur.execute("SELECT id, kind, detail, status, raised_at, resolved_at, resolved_by, resolution "
                "FROM grievances WHERE application_id = %s ORDER BY raised_at DESC", (app_id,))
    return [{"id": r[0], "kind": r[1], "detail": r[2], "status": r[3], "raised_at": _d(r[4]),
             "resolved_at": _d(r[5]), "resolved_by": r[6], "resolution": r[7]} for r in cur.fetchall()]


APP_COLS = ("id, application_no, applicant_name, mobile, dob, gender, marital_status, husband_name, "
            "village, block, bank_account, ifsc, doc_path, status, submitted_at, decided_at, "
            "decided_by, channel, effective_from, life_certificate_due, life_certified_at")


def _app_dict(row):
    keys = [c.strip() for c in APP_COLS.split(",")]
    return dict(zip(keys, row))


def explain(cur, a):
    """Plain words about where an application stands and what, if anything, to do.
    Written for someone who will hear it read out, not for a dashboard."""
    now = datetime.now()
    st = a["status"]
    lines = []
    facts = {"application_no": a["application_no"], "status": st,
             "submitted_on": _d(a["submitted_at"]), "applied_via": a.get("channel") or "WEB",
             "bank_account_ending": _mask(a["bank_account"]), "ifsc": a["ifsc"]}
    if st == "PENDING":
        due = portal.deemed_on(a["submitted_at"])
        left = (due - now.date()).days
        facts.update({"deemed_approval_on": due.isoformat(), "days_until_deemed_approval": left,
                      "days_pending": (now - a["submitted_at"]).days})
        lines.append("Application %s was received on %s and is waiting for the block officer."
                     % (a["application_no"], a["submitted_at"].strftime("%d %B %Y")))
        if left > 0:
            lines.append("If no decision is made by %s (%d days from now), the law approves it "
                         "automatically." % (due.strftime("%d %B %Y"), left))
        else:
            lines.append("The 15-day limit has passed; it will be marked approved in the next "
                         "nightly run.")
        lines.append("Nothing more is needed from you right now.")
    elif st in ("APPROVED", "DEEMED_APPROVED"):
        eff = a.get("effective_from") or portal.effective_from_for(a["submitted_at"])
        months = (now.year - eff.year) * 12 + (now.month - eff.month) + 1
        who = ("automatically under the Right to Public Services Act, because no officer decided "
               "within 15 days" if st == "DEEMED_APPROVED" else
               "by the block officer (%s)" % (a["decided_by"] or "officer"))
        facts.update({"approved_on": _d(a["decided_at"]), "pension_per_month": portal.PENSION_PER_MONTH,
                      "payable_from_month": eff.strftime("%B %Y"), "months_payable_so_far": months,
                      "amount_payable_so_far": months * portal.PENSION_PER_MONTH})
        lines.append("Application %s is approved %s on %s."
                     % (a["application_no"], who, a["decided_at"].strftime("%d %B %Y")))
        lines.append("The pension is Rs %d a month, payable from %s. That is %d month(s) so far, "
                     "Rs %d in all, paid by bank transfer into the account ending %s at IFSC %s."
                     % (portal.PENSION_PER_MONTH, eff.strftime("%B %Y"), months,
                        months * portal.PENSION_PER_MONTH, a["bank_account"][-4:], a["ifsc"]))
        lines.append("If money has not come: first confirm that account number and IFSC are really "
                     "yours and the account is open; if they are, raise a payment complaint and the "
                     "officer must look into it.")
        due = a.get("life_certificate_due")
        if due:
            facts["life_certificate_due"] = due.isoformat()
            facts["life_certificate_last_confirmed"] = _d(a.get("life_certified_at"))
            days = (due - now.date()).days
            if days < 0:
                lines.append("The yearly confirmation that you are alive was due on %s and has not "
                             "been given. Please confirm it now so the payment is not held up."
                             % due.strftime("%d %B %Y"))
            elif days <= 60:
                lines.append("The yearly confirmation that you are alive is due by %s (%d days). "
                             "It can be done right here." % (due.strftime("%d %B %Y"), days))
    elif st == "REJECTED":
        note = _decision_note(cur, a["id"])
        reason = (note[0] if note and note[0] else "no reason was recorded")
        facts.update({"rejected_on": _d(a["decided_at"]), "reason": reason})
        lines.append("Application %s was rejected on %s by %s." %
                     (a["application_no"], a["decided_at"].strftime("%d %B %Y"), a["decided_by"] or "the officer"))
        lines.append("The reason recorded is: %s" % reason)
        lines.append("A new application can be made from this mobile number once that is put right.")
    elif st == "WITHDRAWN":
        facts["withdrawn_on"] = _d(a["decided_at"])
        lines.append("Application %s was withdrawn on %s. A fresh application can be made."
                     % (a["application_no"], a["decided_at"].strftime("%d %B %Y")))
    open_g = [x for x in _grievances(cur, a["id"]) if x["status"] == "OPEN"]
    if open_g:
        facts["open_complaints"] = open_g
        lines.append("There is an open complaint (%s) raised on %s; the officer has not closed it yet."
                     % (open_g[0]["kind"].replace("_", " ").lower(), open_g[0]["raised_at"]))
    facts["in_plain_words"] = " ".join(lines)
    return facts


# ---------------------------------------------------------------------------
# public
# ---------------------------------------------------------------------------

@api.get("/scheme")
def scheme():
    hours = portal.deadline_remaining()
    return jsonify({
        "scheme": "Old Age Pension, Government of Purvanchal",
        "pension_per_month": portal.PENSION_PER_MONTH,
        "eligibility": "resident of Purvanchal aged %d or above on the day of applying" % portal.MIN_AGE,
        "one_application_rule": "one live application per mobile number; a pending one can be withdrawn",
        "needed": ["the applicant's own mobile number (an OTP goes to it)",
                   "full name exactly as on the age-proof document",
                   "date of birth as DD/MM/YYYY",
                   "gender; marital status; late husband's name only if widowed (optional)",
                   "village and block (one of: %s)" % ", ".join(portal.BLOCKS),
                   "bank account number (9 to 18 digits) and IFSC (11 characters), in the applicant's own name",
                   "one age-proof document: birth certificate, school leaving certificate or Voter ID; "
                   "JPG, PNG or PDF up to 5 MB. Aadhaar is not asked for."],
        "after_submitting": "the block officer decides within %d days; if not, the application stands "
                            "approved automatically. Pension runs from the month of application."
                            % portal.SLA_DAYS,
        "window_closes": portal.SCHEME_DEADLINE.strftime("%d %B %Y %H:%M IST"),
        "hours_left_in_window": hours,
        "blocks": portal.BLOCKS,
    })


# ---------------------------------------------------------------------------
# citizen
# ---------------------------------------------------------------------------

def _draft(cur, mobile):
    cur.execute("SELECT data, doc_path, updated_at FROM drafts WHERE mobile = %s", (mobile,))
    row = cur.fetchone()
    if not row:
        return {}, "", None
    return (row[0] or {}), (row[1] or ""), row[2]


def _normalise_dob(raw):
    """Accept the common ways a date gets typed and return DD/MM/YYYY, or the raw
    text if it cannot be read (validate_application then explains the format)."""
    raw = (raw or "").strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%d %m %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return raw


def _draft_view(cur, mobile, data, doc_path):
    errors, _ = portal.validate_application(data, doc_path)
    missing = [k for k in ("applicant_name", "dob", "village", "block", "bank_account", "ifsc")
               if not (data.get(k) or "").strip()]
    if not doc_path:
        missing.append("age_proof_document")
    live = portal.active_application(cur, mobile)
    out = {"mobile": mobile, "draft": {k: data.get(k, "") for k in DRAFT_FIELDS},
           "age_proof_document": os.path.basename(doc_path) if doc_path else None,
           "still_missing": missing, "problems": errors, "ready_to_review": not errors}
    if live:
        out["ready_to_review"] = False
        out["problems"] = out["problems"] + [
            "this mobile already has application %s (%s); a second one cannot be made while it is live"
            % (live[1], live[2])]
    # One concrete next step. Small models follow this far better than general rules
    # (an eval caught Gemma promising "I will send a link" without ever making one).
    labels = {"applicant_name": "the name as on the age proof", "dob": "the date of birth (DD/MM/YYYY)",
              "village": "the village", "block": "the block", "bank_account": "the bank account number",
              "ifsc": "the IFSC"}
    fields_missing = [k for k in missing if k != "age_proof_document"]
    dob = portal.parse_dob(data.get("dob"))
    too_young = dob is not None and portal.age_on(dob, portal.now_ist().date()) < portal.MIN_AGE
    if live:
        out["next_step"] = "Explain the existing application instead; do not start a new one."
    elif too_young:
        # eligibility comes before anything else (an eval had Gemma collecting a 57-year-old's
        # bank details because the next step only looked at missing fields)
        sixty = dob.replace(year=dob.year + portal.MIN_AGE) if not (dob.month == 2 and dob.day == 29) \
            else dob.replace(year=dob.year + portal.MIN_AGE, day=28)
        out["eligible"] = False
        out["next_step"] = ("Stop here. By the date of birth on the document the applicant is %d, and the scheme "
                            "is for %d and above. Tell them plainly they cannot apply yet, that they can apply "
                            "from %s, and do not ask for anything more. Do not change the date of birth."
                            % (portal.age_on(dob, portal.now_ist().date()), portal.MIN_AGE,
                               sixty.strftime("%d %B %Y")))
    elif fields_missing:
        out["next_step"] = "Ask for %s." % labels[fields_missing[0]]
    elif not doc_path:
        # The portal makes the link itself: two eval runs had Gemma invent a plausible
        # government URL instead of calling upload_link. A real link to copy leaves nothing to invent.
        url = _upload_url(cur, mobile)
        out["upload_url"] = url
        out["next_step"] = ("Only the age-proof document is missing. Give the person this exact link, copied "
                            "character for character: %s . Say: tap the link, choose the photo of the Voter ID, "
                            "birth certificate or school certificate, press Upload, then come back and say done. "
                            "Never write any other link." % url)
    elif out["ready_to_review"]:
        out["next_step"] = "Call review_application and read the whole preview and the declaration back."
    else:
        out["next_step"] = "Explain the problems listed and ask for the corrected detail."
    return out


@api.get("/citizen/me")
@require_role("citizen")
def citizen_me():
    mobile = g.who
    conn = portal.get_db()
    cur = conn.cursor()
    cur.execute("SELECT " + APP_COLS + " FROM applications WHERE mobile = %s ORDER BY submitted_at DESC",
                (mobile,))
    apps = [_app_dict(r) for r in cur.fetchall()]
    data, doc_path, updated = _draft(cur, mobile)
    live = [a for a in apps if a["status"] in portal.ACTIVE_STATUSES]
    out = {"mobile": mobile,
           "signed_in_as": "the holder of mobile %s (verified by OTP on the portal)" % mobile,
           "live_application": None, "past_applications": [],
           "draft_in_progress": bool(data or doc_path), "window_open": portal.deadline_remaining() is not None}
    if live:
        out["live_application"] = explain(cur, live[0])
    out["past_applications"] = [{"application_no": a["application_no"], "status": a["status"],
                                 "submitted_on": _d(a["submitted_at"])} for a in apps if a not in live]
    if data or doc_path:
        out["draft"] = _draft_view(cur, mobile, data, doc_path)
    if live:
        out["what_next"] = ("There is already a live application; explain its status. "
                            "A new one cannot be made unless the pending one is withdrawn.")
    elif not out["window_open"]:
        out["what_next"] = "The application window has closed; only status and complaints are possible."
    elif data or doc_path:
        out["what_next"] = "Continue the draft: fill what is still missing, then review and submit."
    else:
        out["what_next"] = ("No application yet. Ask one question at a time: name as on the age proof, "
                            "date of birth, gender, marital status, village, block, bank account, IFSC, "
                            "then the age-proof document.")
    cur.close(); conn.close()
    return jsonify(out)


@api.route("/citizen/draft", methods=["GET", "PUT", "DELETE"])
@require_role("citizen")
def citizen_draft():
    mobile = g.who
    conn = portal.get_db()
    cur = conn.cursor()
    if request.method == "DELETE":
        cur.execute("DELETE FROM drafts WHERE mobile = %s", (mobile,))
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"ok": True, "message": "draft cleared; nothing was submitted"})
    data, doc_path, _ = _draft(cur, mobile)
    if request.method == "PUT":
        if portal.deadline_remaining() is None:
            cur.close(); conn.close()
            return _error(409, "window_closed", "the application window has closed")
        body = _body()
        unknown = [k for k in body if k not in DRAFT_FIELDS]
        if unknown:
            cur.close(); conn.close()
            return _error(400, "unknown_field", "not a form field: " + ", ".join(unknown))
        for k, v in body.items():
            if v is None:
                data.pop(k, None)
                continue
            v = str(v).strip()
            if k == "dob":
                v = _normalise_dob(v)
            if k == "block":
                # the form is a drop-down; accept the name however it is capitalised
                match = [b for b in portal.BLOCKS if b.lower() == v.lower()]
                v = match[0] if match else v
            if k in ("gender", "marital_status"):
                v = v.capitalize()
            if k == "ifsc":
                v = re.sub(r"[\s-]", "", v).upper()
            if k == "bank_account":
                v = re.sub(r"[\s-]", "", v)
            data[k] = v
        cur.execute("INSERT INTO drafts (mobile, data, doc_path, channel, updated_at) VALUES (%s, %s, %s, 'AGENT', %s) "
                    "ON CONFLICT (mobile) DO UPDATE SET data = EXCLUDED.data, updated_at = EXCLUDED.updated_at",
                    (mobile, json.dumps(data), doc_path or None, datetime.now()))
        conn.commit()
    out = _draft_view(cur, mobile, data, doc_path)
    cur.close(); conn.close()
    return jsonify(out)


@api.post("/citizen/draft/document")
@require_role("citizen")
def citizen_draft_document():
    """Attach the age proof as base64 (for clients that can carry a file)."""
    mobile = g.who
    body = _body()
    name = (body.get("filename") or "").strip()
    try:
        content = base64.b64decode(body.get("content_base64") or "", validate=True)
    except Exception:
        return _error(400, "bad_file", "content_base64 is not valid base64")

    class _F(io.BytesIO):
        pass
    f = _F(content)
    f.filename = name
    problem = portal.check_upload(f)
    if problem:
        return _error(400, "bad_file", problem)
    path = portal.store_upload(mobile, name, content)
    conn = portal.get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO drafts (mobile, data, doc_path, channel, updated_at) VALUES (%s, '{}'::jsonb, %s, 'AGENT', %s) "
                "ON CONFLICT (mobile) DO UPDATE SET doc_path = EXCLUDED.doc_path, updated_at = EXCLUDED.updated_at",
                (mobile, path, datetime.now()))
    conn.commit()
    data, doc_path, _ = _draft(cur, mobile)
    out = _draft_view(cur, mobile, data, doc_path)
    cur.close(); conn.close()
    return jsonify(out)


def _upload_url(cur, mobile):
    """A one-time upload link for this mobile, reusing an unused one made in the last 20
    minutes so repeated draft reads do not mint a new link each time. Commits."""
    cur.execute("SELECT token FROM upload_tokens WHERE mobile = %s AND used_at IS NULL AND created_at > %s "
                "ORDER BY created_at DESC LIMIT 1", (mobile, datetime.now() - timedelta(minutes=20)))
    row = cur.fetchone()
    token = row[0] if row else secrets.token_urlsafe(24)
    if not row:
        cur.execute("INSERT INTO upload_tokens (token, mobile, created_at) VALUES (%s, %s, %s)",
                    (token, mobile, datetime.now()))
        cur.connection.commit()
    return oauth.ISSUER + "/upload/" + token


@api.post("/citizen/draft/upload-link")
@require_role("citizen")
def citizen_upload_link():
    """A one-time link to attach the document from the phone's browser."""
    mobile = g.who
    conn = portal.get_db()
    cur = conn.cursor()
    url = _upload_url(cur, mobile)
    cur.close(); conn.close()
    return jsonify({"upload_url": url, "valid_for_minutes": 30,
                    "message": "Open this link on the phone, choose the photo or PDF of the age proof, "
                               "and press Upload. Then check the draft again."})


def _review_token(mobile, data, doc_path):
    canon = json.dumps({k: data.get(k, "") for k in DRAFT_FIELDS}, sort_keys=True) + "|" + (doc_path or "")
    return hmac.new(portal.app.secret_key.encode(), (mobile + "|" + canon).encode(), hashlib.sha256).hexdigest()[:24]


@api.post("/citizen/draft/review")
@require_role("citizen")
def citizen_review():
    """The preview step of the form: everything as it will be submitted, the problems
    if any, and a token that submit must quote, so nothing goes in unseen."""
    mobile = g.who
    conn = portal.get_db()
    cur = conn.cursor()
    data, doc_path, _ = _draft(cur, mobile)
    view = _draft_view(cur, mobile, data, doc_path)
    cur.close(); conn.close()
    out = {"preview": view["draft"], "age_proof_document": view["age_proof_document"],
           "problems": view["problems"], "can_submit": view["ready_to_review"]}
    if view["ready_to_review"]:
        out["review_token"] = _review_token(mobile, data, doc_path)
        out["declaration"] = ("I hereby declare that the information furnished above is true to the best "
                              "of my knowledge and belief. I understand that furnishing false information "
                              "is punishable under applicable law and will result in cancellation of pension.")
        out["message"] = ("Read every line of the preview back to the applicant, then read the declaration. "
                          "Submit only after they say yes to both.")
    else:
        out["message"] = "Cannot submit yet; fix the problems listed."
    return jsonify(out)


@api.post("/citizen/draft/submit")
@require_role("citizen")
def citizen_submit():
    mobile = g.who
    body = _body()
    if body.get("declaration_accepted") is not True:
        return _error(400, "declaration", "the applicant must accept the declaration (declaration_accepted: true)")
    conn = portal.get_db()
    cur = conn.cursor()
    data, doc_path, _ = _draft(cur, mobile)
    if not data and not doc_path:
        cur.close(); conn.close()
        return _error(409, "no_draft", "there is no draft to submit")
    if (body.get("review_token") or "") != _review_token(mobile, data, doc_path):
        cur.close(); conn.close()
        return _error(409, "review_first", "the draft changed since it was reviewed, or was not reviewed; "
                                           "call review again and read it back")
    errors, cleaned = portal.validate_application(data, doc_path)
    if errors:
        cur.close(); conn.close()
        return jsonify({"error": "not_valid", "problems": errors}), 422
    existing = portal.active_application(cur, mobile)
    if existing:
        cur.close(); conn.close()
        return _error(409, "already_applied", "application %s (%s) already exists for this mobile"
                      % (existing[1], existing[2]))
    new_id, app_no = portal.create_application(cur, mobile, cleaned, channel="AGENT")
    cur.execute("DELETE FROM drafts WHERE mobile = %s", (mobile,))
    conn.commit()
    cur.close(); conn.close()
    portal.send_sms(mobile, "Sewa Setu: application %s received. Track at the status portal "
                            "with mobile no. and password (DOB as DDMMYYYY)." % app_no)
    return jsonify({"ok": True, "application_no": app_no, "status": "PENDING",
                    "deemed_approval_on": portal.deemed_on(datetime.now()).isoformat(),
                    "acknowledgment_pdf": oauth.ISSUER + "/ack/%d.pdf" % new_id,
                    "message": "Submitted. An SMS with the application number %s has gone to %s. The block "
                               "officer decides within %d days; otherwise it is approved automatically. "
                               "The status portal password is the date of birth as DDMMYYYY."
                               % (app_no, mobile, portal.SLA_DAYS)}), 201


def _own_application(cur, mobile, app_no):
    cur.execute("SELECT " + APP_COLS + " FROM applications WHERE mobile = %s AND application_no = %s",
                (mobile, (app_no or "").strip().upper()))
    row = cur.fetchone()
    return _app_dict(row) if row else None


@api.get("/citizen/applications")
@require_role("citizen")
def citizen_applications():
    mobile = g.who
    conn = portal.get_db()
    cur = conn.cursor()
    cur.execute("SELECT " + APP_COLS + " FROM applications WHERE mobile = %s ORDER BY submitted_at DESC", (mobile,))
    rows = [explain(cur, _app_dict(r)) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify({"mobile": mobile, "applications": rows,
                    "message": "no application for this mobile number" if not rows else None})


@api.get("/citizen/applications/<app_no>")
@require_role("citizen")
def citizen_application(app_no):
    conn = portal.get_db()
    cur = conn.cursor()
    a = _own_application(cur, g.who, app_no)
    if not a:
        cur.close(); conn.close()
        return _error(404, "not_found", "no application %s for this mobile number" % app_no)
    out = explain(cur, a)
    out["details"] = {"applicant_name": a["applicant_name"], "dob": _d(a["dob"]), "village": a["village"],
                      "block": a["block"], "gender": a["gender"], "marital_status": a["marital_status"]}
    cur.close(); conn.close()
    return jsonify(out)


@api.post("/citizen/applications/<app_no>/withdraw")
@require_role("citizen")
def citizen_withdraw(app_no):
    mobile = g.who
    conn = portal.get_db()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, decided_by = 'APPLICANT' "
                "WHERE mobile = %s AND application_no = %s AND status = 'PENDING' RETURNING id",
                (datetime.now(), mobile, (app_no or "").strip().upper()))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return _error(409, "not_pending", "only a pending application of this mobile can be withdrawn")
    portal.write_audit(cur, row[0], "WITHDRAW", "applicant:%s" % mobile, "via assistant")
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"ok": True, "application_no": app_no.upper(), "status": "WITHDRAWN",
                    "message": "Withdrawn. A fresh application can now be made from this mobile."})


@api.post("/citizen/applications/<app_no>/grievance")
@require_role("citizen")
def citizen_grievance(app_no):
    mobile = g.who
    body = _body()
    kind = (body.get("kind") or "PAYMENT_NOT_RECEIVED").upper()
    detail = (body.get("detail") or "").strip()
    if kind not in GRIEVANCE_KINDS:
        return _error(400, "bad_kind", "kind must be one of: " + ", ".join(GRIEVANCE_KINDS))
    if len(detail) < 10:
        return _error(400, "detail", "say in a sentence what happened (at least 10 characters)")
    conn = portal.get_db()
    cur = conn.cursor()
    a = _own_application(cur, mobile, app_no)
    if not a:
        cur.close(); conn.close()
        return _error(404, "not_found", "no application %s for this mobile number" % app_no)
    if kind == "PAYMENT_NOT_RECEIVED" and a["status"] not in ("APPROVED", "DEEMED_APPROVED"):
        cur.close(); conn.close()
        return _error(409, "not_approved", "application %s is %s; pension is paid only after approval, so "
                      "there is no payment to complain about yet" % (a["application_no"], a["status"]))
    cur.execute("SELECT id FROM grievances WHERE application_id = %s AND status = 'OPEN' AND kind = %s",
                (a["id"], kind))
    dup = cur.fetchone()
    if dup:
        cur.close(); conn.close()
        return jsonify({"ok": True, "grievance_id": dup[0], "already_open": True,
                        "message": "a complaint of this kind is already open (no. %d); the officer will see it" % dup[0]})
    cur.execute("INSERT INTO grievances (application_id, mobile, kind, detail, status, channel, raised_at) "
                "VALUES (%s, %s, %s, %s, 'OPEN', 'AGENT', %s) RETURNING id",
                (a["id"], mobile, kind, detail[:1000], datetime.now()))
    gid = cur.fetchone()[0]
    portal.write_audit(cur, a["id"], "GRIEVANCE", "applicant:%s" % mobile, "%s: %s" % (kind, detail[:400]))
    conn.commit()
    cur.close(); conn.close()
    portal.send_sms(mobile, "Sewa Setu: complaint no. %d recorded against application %s. The block officer "
                            "will look into it." % (gid, a["application_no"]))
    return jsonify({"ok": True, "grievance_id": gid, "application_no": a["application_no"],
                    "bank_account_ending": _mask(a["bank_account"]), "ifsc": a["ifsc"],
                    "message": "Complaint no. %d is recorded and is in the officer's queue. Keep the number. "
                               "The pension goes to the account ending %s at IFSC %s; if that is not the "
                               "applicant's own open account, say so to the officer." % (gid, a["bank_account"][-4:], a["ifsc"])}), 201


@api.post("/citizen/applications/<app_no>/life-certificate")
@require_role("citizen")
def citizen_life_certificate(app_no):
    """The yearly 'I am alive'. The sign-in itself is the proof: the OTP went to the
    applicant's registered mobile minutes ago, the same proof the form relies on."""
    mobile = g.who
    conn = portal.get_db()
    cur = conn.cursor()
    a = _own_application(cur, mobile, app_no)
    if not a:
        cur.close(); conn.close()
        return _error(404, "not_found", "no application %s for this mobile number" % app_no)
    if a["status"] not in ("APPROVED", "DEEMED_APPROVED"):
        cur.close(); conn.close()
        return _error(409, "not_approved", "a life certificate applies only to an approved pension")
    now = datetime.now()
    due = portal.a_year_from(now)
    cur.execute("UPDATE applications SET life_certified_at = %s, life_certificate_due = %s WHERE id = %s",
                (now, due, a["id"]))
    portal.write_audit(cur, a["id"], "LIFE_CERT", "applicant:%s" % mobile, "confirmed alive via assistant (OTP sign-in)")
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"ok": True, "confirmed_on": _d(now), "next_due": due.isoformat(),
                    "message": "Recorded. The next yearly confirmation is due by %s." % due.strftime("%d %B %Y")})


# ---------------------------------------------------------------------------
# officer
# ---------------------------------------------------------------------------

def _flags(cur, a):
    """Things an officer would want pointed out before deciding. Facts, not verdicts."""
    flags = []
    now = datetime.now()
    if a["status"] == "PENDING":
        days = (now - a["submitted_at"]).days
        if days > portal.SLA_DAYS:
            flags.append("beyond the %d-day limit: will be deemed approved in the next nightly run" % portal.SLA_DAYS)
        elif days >= portal.SLA_DAYS - 3:
            flags.append("deemed approval on %s unless decided" % portal.deemed_on(a["submitted_at"]).strftime("%d %b"))
    if a["dob"] and portal.age_on(a["dob"], a["submitted_at"].date()) < portal.MIN_AGE:
        flags.append("age on the day of applying was under %d" % portal.MIN_AGE)
    if not a["doc_path"]:
        flags.append("no age-proof file in the portal (counter applications keep the paper copy)")
    elif not os.path.exists(a["doc_path"]):
        cur.execute("SELECT 1 FROM documents WHERE path = %s", (a["doc_path"],))
        if not cur.fetchone():
            flags.append("age-proof file is recorded but missing from storage")
    cur.execute("SELECT application_no, status FROM applications WHERE mobile = %s AND id <> %s "
                "AND status IN %s", (a["mobile"], a["id"], portal.ACTIVE_STATUSES))
    for no, st in cur.fetchall():
        flags.append("same mobile has another live application %s (%s)" % (no, st))
    cur.execute("SELECT application_no, applicant_name, status FROM applications WHERE bank_account = %s "
                "AND id <> %s AND status IN %s LIMIT 3", (a["bank_account"], a["id"], portal.ACTIVE_STATUSES))
    for no, name, st in cur.fetchall():
        flags.append("same bank account as %s (%s, %s)" % (no, name, st))
    if a["marital_status"] == "Widowed" and not a["husband_name"]:
        flags.append("widowed, late husband's name not given (optional on the form)")
    if a.get("channel") == "AGENT":
        flags.append("filed through an AI assistant; the applicant verified the mobile by OTP")
    return flags


def _queue_row(cur, a):
    now = datetime.now()
    due = portal.deemed_on(a["submitted_at"])
    return {"application_no": a["application_no"], "applicant_name": a["applicant_name"],
            "age": portal.age_on(a["dob"], now.date()) if a["dob"] else None, "village": a["village"],
            "block": a["block"], "submitted_on": _d(a["submitted_at"]), "days_pending": (now - a["submitted_at"]).days,
            "deemed_approval_on": due.isoformat(), "days_left": (due - now.date()).days,
            "channel": a.get("channel") or "WEB", "flags": _flags(cur, a)}


@api.get("/officer/summary")
@require_role("officer")
def officer_summary():
    conn = portal.get_db()
    cur = conn.cursor()
    now = datetime.now()
    cur.execute("SELECT status, count(*) FROM applications GROUP BY status")
    by_status = dict(cur.fetchall())
    cur.execute("SELECT block, count(*) FROM applications WHERE status = 'PENDING' GROUP BY block ORDER BY 2 DESC")
    by_block = dict(cur.fetchall())
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' AND submitted_at < %s",
                (now - timedelta(days=portal.SLA_DAYS),))
    overdue = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' AND submitted_at < %s",
                (now - timedelta(days=portal.SLA_DAYS - 7),))
    deeming_this_week = cur.fetchone()[0]
    cur.execute("SELECT min(submitted_at) FROM applications WHERE status = 'PENDING'")
    oldest = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM grievances WHERE status = 'OPEN'")
    open_g = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' AND channel = 'AGENT'")
    via_agent = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM applications WHERE decided_by = %s AND decided_at::date = %s", (g.who, now.date()))
    today = cur.fetchone()[0]
    cur.close(); conn.close()
    pending = by_status.get("PENDING", 0)
    return jsonify({"officer": g.who, "pending": pending, "pending_by_block": by_block,
                    "overdue_beyond_sla": overdue, "will_be_deemed_approved_within_7_days": deeming_this_week,
                    "oldest_pending_since": _d(oldest), "pending_filed_via_assistant": via_agent,
                    "open_complaints": open_g, "decided_by_you_today": today, "all_time_by_status": by_status,
                    "in_plain_words": "%d applications are waiting. %d of them will be approved automatically "
                                      "within 7 days if nobody decides (%d already past the limit). %d complaints "
                                      "are open." % (pending, deeming_this_week, overdue, open_g)})


@api.get("/officer/queue")
@require_role("officer")
def officer_queue():
    block = (request.args.get("block") or "").strip()
    limit = max(1, min(int(request.args.get("limit") or 10), 50))
    only_agent = request.args.get("channel") == "AGENT"
    conn = portal.get_db()
    cur = conn.cursor()
    sql = "SELECT " + APP_COLS + " FROM applications WHERE status = 'PENDING'"
    args = []
    if block:
        match = [b for b in portal.BLOCKS if b.lower() == block.lower()]
        if not match:
            cur.close(); conn.close()
            return _error(400, "bad_block", "block must be one of: " + ", ".join(portal.BLOCKS))
        sql += " AND block = %s"
        args.append(match[0])
    if only_agent:
        sql += " AND channel = 'AGENT'"
    sql += " ORDER BY submitted_at ASC LIMIT %s"
    args.append(limit)
    cur.execute(sql, args)
    rows = [_queue_row(cur, _app_dict(r)) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify({"block": block or "all", "oldest_first": True, "count": len(rows), "queue": rows})


def _case(cur, app_no):
    cur.execute("SELECT " + APP_COLS + " FROM applications WHERE application_no = %s", ((app_no or "").strip().upper(),))
    row = cur.fetchone()
    return _app_dict(row) if row else None


@api.get("/officer/case/<app_no>")
@require_role("officer")
def officer_case(app_no):
    conn = portal.get_db()
    cur = conn.cursor()
    a = _case(cur, app_no)
    if not a:
        cur.close(); conn.close()
        return _error(404, "not_found", "no application %s" % app_no)
    cur.execute("SELECT at, action, actor, note FROM audit_log WHERE application_id = %s ORDER BY at ASC", (a["id"],))
    audit = [{"at": _d(r[0]), "action": r[1], "by": r[2], "note": r[3]} for r in cur.fetchall()]
    out = {k: _d(v) for k, v in a.items() if k not in ("id", "doc_path")}
    out["age_now"] = portal.age_on(a["dob"], datetime.now().date()) if a["dob"] else None
    out["age_when_applied"] = portal.age_on(a["dob"], a["submitted_at"].date()) if a["dob"] else None
    out["age_proof_document"] = os.path.basename(a["doc_path"]) if a["doc_path"] else None
    out["flags"] = _flags(cur, a)
    out["audit_trail"] = audit
    out["complaints"] = _grievances(cur, a["id"])
    if a["status"] == "PENDING":
        out["deemed_approval_on"] = portal.deemed_on(a["submitted_at"]).isoformat()
        out["days_pending"] = (datetime.now() - a["submitted_at"]).days
    cur.close(); conn.close()
    return jsonify(out)


@api.get("/officer/case/<app_no>/document")
@require_role("officer")
def officer_case_document(app_no):
    conn = portal.get_db()
    cur = conn.cursor()
    a = _case(cur, app_no)
    data = portal.read_document(cur, a["doc_path"]) if a else None
    cur.close(); conn.close()
    if not a:
        return _error(404, "not_found", "no application %s" % app_no)
    if data is None:
        return _error(404, "no_document", "no age-proof file is stored for %s" % a["application_no"])
    ext = a["doc_path"].rsplit(".", 1)[-1].lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "pdf": "application/pdf"}.get(ext, "application/octet-stream")
    return jsonify({"filename": os.path.basename(a["doc_path"]), "mime": mime, "bytes": len(data),
                    "content_base64": base64.b64encode(data).decode("ascii")})


@api.post("/officer/case/<app_no>/decision")
@require_role("officer")
def officer_decision(app_no):
    body = _body()
    decision = (body.get("decision") or "").strip().lower()
    note = (body.get("reason") or body.get("note") or "").strip()
    if decision not in ("approve", "reject"):
        return _error(400, "bad_decision", "decision must be approve or reject")
    new_status = "APPROVED" if decision == "approve" else "REJECTED"
    conn = portal.get_db()
    cur = conn.cursor()
    a = _case(cur, app_no)
    if not a:
        cur.close(); conn.close()
        return _error(404, "not_found", "no application %s" % app_no)
    try:
        row = portal.decide_application(cur, a["id"], new_status, g.who, note)
    except ValueError as e:
        cur.close(); conn.close()
        return _error(400, "reason_required", str(e))
    if not row:
        cur.close(); conn.close()
        return _error(409, "not_pending", "application %s is %s, not pending" % (a["application_no"], a["status"]))
    conn.commit()
    a = _case(cur, app_no)
    cur.close(); conn.close()
    portal.notify_decision(row[0], row[1], new_status, note)
    out = {"ok": True, "application_no": row[0], "status": new_status, "decided_by": g.who,
           "decided_at": _d(a["decided_at"]), "reason_recorded": note or None,
           "sms_sent_to": row[1]}
    if new_status == "APPROVED":
        out["effective_from"] = _d(a["effective_from"])
        out["life_certificate_due"] = _d(a["life_certificate_due"])
        out["message"] = ("Approved in the name of %s. Pension runs from %s (the month of application). "
                          "The applicant has been told by SMS." % (g.who, a["effective_from"].strftime("%B %Y")))
    else:
        out["message"] = "Rejected in the name of %s with the reason recorded. The applicant has been told by SMS." % g.who
    return jsonify(out)


@api.get("/officer/search")
@require_role("officer")
def officer_search():
    mobile = (request.args.get("mobile") or "").strip()
    name = (request.args.get("name") or "").strip()
    app_no = (request.args.get("application_no") or "").strip().upper()
    if not (mobile or name or app_no):
        return _error(400, "empty", "give a mobile, a name or an application number")
    conn = portal.get_db()
    cur = conn.cursor()
    if app_no:
        cur.execute("SELECT " + APP_COLS + " FROM applications WHERE application_no = %s", (app_no,))
    elif mobile:
        cur.execute("SELECT " + APP_COLS + " FROM applications WHERE mobile = %s ORDER BY submitted_at DESC", (mobile,))
    else:
        cur.execute("SELECT " + APP_COLS + " FROM applications WHERE applicant_name ILIKE %s "
                    "ORDER BY submitted_at DESC LIMIT 20", ("%" + name + "%",))
    rows = []
    for r in cur.fetchall():
        a = _app_dict(r)
        rows.append({"application_no": a["application_no"], "applicant_name": a["applicant_name"], "mobile": a["mobile"],
                     "village": a["village"], "block": a["block"], "status": a["status"],
                     "submitted_on": _d(a["submitted_at"]), "decided_by": a["decided_by"], "channel": a.get("channel")})
    cur.close(); conn.close()
    return jsonify({"count": len(rows), "results": rows})


@api.get("/officer/grievances")
@require_role("officer")
def officer_grievances():
    limit = max(1, min(int(request.args.get("limit") or 10), 50))
    status = (request.args.get("status") or "OPEN").upper()
    conn = portal.get_db()
    cur = conn.cursor()
    cur.execute("SELECT g.id, g.kind, g.detail, g.status, g.raised_at, a.application_no, a.applicant_name, a.mobile, "
                "a.block, a.status, a.bank_account, a.ifsc, a.decided_at FROM grievances g JOIN applications a "
                "ON a.id = g.application_id WHERE g.status = %s ORDER BY g.raised_at ASC LIMIT %s", (status, limit))
    rows = [{"grievance_id": r[0], "kind": r[1], "detail": r[2], "status": r[3], "raised_at": _d(r[4]),
             "application_no": r[5], "applicant_name": r[6], "mobile": r[7], "block": r[8],
             "application_status": r[9], "bank_account": r[10], "ifsc": r[11], "approved_on": _d(r[12])}
            for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify({"count": len(rows), "grievances": rows})


@api.post("/officer/grievances/<int:gid>/resolve")
@require_role("officer")
def officer_resolve(gid):
    resolution = (_body().get("resolution") or "").strip()
    conn = portal.get_db()
    cur = conn.cursor()
    app_id = portal.resolve_grievance(cur, gid, g.who, resolution)
    if app_id is None:
        cur.close(); conn.close()
        return _error(409, "not_resolved", "grievance %d is not open, or the resolution is shorter than %d characters"
                      % (gid, portal.REJECT_REASON_MIN))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"ok": True, "grievance_id": gid, "resolved_by": g.who,
                    "message": "Closed in the name of %s; the applicant has been told by SMS." % g.who})
