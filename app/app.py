# -*- coding: utf-8 -*-
"""
Sewa Setu - Old Age Pension Portal
Government of Purvanchal, Department of Social Welfare

Developed by: Netlink Infosolutions Pvt Ltd (2023)
Maintained in-house since 02/2026.
"""

import os
import io
import re
import random
import hashlib
import configparser
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist():
    """The portal's clock is Indian Standard Time regardless of where the server runs."""
    return datetime.now(IST).replace(tzinfo=None)

import requests
import psycopg2
from flask import (Flask, request, session, redirect, url_for, render_template,
                   flash, send_file, abort)
from fpdf import FPDF

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, "config", "app.ini"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or config.get("app", "secret_key")
# Two hours: elderly applicants fill the form slowly; the draft lives in the session.
app.config["PERMANENT_SESSION_LIFETIME"] = 7200

# Google Search Console ownership tag, so a Safe Browsing review can be requested
app.config["GOOGLE_SITE_VERIFICATION"] = os.environ.get("GOOGLE_SITE_VERIFICATION", "")

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# An applicant may hold at most one application in these states at a time.
# WITHDRAWN and REJECTED applications do not block a fresh application.
ACTIVE_STATUSES = ("PENDING", "APPROVED", "DEEMED_APPROVED")

SMS_GATEWAY_URL = os.environ.get("SMS_GATEWAY_URL") or config.get("app", "sms_gateway_url")
# A hosted Postgres (e.g. Neon) is given as one URL; otherwise the [database] section.
DATABASE_URL = os.environ.get("DATABASE_URL")
OTP_VALIDITY_SECONDS = config.getint("app", "otp_validity_seconds")
UPLOAD_DIR = config.get("app", "upload_dir")
SCHEME_DEADLINE = datetime.strptime(config.get("pension", "scheme_deadline"),
                                    "%Y-%m-%d %H:%M")
MIN_AGE = config.getint("pension", "min_age")
SLA_DAYS = config.getint("pension", "sla_days")

BLOCKS = ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]

import logging
_logdir = "/var/log/sewasetu"
try:
    os.makedirs(_logdir, exist_ok=True)
    _fh = logging.FileHandler(os.path.join(_logdir, "sewasetu-app.log"))
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s in app: %(message)s"))
    app.logger.addHandler(_fh)
    app.logger.setLevel(logging.INFO)
except Exception:
    pass


def get_db():
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL)
    return psycopg2.connect(
        host=config.get("database", "host"),
        port=config.get("database", "port"),
        dbname=config.get("database", "name"),
        user=config.get("database", "user"),
        password=config.get("database", "password"),
    )


def sanitize(value, maxlen=100):
    # Trim whitespace and cap at the column width, counting characters (not
    # bytes) so Assamese/Bangla names are stored intact.
    if value is None:
        return ""
    return value.strip()[:maxlen]


def hash_password(p):
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def send_sms(mobile, text):
    try:
        requests.post(SMS_GATEWAY_URL + "/api/send",
                      json={"to": mobile, "text": text}, timeout=5)
    except Exception as e:
        app.logger.error("sms gateway error: %s" % e)


def deadline_remaining():
    delta = SCHEME_DEADLINE - now_ist()
    if delta.total_seconds() <= 0:
        return None
    return int(delta.total_seconds() // 3600)


def new_application_no():
    return "SSP" + datetime.now().strftime("%y") + str(random.randint(100000, 999999))


def write_audit(cur, app_id, action, actor, note=""):
    cur.execute("INSERT INTO audit_log (application_id, action, actor, note, at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (app_id, action, actor, note, datetime.now()))


def apply_migrations():
    """Bring the database up to the shape in migrations.sql. Idempotent; the four
    gunicorn workers race here, so it runs under a transaction-level advisory lock."""
    path = os.path.join(BASE_DIR, "migrations.sql")
    try:
        with open(path) as fh:
            sql = fh.read()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_xact_lock(7331)")
        cur.execute(sql)
        conn.commit()
        cur.close(); conn.close()
    except Exception as e:
        app.logger.error("migrations: %s" % e)


# ---------------------------------------------------------------------------
# Decisions: the single place an application changes from PENDING to a decided
# state, used by the web admin pages, the officer API and (for its vocabulary)
# the nightly deemed-approval job. Records who decided and why, and derives the
# dates that follow from an approval.
# ---------------------------------------------------------------------------

PENSION_PER_MONTH = 250
REJECT_REASON_MIN = 10   # characters; "no" is not a reason an applicant can act on
LIFE_CERT_MONTHS = 12


def deemed_on(submitted_at):
    """The day a still-pending application stands approved under the RTPS Act."""
    return (submitted_at + timedelta(days=SLA_DAYS)).date()


def effective_from_for(submitted_at):
    """Pension runs from the first day of the month the application was made.
    (Setting it to the month of the decision instead is how applicants lose arrears.)"""
    return submitted_at.date().replace(day=1)


def decide_application(cur, app_id, new_status, actor, note=""):
    """Approve or reject a PENDING application. Returns (application_no, mobile)
    or None if it was not pending. Raises ValueError when a rejection has no usable
    reason. The caller commits and then calls notify_decision()."""
    note = (note or "").strip()
    if new_status == "REJECTED" and len(note) < REJECT_REASON_MIN:
        raise ValueError("a rejection needs a reason the applicant can act on "
                         "(at least %d characters)" % REJECT_REASON_MIN)
    now = datetime.now()
    if new_status in ("APPROVED", "DEEMED_APPROVED"):
        cur.execute("UPDATE applications SET status = %s, decided_at = %s, decided_by = %s, "
                    "effective_from = date_trunc('month', submitted_at)::date, "
                    "life_certificate_due = %s "
                    "WHERE id = %s AND status = 'PENDING' RETURNING application_no, mobile",
                    (new_status, now, actor, (now + timedelta(days=30 * LIFE_CERT_MONTHS)).date(),
                     app_id))
    else:
        cur.execute("UPDATE applications SET status = %s, decided_at = %s, decided_by = %s "
                    "WHERE id = %s AND status = 'PENDING' RETURNING application_no, mobile",
                    (new_status, now, actor, app_id))
    row = cur.fetchone()
    if row:
        write_audit(cur, app_id, new_status, "admin:%s" % actor, note)
    return row


def notify_decision(app_no, mobile, new_status, note=""):
    if new_status == "APPROVED":
        send_sms(mobile, "Sewa Setu: application %s is APPROVED. Pension of Rs %d/month will be "
                         "credited to your bank account by DBT." % (app_no, PENSION_PER_MONTH))
    elif new_status == "REJECTED":
        send_sms(mobile, "Sewa Setu: application %s is REJECTED. Reason: %s. You may apply "
                         "afresh after correcting this." % (app_no, note[:120]))


# ---------------------------------------------------------------------------
# Scheme rules: the single source of truth for what an application must satisfy.
# Every channel that creates an application (the web form, any API, any batch
# import) MUST go through validate_application() and create_application() so
# that no channel admits an application another channel would refuse.
# ---------------------------------------------------------------------------

def parse_dob(raw):
    try:
        return datetime.strptime((raw or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def age_on(dob, on):
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def validate_application(data, doc_path):
    """Validate a complete application. Returns (errors, cleaned).

    `errors` is a list of plain-language problems (empty when valid);
    `cleaned` holds the normalised values ready for create_application().
    """
    errors = []
    cleaned = {
        "applicant_name": sanitize(data.get("applicant_name")),
        "village": (data.get("village") or "").strip(),
        "block": (data.get("block") or "").strip(),
        "gender": data.get("gender") or "",
        "marital_status": data.get("marital_status") or "",
        "husband_name": (data.get("husband_name") or "").strip(),
        "husband_employer": "",  # no longer collected (not a scheme requirement)
        "bank_account": re.sub(r"[\s-]", "", data.get("bank_account") or ""),
        "ifsc": re.sub(r"[\s-]", "", (data.get("ifsc") or "")).upper(),
        "doc_path": doc_path or "",
        "dob": None,
    }
    for label, key in (("full name", "applicant_name"), ("village", "village"),
                       ("block", "block"), ("bank account", "bank_account")):
        if not cleaned[key]:
            errors.append("%s is required" % label)
    if cleaned["block"] and cleaned["block"] not in BLOCKS:
        errors.append("block must be one of: " + ", ".join(BLOCKS))
    if cleaned["bank_account"] and not re.fullmatch(r"\d{9,18}", cleaned["bank_account"]):
        errors.append("bank account number must be 9 to 18 digits")
    if cleaned["ifsc"] and not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", cleaned["ifsc"]):
        errors.append("IFSC must be 11 characters, e.g. SBIN0003077")
    dob = parse_dob(data.get("dob"))
    if dob is None:
        errors.append("date of birth must be given as DD/MM/YYYY")
    else:
        cleaned["dob"] = dob
        if age_on(dob, now_ist().date()) < MIN_AGE:
            errors.append("applicant must be %d years of age or above" % MIN_AGE)
    if not cleaned["doc_path"]:
        errors.append("age proof document is required")
    if now_ist() > SCHEME_DEADLINE:
        errors.append("the application window has closed")
    return errors, cleaned


def active_application(cur, mobile):
    """The applicant's current live application, if any."""
    cur.execute("SELECT id, application_no, status FROM applications "
                "WHERE mobile = %s AND status IN %s "
                "ORDER BY submitted_at DESC LIMIT 1", (mobile, ACTIVE_STATUSES))
    return cur.fetchone()


def create_application(cur, mobile, cleaned, channel="WEB"):
    """Insert a validated application; ensure the status-portal account exists.

    The caller commits. Returns (id, application_no).
    """
    # SSP + year + 6 random digits: with ~100k numbers already issued a fresh draw hits
    # an existing one about one time in nine, so draw until it is unused (the unique
    # index in migrations.sql is the backstop).
    while True:
        app_no = new_application_no()
        cur.execute("SELECT 1 FROM applications WHERE application_no = %s", (app_no,))
        if not cur.fetchone():
            break
    cur.execute(
        """INSERT INTO applications
           (application_no, applicant_name, mobile, dob, gender, marital_status,
            husband_name, husband_employer, village, block, bank_account, ifsc,
            doc_path, status, submitted_at, channel)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s,%s)
           RETURNING id""",
        (app_no, cleaned["applicant_name"], mobile, cleaned["dob"], cleaned["gender"],
         cleaned["marital_status"], cleaned["husband_name"], cleaned["husband_employer"],
         cleaned["village"], cleaned["block"], cleaned["bank_account"], cleaned["ifsc"],
         cleaned["doc_path"], datetime.now(), channel))
    new_id = cur.fetchone()[0]

    # status portal account; password is DOB as DDMMYYYY per dept. circular
    portal_pass = cleaned["dob"].strftime("%d%m%Y")
    cur.execute("SELECT count(*) FROM portal_users WHERE mobile = %s", (mobile,))
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO portal_users (mobile, password_hash) VALUES (%s,%s)",
                    (mobile, hash_password(portal_pass)))
    write_audit(cur, new_id, "SUBMIT", "applicant:%s" % mobile)
    return new_id, app_no


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", hours_left=deadline_remaining())


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/__gateway/", defaults={"subpath": ""})
@app.route("/__gateway/<path:subpath>")
def gateway_proxy(subpath):
    # Convenience proxy to the internal SMS gateway, so the OTP inbox is reachable
    # on the main site without opening a second port on the host. The gateway
    # itself runs only on the internal network.
    try:
        r = requests.get(SMS_GATEWAY_URL + "/" + subpath,
                         params=request.args, timeout=5)
        return (r.content, r.status_code,
                {"Content-Type": r.headers.get("Content-Type", "text/html")})
    except Exception as e:
        return ("SMS gateway unreachable: %s" % e, 502)


# ---------------------------------------------------------------------------
# Application flow: mobile -> OTP -> form steps -> upload -> declaration
# ---------------------------------------------------------------------------

@app.route("/apply", methods=["GET", "POST"])
def apply():
    if deadline_remaining() is None:
        flash("The application window for this scheme has closed.")
        return redirect(url_for("index"))
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        captcha = request.form.get("captcha", "")
        try:
            captcha_ok = int(captcha) == session.get("captcha_answer")
        except (ValueError, TypeError):
            captcha_ok = False
        if not captcha_ok:
            flash("Security check answer is incorrect. Please try again.")
            return render_template("apply.html", captcha_q=make_captcha())
        if len(mobile) != 10 or not mobile.isdigit():
            flash("Please enter a valid 10-digit mobile number.")
            return render_template("apply.html", captcha_q=make_captcha())
        code = str(random.randint(100000, 999999))
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
                    (mobile, code, datetime.now()))
        conn.commit()
        cur.close(); conn.close()
        send_sms(mobile, "Your Sewa Setu OTP is %s. Valid for 5 minutes." % code)
        session.permanent = True
        session["apply_mobile"] = mobile
        return redirect(url_for("verify"))
    return render_template("apply.html", captcha_q=make_captcha())


def make_captcha():
    a, b = random.randint(1, 9), random.randint(1, 9)
    session["captcha_answer"] = a + b
    return "%d + %d" % (a, b)


@app.route("/verify", methods=["GET", "POST"])
def verify():
    mobile = session.get("apply_mobile")
    if not mobile:
        return redirect(url_for("apply"))
    if request.method == "POST":
        code = request.form.get("otp", "").strip()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT code, created_at FROM otps WHERE mobile = %s "
                    "ORDER BY id DESC LIMIT 1", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == code:
            age = (datetime.now() - row[1]).total_seconds()
            if age > OTP_VALIDITY_SECONDS:
                app.logger.warning("otp expired mobile=%s age=%ds" % (mobile, int(age)))
                flash("OTP expired. Please request a new OTP.")
                return redirect(url_for("apply"))
            session["verified_mobile"] = mobile
            return redirect(url_for("form_step", step=1))
        flash("Invalid OTP.")
    return render_template("verify.html", mobile=mobile)


@app.route("/form/<int:step>", methods=["GET", "POST"])
def form_step(step):
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if step not in (1, 2, 3):
        abort(404)
    if request.method == "POST":
        data = session.get("form_data", {})
        for k, v in request.form.items():
            data[k] = v
        session["form_data"] = data
        if step < 3:
            return redirect(url_for("form_step", step=step + 1))
        return redirect(url_for("upload"))
    return render_template("form_step%d.html" % step,
                           data=session.get("form_data", {}), blocks=BLOCKS)


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        f = request.files.get("document")
        if f is None or f.filename == "":
            flash("Please choose a file to upload (JPG, PNG or PDF, up to 5 MB).")
            return render_template("upload.html")
        filename = f.filename.lower()
        content = f.read()
        if not filename.rsplit(".", 1)[-1] in ("pdf", "jpg", "jpeg", "png"):
            flash("Please upload a JPG, PNG or PDF file.")
            return render_template("upload.html")
        if len(content) > 5 * 1024 * 1024:
            flash("File is larger than 5 MB. Please upload a smaller file.")
            return render_template("upload.html")
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        path = os.path.join(UPLOAD_DIR, "%s_%s" % (session["verified_mobile"], filename))
        with open(path, "wb") as out:
            out.write(content)
        session["doc_path"] = path
        return redirect(url_for("declaration"))
    return render_template("upload.html")


@app.route("/declaration", methods=["GET", "POST"])
def declaration():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        return handle_submission()
    # Preview everything before the applicant commits.
    data = session.get("form_data", {})
    errors, _ = validate_application(data, session.get("doc_path", ""))
    return render_template("declaration.html", data=data, errors=errors,
                           doc_name=os.path.basename(session.get("doc_path", "") or ""))


def handle_submission():
    mobile = session.get("verified_mobile")
    data = session.get("form_data", {})
    errors, cleaned = validate_application(data, session.get("doc_path", ""))
    if errors:
        flash("Please correct the following before submitting: " + "; ".join(errors) + ".")
        return redirect(url_for("form_step", step=1))

    conn = get_db()
    cur = conn.cursor()
    existing = active_application(cur, mobile)
    if existing:
        cur.close(); conn.close()
        flash("Application %s (%s) already exists for this mobile number. A pending "
              "application can be withdrawn from the status portal if you need to "
              "apply afresh." % (existing[1], existing[2]))
        return redirect(url_for("index"))

    new_id, app_no = create_application(cur, mobile, cleaned)
    conn.commit()
    cur.close(); conn.close()

    session.pop("form_data", None)
    session.pop("doc_path", None)
    session.pop("verified_mobile", None)
    # The applicant just proved ownership of this mobile by OTP, so let them
    # see their application and download the acknowledgment without a second login.
    session["logged_in"] = True
    session["portal_mobile"] = mobile
    send_sms(mobile, "Sewa Setu: application %s received. Track at the status portal "
                     "with mobile no. and password (DOB as DDMMYYYY)." % app_no)
    return render_template("confirmation.html", app_no=app_no, app_id=new_id)


def generate_acknowledgment(cur, app_id):
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, submitted_at, status FROM applications WHERE id = %s",
                (app_id,))
    row = cur.fetchone()
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "GOVERNMENT OF PURVANCHAL", ln=1, align="C")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, "Department of Social Welfare", ln=1, align="C")
    pdf.cell(0, 8, "Old Age Pension Scheme - Acknowledgment", ln=1, align="C")
    pdf.ln(4)
    # letterhead rules
    pdf.line(10, 12, 200, 12)
    pdf.line(10, 282, 200, 282)
    labels = ["Application No", "Applicant Name", "Mobile", "Date of Birth",
              "Village", "Block", "Bank Account", "IFSC", "Submitted At", "Status"]
    pdf.set_font("Helvetica", "", 10)
    for label, val in zip(labels, row):
        try:
            pdf.cell(60, 8, label, border=1)
            pdf.cell(0, 8, str(val), border=1, ln=1)
        except Exception:
            pdf.cell(0, 8, "?", border=1, ln=1)
    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 5, "This is a computer generated acknowledgment. Processing SLA "
                         "as per the Purvanchal Right to Public Services Act applies.")
    return bytes(pdf.output())


# ---------------------------------------------------------------------------
# Status portal (citizen login)
# ---------------------------------------------------------------------------

@app.route("/status", methods=["GET", "POST"])
def status_login():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == hash_password(password):
            session["logged_in"] = True
            session["portal_mobile"] = mobile
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id FROM applications WHERE mobile = %s "
                        "ORDER BY submitted_at DESC LIMIT 1", (mobile,))
            r = cur.fetchone()
            cur.close(); conn.close()
            if r:
                return redirect(url_for("view_application", app_id=r[0]))
            flash("No application found for this mobile number.")
            return redirect(url_for("status_login"))
        flash("Mobile number or password is incorrect. The password is your date of "
              "birth as DDMMYYYY.")
    return render_template("status_login.html")


@app.route("/application/<int:app_id>")
def view_application(app_id):
    if not session.get("logged_in"):
        flash("Please login to view application status.")
        return redirect(url_for("status_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, status, submitted_at, decided_at, channel, "
                "effective_from, life_certificate_due "
                "FROM applications WHERE id = %s AND mobile = %s",
                (app_id, session.get("portal_mobile")))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        abort(404)
    cur.execute("SELECT id, kind, detail, status, raised_at, resolution FROM grievances "
                "WHERE application_id = %s ORDER BY raised_at DESC", (app_id,))
    grievances = cur.fetchall()
    cur.execute("SELECT note FROM audit_log WHERE application_id = %s AND action = 'REJECTED' "
                "ORDER BY at DESC LIMIT 1", (app_id,))
    r = cur.fetchone()
    cur.close(); conn.close()
    return render_template("application.html", a=row, app_id=app_id, grievances=grievances,
                           deemed=deemed_on(row[9]) if row[9] else None,
                           reason=(r[0] if r and r[0] else None))


@app.route("/application/<int:app_id>/withdraw", methods=["POST"])
def withdraw_application(app_id):
    """Applicant withdraws their own PENDING application so they can apply afresh."""
    if not session.get("logged_in") or not session.get("portal_mobile"):
        flash("Please login to manage your application.")
        return redirect(url_for("status_login"))
    mobile = session["portal_mobile"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, "
                "decided_by = 'APPLICANT' WHERE id = %s AND mobile = %s "
                "AND status = 'PENDING' RETURNING application_no",
                (datetime.now(), app_id, mobile))
    row = cur.fetchone()
    if row:
        write_audit(cur, app_id, "WITHDRAW", "applicant:%s" % mobile)
        conn.commit()
        flash("Application %s has been withdrawn. You may submit a new application." % row[0])
    else:
        flash("Only a pending application can be withdrawn.")
    cur.close(); conn.close()
    return redirect(url_for("view_application", app_id=app_id))


@app.route("/ack/<int:app_id>.pdf")
def ack_pdf(app_id):
    if not session.get("admin"):
        conn0 = get_db(); c0 = conn0.cursor()
        c0.execute("SELECT 1 FROM applications WHERE id = %s AND mobile = %s",
                   (app_id, session.get("portal_mobile")))
        owned = c0.fetchone()
        c0.close(); conn0.close()
        if not owned:
            abort(403)
    path = os.path.join(UPLOAD_DIR, "ack", "%d.pdf" % app_id)
    if not os.path.exists(path):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM applications WHERE id = %s", (app_id,))
        if not cur.fetchone():
            cur.close(); conn.close()
            abort(404)
        data = generate_acknowledgment(cur, app_id)
        cur.close(); conn.close()
        return send_file(io.BytesIO(data), mimetype="application/pdf")
    return send_file(path, mimetype="application/pdf")


@app.route("/status/reset", methods=["GET", "POST"])
def reset_password():
    if request.method == "POST":
        mobile = request.form.get("mobile", "")
        conn = get_db()
        cur = conn.cursor()
        # fetch account for reset
        cur.execute("SELECT mobile FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        if row:
            cur.execute("SELECT dob FROM applications WHERE mobile = %s LIMIT 1", (mobile,))
            r2 = cur.fetchone()
            if r2:
                newpass = r2[0].strftime("%d%m%Y")
                cur.execute("UPDATE portal_users SET password_hash = %s WHERE mobile = %s",
                            (hash_password(newpass), row[0]))
                conn.commit()
                send_sms(row[0], "Sewa Setu: your password has been reset to your "
                                 "date of birth (DDMMYYYY).")
        cur.close(); conn.close()
        flash("If the mobile number exists, the password has been reset and sent by SMS.")
    return render_template("reset.html")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if (ADMIN_PASSWORD and request.form.get("username") == ADMIN_USERNAME and
                request.form.get("password") == ADMIN_PASSWORD):
            session["logged_in"] = True
            session["admin"] = True
            session["admin_user"] = ADMIN_USERNAME
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials.")
    return render_template("admin_login.html")


@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, count(*) FROM applications GROUP BY status")
    by_status = cur.fetchall()
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' "
                "AND submitted_at < %s", (datetime.now() - timedelta(days=SLA_DAYS),))
    overdue = cur.fetchone()[0]
    cur.execute("SELECT block, count(*) FROM applications WHERE status = 'PENDING' "
                "GROUP BY block ORDER BY count(*) DESC")
    by_block = cur.fetchall()
    cur.execute("SELECT count(*) FROM grievances WHERE status = 'OPEN'")
    open_complaints = cur.fetchone()[0]
    cur.close(); conn.close()
    return render_template("admin_dashboard.html", by_status=by_status,
                           overdue=overdue, by_block=by_block, sla=SLA_DAYS,
                           open_complaints=open_complaints)


@app.route("/admin/applications")
def admin_list():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    status = request.args.get("status", "PENDING")
    mobile = request.args.get("mobile", "").strip()
    page = int(request.args.get("page", 1))
    conn = get_db()
    cur = conn.cursor()
    if mobile:
        cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                    "submitted_at FROM applications WHERE mobile = %s ORDER BY submitted_at ASC",
                    (mobile,))
        status = "mobile %s" % mobile
    else:
        cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                    "submitted_at FROM applications WHERE status = %s "
                    "ORDER BY submitted_at ASC LIMIT 50 OFFSET %s",
                    (status, (page - 1) * 50))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_list.html", rows=rows, status=status, page=page, mobile=mobile)


@app.route("/admin/application/<int:app_id>")
def admin_view(app_id):
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, gender, "
                "marital_status, village, block, bank_account, ifsc, doc_path, "
                "status, submitted_at, decided_at, decided_by, channel, effective_from, "
                "life_certificate_due "
                "FROM applications WHERE id = %s", (app_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        abort(404)
    cur.execute("SELECT at, action, actor, note FROM audit_log "
                "WHERE application_id = %s ORDER BY at ASC", (app_id,))
    audit = cur.fetchall()
    cur.execute("SELECT id, kind, detail, status, raised_at, resolved_by, resolution "
                "FROM grievances WHERE application_id = %s ORDER BY raised_at DESC", (app_id,))
    grievances = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_view.html", a=row, app_id=app_id, audit=audit,
                           grievances=grievances)


def _admin_decide(app_id, new_status, note):
    """Approve/reject a PENDING application, recording who decided and when."""
    if not session.get("admin"):
        abort(403)
    actor = session.get("admin_user", ADMIN_USERNAME)
    conn = get_db()
    cur = conn.cursor()
    try:
        row = decide_application(cur, app_id, new_status, actor, note)
    except ValueError as e:
        cur.close(); conn.close()
        flash("Not recorded: %s." % e)
        return redirect(url_for("admin_view", app_id=app_id))
    if row:
        conn.commit()
        notify_decision(row[0], row[1], new_status, note)
        flash("Application %s %s." % (row[0], new_status.lower()))
    else:
        flash("Only a pending application can be decided.")
    cur.close(); conn.close()
    return redirect(url_for("admin_view", app_id=app_id))


@app.route("/admin/grievance/<int:gid>/resolve", methods=["POST"])
def admin_resolve_grievance(gid):
    if not session.get("admin"):
        abort(403)
    actor = session.get("admin_user", ADMIN_USERNAME)
    conn = get_db()
    cur = conn.cursor()
    app_id = resolve_grievance(cur, gid, actor, request.form.get("resolution", ""))
    conn.commit()
    cur.close(); conn.close()
    if app_id is None:
        flash("Grievance not found, already resolved, or the resolution is too short.")
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("admin_view", app_id=app_id))


def resolve_grievance(cur, gid, actor, resolution):
    """Close an open grievance in the officer's name. Returns the application id,
    or None if nothing was closed. The caller commits."""
    resolution = (resolution or "").strip()
    if len(resolution) < REJECT_REASON_MIN:
        return None
    cur.execute("UPDATE grievances SET status = 'RESOLVED', resolved_at = %s, resolved_by = %s, "
                "resolution = %s WHERE id = %s AND status = 'OPEN' "
                "RETURNING application_id, mobile, kind",
                (datetime.now(), actor, resolution[:1000], gid))
    row = cur.fetchone()
    if not row:
        return None
    write_audit(cur, row[0], "GRIEVANCE_CLOSED", "admin:%s" % actor, resolution[:500])
    send_sms(row[1], "Sewa Setu: your complaint (%s) has been looked into. %s"
             % (row[2].replace("_", " ").lower(), resolution[:110]))
    return row[0]


# ---------------------------------------------------------------------------
# Agent channel: pages that support the MCP servers (the API itself is api.py)
# ---------------------------------------------------------------------------

@app.route("/agents")
def agents_page():
    """The official channel for AI assistants: where the endpoints are and what they may do."""
    return render_template("agents.html", issuer=os.environ.get("PUBLIC_BASE_URL",
                                                                 "http://localhost:8000"),
                           citizen_url=os.environ.get("MCP_CITIZEN_URL", "http://localhost:8101/mcp/citizen"),
                           officer_url=os.environ.get("MCP_OFFICER_URL", "http://localhost:8102/mcp/officer"))


@app.route("/upload/<token>", methods=["GET", "POST"])
def upload_by_link(token):
    """One-time page, opened on the phone's browser, that attaches an age-proof file to
    the draft an assistant is filling in. The token is the proof: it was handed out to
    the signed-in citizen's assistant and dies on first use or after 30 minutes."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT mobile, created_at, used_at FROM upload_tokens WHERE token = %s", (token,))
    row = cur.fetchone()
    if not row or row[2] is not None or (datetime.now() - row[1]).total_seconds() > 1800:
        cur.close(); conn.close()
        return render_template("oauth_error.html", error="This upload link is no longer valid",
                               detail="Ask your assistant for a fresh link."), 410
    mobile = row[0]
    if request.method == "POST":
        f = request.files.get("document")
        problem = check_upload(f)
        if problem:
            flash(problem)
        else:
            path = store_upload(mobile, f.filename, f.read())
            cur.execute("INSERT INTO drafts (mobile, data, doc_path, channel, updated_at) "
                        "VALUES (%s, '{}'::jsonb, %s, 'AGENT', %s) ON CONFLICT (mobile) DO UPDATE "
                        "SET doc_path = EXCLUDED.doc_path, updated_at = EXCLUDED.updated_at",
                        (mobile, path, datetime.now()))
            cur.execute("UPDATE upload_tokens SET used_at = %s WHERE token = %s",
                        (datetime.now(), token))
            conn.commit()
            cur.close(); conn.close()
            return render_template("upload_done.html", doc_name=os.path.basename(path))
    cur.close(); conn.close()
    return render_template("upload_link.html", mobile=mobile)


def check_upload(f):
    """The web form's file rules, in one place. Returns a problem string or None."""
    if f is None or f.filename == "":
        return "Please choose a file to upload (JPG, PNG or PDF, up to 5 MB)."
    if f.filename.lower().rsplit(".", 1)[-1] not in ("pdf", "jpg", "jpeg", "png"):
        return "Please upload a JPG, PNG or PDF file."
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if size > 5 * 1024 * 1024:
        return "File is larger than 5 MB. Please upload a smaller file."
    return None


def store_upload(mobile, filename, content):
    """Write the file to the upload folder and keep a copy in the database, so the
    officer can still see it on hosts whose disk is wiped on every restart."""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(filename.lower()))[-80:]
    path = os.path.join(UPLOAD_DIR, "%s_%s" % (mobile, safe))
    with open(path, "wb") as out:
        out.write(content)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO documents (path, content, created_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (path) DO UPDATE SET content = EXCLUDED.content, created_at = EXCLUDED.created_at",
                (path, psycopg2.Binary(content), datetime.now()))
    conn.commit()
    cur.close(); conn.close()
    return path


def read_document(cur, path):
    """The stored bytes of an uploaded document, from disk or else the database."""
    if not path:
        return None
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read()
    cur.execute("SELECT content FROM documents WHERE path = %s", (path,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


@app.route("/google5d01b4d0624bcc74.html")
def google_site_verification_file():
    """Google Search Console ownership file, needed to request a Safe Browsing review."""
    return "google-site-verification: google5d01b4d0624bcc74.html", 200, {"Content-Type": "text/html"}


@app.route("/healthz")
def healthz():
    """For uptime pingers: answers without touching the database."""
    return "ok"


EVAL_RESULTS_DIR = os.environ.get("EVAL_RESULTS_DIR", os.path.join(BASE_DIR, "eval_results"))


@app.route("/eval-dashboard")
def eval_dashboard():
    """The people I invented, what they said to the assistant, what I checked in the
    database afterwards, and what passed. Rendered from evals/results/*.json."""
    import json
    runs = []
    try:
        names = sorted(n for n in os.listdir(EVAL_RESULTS_DIR) if n.endswith(".json")
                       and n != "summary.json")
    except OSError:
        names = []
    for n in names:
        try:
            with open(os.path.join(EVAL_RESULTS_DIR, n), encoding="utf-8") as fh:
                runs.append(json.load(fh))
        except (OSError, ValueError) as e:
            app.logger.error("eval result %s unreadable: %s" % (n, e))
    summary = None
    try:
        with open(os.path.join(EVAL_RESULTS_DIR, "summary.json"), encoding="utf-8") as fh:
            summary = json.load(fh)
    except (OSError, ValueError):
        pass
    passed = sum(1 for r in runs if r.get("passed"))
    return render_template("eval_dashboard.html", runs=runs, summary=summary,
                           passed=passed, total=len(runs))


@app.route("/admin/approve/<int:app_id>", methods=["POST"])
def admin_approve(app_id):
    return _admin_decide(app_id, "APPROVED", request.form.get("note", ""))


@app.route("/admin/reject/<int:app_id>", methods=["POST"])
def admin_reject(app_id):
    return _admin_decide(app_id, "REJECTED", request.form.get("note", ""))


from oauth import oauth as _oauth_blueprint   # noqa: E402  (needs the definitions above)
app.register_blueprint(_oauth_blueprint)
from api import api as _api_blueprint         # noqa: E402
app.register_blueprint(_api_blueprint)
from assistant import assistant as _assistant_blueprint   # noqa: E402
app.register_blueprint(_assistant_blueprint)

apply_migrations()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
