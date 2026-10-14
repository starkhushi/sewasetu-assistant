# -*- coding: utf-8 -*-
"""
Play my invented citizens and officer against the real assistant and check the portal.

For each persona in personas.yaml:
  1. set the scene in the database (clear the reserved mobile, seed an earlier application)
  2. sign in as that person through the portal's own pages (signin.py), token audience = the MCP server
  3. the assistant is Claude Code itself (`claude -p`, a small model, only the Sewa Setu MCP tools,
     no other tools, run from an empty folder so nothing of this repo leaks in)
  4. the person is played by a second model from the persona's brief, one message at a time
  5. when the assistant hands out an upload link, the runner uploads the fixture photo to it,
     as the grandson would on his phone
  6. stop at [DONE] or the exchange limit, then check the database
Results go to evals/results/<id>.json and are shown at /eval-dashboard.

    python evals/run.py                  # everything
    python evals/run.py parboti_grandson # one persona
    ASSISTANT_MODEL=sonnet python evals/run.py
"""

import os
import re
import sys
import json
import time
import uuid
import shutil
import tempfile
import subprocess
from datetime import datetime, date

import yaml
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from signin import sign_in, refresh, PORTAL  # noqa: E402
import db  # noqa: E402
from gemma_agent import GemmaAgent, persona as gemma_persona  # noqa: E402

CITIZEN_URL = os.environ.get("MCP_CITIZEN_URL", "http://localhost:8101/mcp/citizen")
OFFICER_URL = os.environ.get("MCP_OFFICER_URL", "http://localhost:8102/mcp/officer")
# ASSISTANT=gemma (default): Gemma 4 as a plain MCP client (gemma_agent.py), persona also Gemma.
# ASSISTANT=claude: Claude Code headless as the client, persona played by Claude.
ASSISTANT = os.environ.get("ASSISTANT", "gemma")
ASSISTANT_MODEL = os.environ.get("ASSISTANT_MODEL", "gemma-4-26b-a4b-it" if ASSISTANT == "gemma" else "haiku")
PERSONA_MODEL = os.environ.get("PERSONA_MODEL", "gemma-4-31b-it" if ASSISTANT == "gemma" else "sonnet")
MAX_TURNS = os.environ.get("ASSISTANT_MAX_TURNS", "12")   # tool-use turns per reply, like the assessors' cap
RESULTS = os.path.join(HERE, "results")
CLAUDE = shutil.which("claude")

# Said once to the assistant, the way a chat app would frame it. Kept short on purpose:
# the assessors run Claude Code without any of my instructions, so the server's own
# instructions and tool descriptions have to carry the behaviour.
ASSISTANT_FRAME = ("You are chatting by text with someone on a phone. Reply in plain short messages. "
                   "The Sewa Setu tools act on the government pension portal for the signed-in person.")
ASSISTANT_FRAME_GEMMA = "\n\n" + ASSISTANT_FRAME + " Today is %s." % datetime.now().strftime("%d %B %Y")


def claude(prompt, args, cwd, timeout=300):
    """Run Claude Code headless; returns the parsed stream-json events."""
    p = subprocess.run([CLAUDE, "-p", "--output-format", "stream-json", "--verbose"] + args,
                       input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=cwd, timeout=timeout)
    events = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    if not any(e.get("type") == "result" for e in events):
        raise RuntimeError("claude failed: %s" % (p.stderr.strip() or p.stdout.strip())[:400])
    return events


def assistant_turn(text, session, first, workdir):
    args = ["--model", ASSISTANT_MODEL, "--mcp-config", os.path.join(workdir, "mcp.json"), "--strict-mcp-config",
            "--allowedTools", "mcp__sewasetu", "--tools", "", "--max-turns", MAX_TURNS,
            "--append-system-prompt", ASSISTANT_FRAME]
    args += ["--session-id", session] if first else ["--resume", session]
    events = claude(text, args, workdir)
    tools, reply = [], ""
    for e in events:
        if e.get("type") == "assistant":
            for block in e.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    tools.append(block["name"].replace("mcp__sewasetu__", ""))
        if e.get("type") == "result":
            reply = e.get("result") or ""
    return reply.strip(), tools


def persona_turn(persona, transcript, workdir):
    convo = "\n".join(("ASSISTANT: " if m["role"] == "assistant" else "YOU: ") + m["text"] for m in transcript)
    prompt = ("Here is the conversation so far.\n\n%s\n\nWrite your next message to the assistant, and nothing "
              "else: no quotes, no stage directions. Stay in character." % convo)
    system = ("You are playing a person in a test of a government pension assistant. Never break character, "
              "never mention tests or AI. Follow this brief exactly.\n\n" + persona["brief"])
    events = claude(prompt, ["--model", PERSONA_MODEL, "--tools", "", "--strict-mcp-config", "--max-turns", "1",
                             "--system-prompt", system, "--no-session-persistence"], workdir)
    return next(e.get("result", "") for e in events if e.get("type") == "result").strip().strip('"')


def do_uploads(reply, fixture):
    """The person opens the link the assistant gave and uploads the photo."""
    done = []
    for url in set(re.findall(r"https?://[^\s)\]>\"']+/upload/[A-Za-z0-9_-]+", reply)):
        if not url.startswith(PORTAL + "/upload/"):
            # the assistant made up a link; the person's phone would get nowhere
            done.append((url, "not the portal's link (invented)"))
            continue
        try:
            with open(os.path.join(HERE, fixture), "rb") as fh:
                r = requests.post(url, files={"document": (os.path.basename(fixture), fh, "image/png")}, timeout=30)
            done.append((url, r.status_code))
        except requests.RequestException as e:
            done.append((url, "failed: %s" % e))
    return done


# ---------------------------------------------------------------------------
# checks: what must be true in the portal afterwards
# ---------------------------------------------------------------------------

def _s(v):
    return v.isoformat() if isinstance(v, (date, datetime)) else ("" if v is None else str(v))


def run_check(cur, c, persona, transcript):
    mobile = c.get("mobile") or persona.get("mobile")
    said = " ".join(m["text"] for m in transcript if m["role"] == "assistant").lower()
    t = c["type"]
    if t == "application":
        a = db.latest_application(cur, mobile)
        if not a:
            return False, "no application for %s" % mobile
        bad = ["%s is %r, wanted %r" % (k, _s(a.get(k)), str(v)) for k, v in c.items()
               if k not in ("type", "mobile") and _s(a.get(k)).lower() != str(v).lower()]
        return not bad, "; ".join(bad) or "%s %s as expected" % (a["application_no"], a["status"])
    if t == "application_count":
        cur.execute("SELECT count(*) FROM applications WHERE mobile = %s", (mobile,))
        n = cur.fetchone()[0]
        return n == c["count"], "%d application(s)" % n
    if t == "no_draft":
        cur.execute("SELECT data FROM drafts WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        return row is None, "no draft left" if row is None else "draft left behind: %s" % row[0]
    if t == "sms_contains":
        msgs = requests.get(PORTAL + "/__gateway/api/messages", params={"to": mobile}, timeout=5).json()["messages"]
        hit = [m["text"] for m in msgs if m["text"] and c["text"].lower() in m["text"].lower()]
        return bool(hit), hit[-1] if hit else "no SMS containing %r" % c["text"]
    if t == "grievance":
        cur.execute("SELECT kind, status, detail FROM grievances WHERE mobile = %s ORDER BY raised_at DESC LIMIT 1", (mobile,))
        row = cur.fetchone()
        if not row:
            return False, "no complaint recorded"
        ok = row[0] == c.get("kind", row[0]) and row[1] == c.get("status", row[1])
        return ok, "%s %s: %s" % row
    if t == "grievance_count":
        cur.execute("SELECT count(*) FROM grievances WHERE mobile = %s", (mobile,))
        n = cur.fetchone()[0]
        return n == c["count"], "%d complaint(s)" % n
    if t == "life_certified":
        a = db.latest_application(cur, mobile)
        ok = bool(a and a["life_certified_at"] and a["life_certificate_due"] > date.today())
        return ok, "confirmed %s, next due %s" % (_s(a and a["life_certified_at"]), _s(a and a["life_certificate_due"]))
    if t == "transcript_contains":
        return c["text"].lower() in said, ("assistant said %r" if c["text"].lower() in said else "assistant never said %r") % c["text"]
    if t == "transcript_contains_any":
        hit = [x for x in c["texts"] if x.lower() in said]
        return bool(hit), "said %s" % hit if hit else "said none of %s" % c["texts"]
    if t == "decision":
        a = db.latest_application(cur, mobile)
        if not a:
            return False, "no application for %s" % mobile
        problems = []
        if a["status"] != c["status"]:
            problems.append("status %s, wanted %s" % (a["status"], c["status"]))
        if c.get("decided_by") and a["decided_by"] != c["decided_by"]:
            problems.append("decided_by %s" % a["decided_by"])
        if c.get("effective_from_is_month_of_submission") and a["effective_from"] != a["submitted_at"].date().replace(day=1):
            problems.append("effective_from %s, submitted %s" % (a["effective_from"], a["submitted_at"].date()))
        if c.get("note_contains"):
            cur.execute("SELECT note FROM audit_log WHERE application_id = %s AND action = %s ORDER BY at DESC LIMIT 1",
                        (a["id"], c["status"]))
            row = cur.fetchone()
            if not row or c["note_contains"].lower() not in (row[0] or "").lower():
                problems.append("recorded reason %r" % (row[0] if row else None))
        return not problems, "; ".join(problems) or "%s %s by %s" % (a["application_no"], a["status"], a["decided_by"])
    return False, "unknown check %s" % t


# ---------------------------------------------------------------------------

def setup(cur, persona, fixture):
    for step in persona.get("setup", []):
        if step == "clear_mobile":
            db.clear_mobile(cur, persona["mobile"])
        elif step["type"] == "clear_mobile":
            db.clear_mobile(cur, step.get("mobile") or persona["mobile"])
        elif step["type"] == "seed_application":
            db.seed_application(cur, step.get("mobile") or persona["mobile"], step, fixture)


def play(persona, defaults, cur):
    fixture = persona.get("document", defaults["document"])
    setup(cur, persona, fixture)
    if persona["role"] == "citizen":
        url, tok = CITIZEN_URL, sign_in(CITIZEN_URL, "citizen", mobile=persona["mobile"])
    else:
        url, tok = OFFICER_URL, sign_in(OFFICER_URL, "officer", password=os.environ.get("OFFICER_PASSWORD"))
    workdir = tempfile.mkdtemp(prefix="sewasetu-eval-")
    agent = None
    if ASSISTANT == "gemma":
        agent = GemmaAgent(url, tok["access_token"], frame=ASSISTANT_FRAME_GEMMA, renew=lambda: refresh(tok))
    else:
        with open(os.path.join(workdir, "mcp.json"), "w") as fh:
            json.dump({"mcpServers": {"sewasetu": {"type": "http", "url": url,
                                                   "headers": {"Authorization": "Bearer " + tok["access_token"]}}}}, fh)
    session = str(uuid.uuid4())
    transcript, started = [], time.time()
    msg = persona["opening"]
    for i in range(int(persona.get("max_exchanges", defaults["max_exchanges"]))):
        transcript.append({"role": "user", "text": msg})
        print("   %s: %s" % (persona["name"].split()[0], msg[:140]), flush=True)
        if agent:
            reply, tools = agent.say(msg)
        else:
            reply, tools = assistant_turn(msg, session, i == 0, workdir)
        transcript.append({"role": "assistant", "text": reply, "tools": tools})
        print("   assistant [%s]: %s" % (", ".join(tools), reply[:200].replace("\n", " ")), flush=True)
        for u, code in do_uploads(reply, fixture):
            print("   (uploaded the photo to %s -> %s)" % (u, code))
        msg = gemma_persona(persona["brief"], transcript) if ASSISTANT == "gemma" else persona_turn(persona, transcript, workdir)
        if "[DONE]" in msg:
            rest = msg.replace("[DONE]", "").strip()
            if rest:
                transcript.append({"role": "user", "text": rest})
            break
    shutil.rmtree(workdir, ignore_errors=True)
    checks = []
    for c in persona["checks"]:
        ok, detail = run_check(cur, c, persona, transcript)
        checks.append({"name": c["type"] + ("" if c["type"] in ("no_draft", "life_certified") else
                                            " " + ", ".join("%s=%s" % (k, v) for k, v in c.items() if k != "type")),
                       "ok": ok, "detail": detail})
    return {"id": persona["id"], "role": persona["role"],
            "persona": {k: persona.get(k) for k in ("name", "age", "village", "who", "wants", "voice", "must_be_true")},
            "transcript": transcript, "checks": checks, "passed": all(c["ok"] for c in checks),
            "turns": len([m for m in transcript if m["role"] == "assistant"]),
            "duration_s": int(time.time() - started), "model": ASSISTANT_MODEL,
            "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M")}


def main():
    if ASSISTANT == "claude" and not CLAUDE:
        sys.exit("claude (Claude Code) is not on PATH")
    with open(os.path.join(HERE, "personas.yaml"), encoding="utf-8") as fh:
        spec = yaml.safe_load(fh)
    only = set(sys.argv[1:])
    personas = [p for p in spec["personas"] if not only or p["id"] in only]
    os.makedirs(RESULTS, exist_ok=True)
    conn = db.connect()
    cur = conn.cursor()
    results = []
    for p in personas:
        print("\n== %s (%s)" % (p["name"], p["id"]))
        try:
            r = play(p, spec["defaults"], cur)
        except Exception as e:
            r = {"id": p["id"], "role": p["role"], "persona": {k: p.get(k) for k in ("name", "age", "village", "who", "wants", "voice", "must_be_true")},
                 "transcript": [], "checks": [{"name": "conversation ran", "ok": False, "detail": str(e)[:300]}],
                 "passed": False, "turns": 0, "duration_s": 0, "model": ASSISTANT_MODEL,
                 "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
        for c in r["checks"]:
            print("   %s %s  (%s)" % ("ok  " if c["ok"] else "FAIL", c["name"], c["detail"]))
        with open(os.path.join(RESULTS, p["id"] + ".json"), "w", encoding="utf-8") as fh:
            json.dump(r, fh, ensure_ascii=False, indent=1, default=str)
        results.append(r)
    summary = {"ran_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
               "model": ("%s (Gemma 4 as an MCP client)" % ASSISTANT_MODEL) if ASSISTANT == "gemma" else "Claude Code, " + ASSISTANT_MODEL,
               "persona_model": PERSONA_MODEL, "mcp_url": CITIZEN_URL + " , " + OFFICER_URL,
               "passed": sum(r["passed"] for r in results), "total": len(results)}
    print("\n%d / %d passed in this run" % (summary["passed"], summary["total"]))
    # the dashboard summary covers every persona's latest result, so re-running a few
    # personas updates the whole picture
    latest = []
    for n in sorted(os.listdir(RESULTS)):
        if n.endswith(".json") and n != "summary.json":
            with open(os.path.join(RESULTS, n), encoding="utf-8") as fh:
                latest.append(json.load(fh))
    summary.update(passed=sum(1 for r in latest if r.get("passed")), total=len(latest))
    with open(os.path.join(RESULTS, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)


if __name__ == "__main__":
    main()
