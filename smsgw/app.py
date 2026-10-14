"""PurvSMS - simulated state SMS gateway.

Stands in for the telecom SMS provider. Messages sent via the API appear on the
web console at / once "delivered", after a short carrier latency.
"""

import random
import re
import time
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, render_template_string
from markupsafe import escape

import os

app = Flask(__name__)

MESSAGES = []  # {id, to, text, sent_at, delivery_at}

# On hosts that restart the container often (Render's free plan), an in-memory inbox
# loses OTPs mid-sign-in. With DATABASE_URL set, messages are kept in the database.
DATABASE_URL = os.environ.get("DATABASE_URL")


def _db():
    import psycopg2
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


if DATABASE_URL:
    _c = _db()
    _c.cursor().execute("CREATE TABLE IF NOT EXISTS sms_messages (id SERIAL PRIMARY KEY, to_mobile VARCHAR(20), "
                        "text TEXT, sent_at DOUBLE PRECISION, delivery_at DOUBLE PRECISION)")
    _c.close()


def _store(to, text, sent_at, delivery_at):
    if DATABASE_URL:
        conn = _db()
        cur = conn.cursor()
        cur.execute("INSERT INTO sms_messages (to_mobile, text, sent_at, delivery_at) VALUES (%s, %s, %s, %s) "
                    "RETURNING id", (to, text, sent_at, delivery_at))
        mid = cur.fetchone()[0]
        conn.close()
        return mid
    msg = {"id": len(MESSAGES) + 1, "to": to, "text": text, "sent_at": sent_at, "delivery_at": delivery_at}
    MESSAGES.append(msg)
    return msg["id"]


def _messages(to="", last=None):
    """Oldest first; optionally only one number, optionally only the last N."""
    if DATABASE_URL:
        conn = _db()
        cur = conn.cursor()
        sql = "SELECT id, to_mobile, text, sent_at, delivery_at FROM sms_messages"
        args = []
        if to:
            sql += " WHERE to_mobile = %s"
            args.append(to)
        sql += " ORDER BY id DESC"
        if last:
            sql += " LIMIT %d" % int(last)
        cur.execute(sql, args)
        rows = [{"id": r[0], "to": r[1], "text": r[2], "sent_at": r[3], "delivery_at": r[4]} for r in cur.fetchall()]
        conn.close()
        return list(reversed(rows))
    rows = [m for m in MESSAGES if not to or m["to"] == to]
    return rows[-last:] if last else rows

# Carrier queue latency, seconds.
DELAY_MIN = 1
DELAY_MAX = 4

CONSOLE = """
<!doctype html><html><head><title>PurvSMS Console</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="5">
<style>
body{font-family:Arial,sans-serif;margin:0;padding:20px 16px 60px;background:#f4f4f4}
h2{color:#1a3c6e;margin:0 0 6px} p{margin:4px 0 12px}
table{border-collapse:collapse;background:#fff;width:100%}
td,th{border:1px solid #ccc;padding:8px 10px;font-size:15px;text-align:left;vertical-align:top}
th{background:#eef1f5}
.q{color:#b36b00}.d{color:#1b7e3c} .otp{font-weight:bold;letter-spacing:.08em}
form{margin:0 0 12px;display:flex;gap:6px;flex-wrap:wrap}
input{padding:8px;font-size:15px;border:1px solid #aab;border-radius:6px}
button,a.b{padding:8px 14px;font-size:15px;background:#1a3c6e;color:#fff;border:none;border-radius:6px;text-decoration:none}
tr.new td{background:#f4fbf4}
</style></head><body>
<h2>PurvSMS Gateway Console (simulation)</h2>
<p>Every SMS the portal sends appears here, oldest first, newest at the bottom. Times are IST.
Auto-refreshes every 5s.</p>
<form method="get"><input name="to" value="{{to}}" placeholder="Show only this mobile" inputmode="numeric" maxlength="10">
<button type="submit">Filter</button>{% if to %}<a class="b" href="?">Show all</a>{% endif %}</form>
<table><tr><th>#</th><th>To</th><th>Message</th><th>Sent (IST)</th><th>Status</th></tr>
{% for m in messages %}
<tr class="{{ 'new' if loop.last }}"><td>{{m.id}}</td><td>{{m.to}}</td>
<td>{% if m.delivered %}{{m.html|safe}}{% else %}<i>(in transit)</i>{% endif %}</td>
<td>{{m.sent_hhmm}}</td>
<td>{% if m.delivered %}<span class="d">Delivered</span>
{% else %}<span class="q">Queued &middot; ~{{m.eta}}s</span>{% endif %}</td></tr>
{% else %}<tr><td colspan="5"><i>No messages yet.</i></td></tr>
{% endfor %}
</table>
<script>window.scrollTo(0, document.body.scrollHeight);</script>
</body></html>
"""
IST = timezone(timedelta(hours=5, minutes=30))


@app.route("/api/send", methods=["POST"])
def send():
    data = request.get_json(force=True, silent=True) or request.form
    to = data.get("to", "")
    text = data.get("text", "")
    if not to or not text:
        return jsonify({"status": "error", "reason": "to and text required"}), 400
    now = time.time()
    mid = _store(to, text, now, now + random.randint(DELAY_MIN, DELAY_MAX))
    return jsonify({"status": "queued", "id": mid})


@app.route("/api/messages")
def messages():
    # Inbox query for a given number. Used to read delivered OTPs/SMS.
    to = request.args.get("to", "")
    now = time.time()
    out = []
    for m in _messages(to):
        out.append({
            "id": m["id"], "to": m["to"],
            "text": m["text"] if now >= m["delivery_at"] else None,
            "delivered": now >= m["delivery_at"],
            "sent_at": m["sent_at"], "delivery_at": m["delivery_at"],
        })
    return jsonify({"messages": out})


@app.route("/")
def console():
    now = time.time()
    to = request.args.get("to", "").strip()
    view = []
    for m in _messages(to, last=100):
        # make the OTP stand out; escape everything else
        # str() first: on a Markup object re.sub would escape the <span> we add as well
        html = re.sub(r"\b(\d{6})\b", r'<span class="otp">\1</span>', str(escape(m["text"])))
        view.append({
            "id": m["id"], "to": m["to"], "text": m["text"], "html": html,
            "sent_hhmm": datetime.fromtimestamp(m["sent_at"], IST).strftime("%d %b %H:%M:%S"),
            "delivered": now >= m["delivery_at"],
            "eta": max(0, int(m["delivery_at"] - now)),
        })
    return render_template_string(CONSOLE, messages=view, to=to)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8025)
