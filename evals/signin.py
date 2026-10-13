# -*- coding: utf-8 -*-
"""
Sign in to Sewa Setu the way a person does, without a browser: register a client,
start an authorization with PKCE for one MCP server, go through the portal's own
login page (citizen: mobile, then the OTP read off the simulated phone at
/__gateway/; officer: departmental login), press Allow on the consent page, and
exchange the code for a token whose audience is that MCP server.

Nothing here talks to the database. It only uses the pages and endpoints any
client would.
"""

import os
import re
import time
import base64
import hashlib
import secrets
from urllib.parse import urlparse, parse_qs, urlencode

import requests

PORTAL = os.environ.get("PORTAL_URL", "http://localhost:8000").rstrip("/")
REDIRECT = "http://localhost:53682/callback"


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _register(s):
    r = s.post(PORTAL + "/oauth/register", json={"client_name": "Sewa Setu eval runner",
                                                 "redirect_uris": [REDIRECT]})
    r.raise_for_status()
    return r.json()["client_id"]


def _rid(resp):
    loc = resp.headers.get("Location", "")
    return parse_qs(urlparse(loc).query).get("rid", [None])[0]


def _latest_otp(mobile, after, timeout=20):
    """Read the OTP SMS off the simulated phone. The carrier takes 1-4 s."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        msgs = requests.get(PORTAL + "/__gateway/api/messages", params={"to": mobile}, timeout=5).json()["messages"]
        for m in reversed(msgs):
            if m["sent_at"] >= after - 1 and m["delivered"] and m["text"] and "OTP" in m["text"]:
                return re.search(r"(\d{6})", m["text"]).group(1)
        time.sleep(1)
    raise RuntimeError("no OTP reached %s" % mobile)


def sign_in(resource, role, mobile=None, username="admin", password=None):
    """Returns the token response (access_token, refresh_token, session_ends, ...)."""
    s = requests.Session()
    client_id = _register(s)
    verifier, challenge = _pkce()
    q = {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
         "code_challenge": challenge, "code_challenge_method": "S256", "scope": role,
         "resource": resource, "state": secrets.token_urlsafe(8)}
    r = s.get(PORTAL + "/oauth/authorize?" + urlencode(q), allow_redirects=False)
    rid = _rid(r)
    if not rid:
        raise RuntimeError("authorize did not start a sign-in: %s %s" % (r.status_code, r.text[:200]))
    login = PORTAL + "/oauth/login?rid=" + rid
    if role == "citizen":
        t0 = time.time()
        s.post(login, data={"mobile": mobile}, allow_redirects=False)
        otp = _latest_otp(mobile, t0)
        r = s.post(login, data={"otp": otp}, allow_redirects=False)
    else:
        password = password or os.environ.get("ADMIN_PASSWORD", "officer-dev-pass")
        r = s.post(login, data={"username": username, "password": password}, allow_redirects=False)
    if "/oauth/consent" not in r.headers.get("Location", ""):
        raise RuntimeError("login was not accepted (%s)" % r.status_code)
    r = s.post(PORTAL + "/oauth/consent?rid=" + rid, data={"decision": "allow"}, allow_redirects=False)
    code = parse_qs(urlparse(r.headers.get("Location", "")).query).get("code", [None])[0]
    if not code:
        raise RuntimeError("consent did not return a code")
    r = s.post(PORTAL + "/oauth/token", data={"grant_type": "authorization_code", "code": code,
                                              "client_id": client_id, "code_verifier": verifier,
                                              "redirect_uri": REDIRECT, "resource": resource})
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    import sys
    role = sys.argv[1]
    res = sys.argv[2]
    tok = sign_in(res, role, mobile=sys.argv[3] if len(sys.argv) > 3 else None)
    print(tok["access_token"])
