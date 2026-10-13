# -*- coding: utf-8 -*-
"""Database access for the eval runner: setting the scene before a conversation and
checking the portal's record after it. Mobiles 98000000xx are reserved for evals."""

import os
import shutil
import subprocess

import psycopg2

DSN = dict(host=os.environ.get("EVAL_DB_HOST", "localhost"), port=int(os.environ.get("EVAL_DB_PORT", 55432)),
           dbname="sewasetu", user="sewasetu", password=os.environ.get("EVAL_DB_PASSWORD", "sewasetu123"))
HERE = os.path.dirname(os.path.abspath(__file__))


def connect():
    conn = psycopg2.connect(**DSN)
    conn.autocommit = True
    return conn


def clear_mobile(cur, mobile):
    assert mobile.startswith("98000000"), "evals only touch the reserved 98000000xx mobiles"
    cur.execute("DELETE FROM audit_log WHERE application_id IN (SELECT id FROM applications WHERE mobile = %s)", (mobile,))
    cur.execute("DELETE FROM grievances WHERE mobile = %s", (mobile,))
    cur.execute("DELETE FROM applications WHERE mobile = %s", (mobile,))
    for t in ("drafts", "upload_tokens", "portal_users", "otps"):
        cur.execute("DELETE FROM %s WHERE mobile = %%s" % t, (mobile,))


def _copy_document(mobile, fixture):
    """Put a document where the app container keeps uploads, so the officer can view it."""
    name = "%s_%s" % (mobile, os.path.basename(fixture))
    docker = shutil.which("docker")
    subprocess.run([docker, "compose", "cp", os.path.join(HERE, fixture), "app:/var/sewasetu/uploads/" + name],
                   check=True, capture_output=True, cwd=os.path.dirname(HERE))
    return "/var/sewasetu/uploads/" + name


def seed_application(cur, mobile, spec, fixture):
    """An application as if it had been made earlier through the web form or counter."""
    import random
    while True:
        app_no = "SSP26" + str(random.randint(100000, 999999))
        cur.execute("SELECT 1 FROM applications WHERE application_no = %s", (app_no,))
        if not cur.fetchone():
            break
    status = spec.get("status", "PENDING")
    sub = int(spec.get("submitted_days_ago", 1))
    dec = spec.get("decided_days_ago")
    decided_by = spec.get("decided_by", "admin" if status in ("APPROVED", "REJECTED") else None)
    doc = _copy_document(mobile, fixture) if spec.get("with_document") else ""
    cur.execute(
        """INSERT INTO applications (application_no, applicant_name, mobile, dob, gender, marital_status,
           husband_name, husband_employer, village, block, bank_account, ifsc, doc_path, status, submitted_at,
           decided_at, decided_by, channel, effective_from, life_certificate_due)
           VALUES (%s,%s,%s,%s,%s,%s,'','',%s,%s,%s,%s,%s,%s, now()::timestamp - %s * interval '1 day',
                   CASE WHEN %s::int IS NULL THEN NULL ELSE now()::timestamp - %s::int * interval '1 day' END,
                   %s, %s,
                   CASE WHEN %s IN ('APPROVED','DEEMED_APPROVED')
                        THEN date_trunc('month', now() - %s * interval '1 day')::date END,
                   CASE WHEN %s::int IS NULL THEN NULL ELSE (now() - %s::int * interval '1 day')::date END)
           RETURNING id, application_no""",
        (app_no, spec["applicant_name"], mobile, spec["dob"],
         spec.get("gender", ""), spec.get("marital_status", ""), spec["village"], spec["block"],
         spec["bank_account"], spec["ifsc"], doc, status, sub, dec, dec, decided_by, spec.get("channel", "WEB"),
         status, sub, spec.get("life_certificate_due_days_ago"), spec.get("life_certificate_due_days_ago")))
    app_id, app_no = cur.fetchone()
    cur.execute("INSERT INTO audit_log (application_id, action, actor, note, at) VALUES "
                "(%s, 'SUBMIT', %s, 'eval seed', now()::timestamp - %s * interval '1 day')",
                (app_id, "applicant:%s" % mobile, sub))
    if status != "PENDING":
        cur.execute("INSERT INTO audit_log (application_id, action, actor, note, at) VALUES "
                    "(%s, %s, %s, 'eval seed', now()::timestamp - %s * interval '1 day')",
                    (app_id, status, decided_by or "system", dec or 0))
    return app_no


def latest_application(cur, mobile):
    cur.execute("SELECT id, application_no, applicant_name, dob, gender, marital_status, village, block, "
                "bank_account, ifsc, status, channel, decided_by, effective_from, submitted_at, life_certified_at, "
                "life_certificate_due, doc_path FROM applications WHERE mobile = %s ORDER BY submitted_at DESC LIMIT 1",
                (mobile,))
    row = cur.fetchone()
    if not row:
        return None
    keys = ("id application_no applicant_name dob gender marital_status village block bank_account ifsc status "
            "channel decided_by effective_from submitted_at life_certified_at life_certificate_due doc_path").split()
    return dict(zip(keys, row))
