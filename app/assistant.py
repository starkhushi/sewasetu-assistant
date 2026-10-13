# -*- coding: utf-8 -*-
"""
A chat assistant on the website itself, for people who do not have one of their own.

It is deliberately just another client of the citizen MCP server, with no shortcut
into the database: the person signs in through the portal's own OAuth pages (mobile
+ OTP, then Allow), the chat holds the resulting token, and every action is an MCP
tool call to the same endpoint Claude or any other assistant would use. The model is
Gemma 4 through Google's Gemini API, with the MCP server's own instructions and tools.

The browser drives the loop one step at a time (/send, then /continue until done),
so no single request waits on several model calls, and the person sees what the
assistant is doing ("Checking your application...") as it happens.
"""

import os
import re
import json
import time
import base64
import hashlib
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests
from flask import Blueprint, request, jsonify, session, redirect, render_template, url_for

import app as portal
import oauth

assistant = Blueprint("assistant", __name__)

CLIENT_ID = "sewasetu-web-assistant"
CLIENT_NAME = "Sewa Setu chat assistant (Gemma)"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# Each model has its own free-tier quota; when one is at its per-minute limit, use the other.
GEMMA_MODELS = [m.strip() for m in os.environ.get("GEMMA_MODELS", "gemma-4-26b-a4b-it,gemma-4-31b-it").split(",") if m.strip()]
KEEP_TOOL_RESULTS = 3   # older tool results are shortened in the history sent to the model
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent"
MCP_PUBLIC = os.environ.get("MCP_CITIZEN_URL", "http://localhost:8101/mcp/citizen").rstrip("/")
MCP_INTERNAL = os.environ.get("MCP_CITIZEN_INTERNAL_URL", MCP_PUBLIC).rstrip("/")
SELF_URL = os.environ.get("PORTAL_SELF_URL", "http://127.0.0.1:8000").rstrip("/")
MAX_STEPS = 16          # model calls (and busy retries) per message from the person

WEB_FRAME = """

You are the chat assistant on the Sewa Setu website itself. The person is already signed in (their mobile was verified by OTP).
- Reply in the language the person writes in (English, Hindi, Assamese, Bengali...), in short plain sentences.
- Links you give are clickable. When you share an upload link, say: "Tap the link, choose the photo, press Upload, then come back and tell me."
- Never show raw JSON, tool names or field names; say things the way a helpful clerk would.
- Today is %s."""

# What the person sees while a tool runs
TOOL_LABELS = {
    "scheme_rules": "Reading the scheme rules", "my_status": "Looking up your file",
    "save_details": "Saving to your form", "show_draft": "Checking your form",
    "clear_draft": "Clearing the form", "upload_link": "Making an upload link",
    "attach_age_proof": "Attaching the document", "review_application": "Preparing the preview",
    "submit_application": "Submitting your application", "application_status": "Checking your application",
    "withdraw_application": "Withdrawing the application", "report_payment_problem": "Recording your complaint",
    "confirm_alive": "Recording your life certificate",
}


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def _load(chat_id):
    if not chat_id:
        return None
    conn = portal.get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, mobile, contents, display, access_token, refresh_token, token_exp, steps "
                "FROM assistant_chats WHERE id = %s", (chat_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        return None
    keys = ("id", "mobile", "contents", "display", "access_token", "refresh_token", "token_exp", "steps")
    return dict(zip(keys, row))


def _save(chat):
    conn = portal.get_db()
    cur = conn.cursor()
    cur.execute("UPDATE assistant_chats SET contents = %s, display = %s, access_token = %s, refresh_token = %s, "
                "token_exp = %s, steps = %s, updated_at = %s WHERE id = %s",
                (json.dumps(chat["contents"]), json.dumps(chat["display"]), chat["access_token"],
                 chat["refresh_token"], chat["token_exp"], chat["steps"], datetime.now(), chat["id"]))
    conn.commit()
    cur.close(); conn.close()


def _ensure_client():
    """The chat is a registered OAuth client of the portal like any other."""
    conn = portal.get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO oauth_clients (client_id, client_name, redirect_uris, secret_hash, kind, created_at, fetched_at) "
                "VALUES (%s, %s, %s, NULL, 'dcr', %s, %s) ON CONFLICT (client_id) DO UPDATE SET "
                "redirect_uris = EXCLUDED.redirect_uris, client_name = EXCLUDED.client_name",
                (CLIENT_ID, CLIENT_NAME, json.dumps([_redirect_uri()]), datetime.now(), datetime.now()))
    conn.commit()
    cur.close(); conn.close()


def _redirect_uri():
    return oauth.ISSUER + "/assistant/callback"


# ---------------------------------------------------------------------------
# sign-in: the portal's own OAuth flow, audience = the citizen MCP server
# ---------------------------------------------------------------------------

@assistant.get("/assistant")
def page():
    chat = _load(session.get("chat_id"))
    return render_template("assistant.html", chat=chat, configured=bool(GEMINI_API_KEY),
                           display=(chat or {}).get("display", []))


@assistant.get("/assistant/start")
def start():
    _ensure_client()
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(16)
    session["asst_pkce"] = verifier
    session["asst_state"] = state
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = {"response_type": "code", "client_id": CLIENT_ID, "redirect_uri": _redirect_uri(), "scope": "citizen",
         "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "resource": MCP_PUBLIC}
    return redirect("/oauth/authorize?" + urlencode(q))


@assistant.get("/assistant/callback")
def callback():
    if request.args.get("error") or request.args.get("state") != session.get("asst_state"):
        return redirect(url_for("assistant.page"))
    r = requests.post(SELF_URL + "/oauth/token", timeout=30, data={
        "grant_type": "authorization_code", "code": request.args.get("code", ""), "client_id": CLIENT_ID,
        "code_verifier": session.pop("asst_pkce", ""), "redirect_uri": _redirect_uri(), "resource": MCP_PUBLIC})
    if r.status_code != 200:
        return redirect(url_for("assistant.page"))
    tok = r.json()
    claims = oauth.verify_token(tok["access_token"], [MCP_PUBLIC, MCP_PUBLIC + "/"]) or {}
    mobile = (claims.get("sub") or ":").split(":", 1)[1]
    chat_id = secrets.token_urlsafe(24)
    greeting = ("Namaste! I am the Sewa Setu assistant. You are signed in with mobile %s. I can apply for the "
                "old age pension, tell you where an application stands, or help if the money has not come. "
                "What would you like to do?" % mobile)
    conn = portal.get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO assistant_chats (id, mobile, contents, display, access_token, refresh_token, token_exp, "
                "steps, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s, %s)",
                (chat_id, mobile, json.dumps([{"role": "model", "parts": [{"text": greeting}]}]),
                 json.dumps([{"role": "assistant", "text": greeting}]), tok["access_token"], tok.get("refresh_token"),
                 datetime.now() + timedelta(seconds=int(tok.get("expires_in", 600)) - 30), datetime.now(), datetime.now()))
    conn.commit()
    cur.close(); conn.close()
    session["chat_id"] = chat_id
    session.pop("asst_state", None)
    return redirect(url_for("assistant.page"))


@assistant.post("/assistant/signout")
def signout():
    chat = _load(session.pop("chat_id", None))
    if chat and chat["refresh_token"]:
        requests.post(SELF_URL + "/oauth/revoke", data={"token": chat["refresh_token"]}, timeout=10)
    return redirect(url_for("assistant.page"))


def _fresh_token(chat):
    if chat["token_exp"] and chat["token_exp"] > datetime.now():
        return chat["access_token"]
    if not chat["refresh_token"]:
        return None
    r = requests.post(SELF_URL + "/oauth/token", timeout=30, data={
        "grant_type": "refresh_token", "refresh_token": chat["refresh_token"], "client_id": CLIENT_ID,
        "resource": MCP_PUBLIC})
    if r.status_code != 200:
        chat["refresh_token"] = None
        return None
    tok = r.json()
    chat["access_token"] = tok["access_token"]
    chat["refresh_token"] = tok.get("refresh_token")
    chat["token_exp"] = datetime.now() + timedelta(seconds=int(tok.get("expires_in", 600)) - 30)
    return chat["access_token"]


# ---------------------------------------------------------------------------
# MCP client (plain JSON-RPC over streamable HTTP; the server is stateless)
# ---------------------------------------------------------------------------

_MCP_CACHE = {}


def _mcp(token, method, params=None):
    r = requests.post(MCP_INTERNAL, timeout=60, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                      headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream",
                               "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"})
    if r.status_code == 401:
        raise PermissionError("signed out")
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(body["error"].get("message", "MCP error"))
    return body["result"]


def _gemini_schema(s):
    """MCP tool input schemas are JSON Schema (from pydantic); the Gemini API takes an
    OpenAPI subset. Collapse Optional[...] and drop what it does not understand."""
    if not isinstance(s, dict):
        return s
    if "anyOf" in s:
        non_null = [x for x in s["anyOf"] if x.get("type") != "null"]
        merged = dict(non_null[0]) if non_null else {"type": "string"}
        if s.get("description"):
            merged["description"] = s["description"]
        s = merged
    out = {}
    if "type" in s:
        out["type"] = s["type"].upper()
    for k in ("description", "enum", "required"):
        if k in s:
            out[k] = s[k]
    if "properties" in s:
        out["properties"] = {k: _gemini_schema(v) for k, v in s["properties"].items()}
        if not out["properties"]:
            del out["properties"]
    if "items" in s:
        out["items"] = _gemini_schema(s["items"])
    return out


def _tools_and_instructions(token):
    if "tools" not in _MCP_CACHE or time.time() - _MCP_CACHE.get("at", 0) > 600:
        init = _mcp(token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": CLIENT_ID, "version": "1"}})
        tools = _mcp(token, "tools/list")["tools"]
        decls = []
        for t in tools:
            d = {"name": t["name"], "description": t.get("description", "")}
            params = _gemini_schema(t.get("inputSchema") or {})
            if params.get("properties"):
                d["parameters"] = params
            decls.append(d)
        _MCP_CACHE.update(tools=decls, instructions=init.get("instructions", ""), at=time.time())
    return _MCP_CACHE["tools"], _MCP_CACHE["instructions"]


def _call_tool(token, name, args):
    res = _mcp(token, "tools/call", {"name": name, "arguments": args or {}})
    if res.get("structuredContent") is not None:
        out = res["structuredContent"]
    else:
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        try:
            out = json.loads(text)
        except ValueError:
            out = {"text": text}
    if res.get("isError"):
        out = {"error": out}
    return out if isinstance(out, dict) else {"result": out}


# ---------------------------------------------------------------------------
# the loop, one step per request
# ---------------------------------------------------------------------------

def _compact(contents):
    """The free tier limits tokens per minute, and every call carries the whole history.
    Keep the last few tool results whole; older ones shrink to a stub (the model has
    already turned them into words in its replies)."""
    seen, out = 0, []
    for c in reversed(contents):
        parts = c.get("parts", [])
        if any("functionResponse" in p for p in parts):
            seen += 1
            if seen > KEEP_TOOL_RESULTS:
                c = {"role": c["role"], "parts": [
                    {"functionResponse": dict(p["functionResponse"], response={"note": "earlier result, already used"})}
                    if "functionResponse" in p else p for p in parts]}
        out.append(c)
    return list(reversed(out))


class Busy(Exception):
    def __init__(self, wait):
        self.wait = wait


def _gemma(contents, tools, instructions):
    body = {"systemInstruction": {"parts": [{"text": instructions + WEB_FRAME % datetime.now().strftime("%d %B %Y")}]},
            "contents": _compact(contents), "tools": [{"functionDeclarations": tools}],
            "generationConfig": {"temperature": 0.3}}
    # The key goes in the query string: AI Studio's newer "AQ." keys are refused in the
    # x-goog-api-key header. Transient 500s get one quick retry; a 429 moves to the next model.
    wait = 5
    for model in GEMMA_MODELS:
        for attempt in range(2):
            r = requests.post(GEMINI_URL % model, params={"key": GEMINI_API_KEY}, json=body, timeout=90)
            if r.status_code == 200:
                break
            portal.app.logger.warning("gemini %s %s: %s" % (model, r.status_code, r.text[:200]))
            if r.status_code in (500, 503) and attempt == 0:
                time.sleep(2)
                continue
            break
        if r.status_code == 200:
            break
        if r.status_code == 429:
            m = re.search(r'"retryDelay":\s*"(\d+)', r.text)
            wait = min(int(m.group(1)) if m else 10, 30)
            continue
        if r.status_code not in (500, 503):
            raise RuntimeError("model error %d" % r.status_code)
    if r.status_code != 200:
        raise Busy(wait)
    cands = r.json().get("candidates") or []
    if not cands or not cands[0].get("content"):
        return {"role": "model", "parts": [{"text": "Sorry, I could not answer that. Could you say it another way?"}]}
    content = cands[0]["content"]
    content["role"] = "model"
    return content


def _step(chat):
    """One model call; runs any tools it asks for. Returns (done, status_label)."""
    token = _fresh_token(chat)
    if not token:
        chat["display"].append({"role": "assistant", "text": "Your sign-in has ended (it lasts 2 hours). "
                                "Please sign in again to continue."})
        return True, None
    tools, instructions = _tools_and_instructions(token)
    content = _gemma(chat["contents"], tools, instructions)
    chat["contents"].append(content)
    calls = [p["functionCall"] for p in content.get("parts", []) if "functionCall" in p]
    if not calls:
        text = "".join(p.get("text", "") for p in content.get("parts", []) if not p.get("thought")).strip()
        chat["display"].append({"role": "assistant", "text": text or "..."})
        return True, None
    responses, labels = [], []
    for fc in calls:
        try:
            out = _call_tool(token, fc["name"], fc.get("args") or {})
        except PermissionError:
            chat["refresh_token"] = None
            chat["display"].append({"role": "assistant", "text": "Your sign-in has ended. Please sign in again."})
            return True, None
        except Exception as e:
            out = {"error": str(e)[:300]}
        part = {"functionResponse": {"name": fc["name"], "response": out}}
        if fc.get("id"):
            part["functionResponse"]["id"] = fc["id"]
        responses.append(part)
        labels.append(TOOL_LABELS.get(fc["name"], "Working"))
    chat["contents"].append({"role": "user", "parts": responses})
    if chat["display"] and chat["display"][-1].get("role") == "tools":
        chat["display"][-1]["labels"] += labels
    else:
        chat["display"].append({"role": "tools", "labels": labels})
    return False, labels[-1]


def _run(first_text=None):
    chat = _load(session.get("chat_id"))
    if not chat:
        return jsonify({"error": "signin", "message": "Please sign in first."}), 401
    if not GEMINI_API_KEY:
        return jsonify({"error": "config", "message": "The chat assistant is not configured on this server."}), 503
    if first_text is not None:
        first_text = first_text.strip()[:2000]
        if not first_text:
            return jsonify({"error": "empty"}), 400
        chat["contents"].append({"role": "user", "parts": [{"text": first_text}]})
        chat["display"].append({"role": "user", "text": first_text})
        chat["steps"] = 0
    wait = 0
    try:
        if chat["steps"] >= MAX_STEPS:
            chat["display"].append({"role": "assistant", "text": "Sorry, that took too many steps. Could you tell me again, more simply?"})
            done, label = True, None
        else:
            chat["steps"] += 1
            done, label = _step(chat)
    except Busy as b:
        # nothing was added to the history; the browser waits and asks again
        done, label, wait = False, "Busy, trying again in a few seconds", b.wait
    except RuntimeError:
        chat["display"].append({"role": "assistant", "text": "Something went wrong on our side. Please try again."})
        done, label = True, None
    _save(chat)
    return jsonify({"done": done, "status": label, "wait": wait, "display": chat["display"],
                    "signed_in": bool(chat["refresh_token"] or (chat["token_exp"] and chat["token_exp"] > datetime.now()))})


@assistant.post("/assistant/send")
def send():
    return _run((request.get_json(silent=True) or {}).get("text", ""))


@assistant.post("/assistant/continue")
def cont():
    return _run(None)
