# -*- coding: utf-8 -*-
"""
Gemma 4 as an MCP client, for the evals.

Given an MCP endpoint and a token from the portal's sign-in, it reads the server's
instructions and tools the way any MCP client does (initialize, tools/list), then
talks: each model turn may call tools over JSON-RPC until it answers in words. It
knows nothing about Sewa Setu beyond what the server tells it.

Also plays the invented people (persona()) from their briefs.

The Gemini API free tier limits requests and tokens per minute per model, so every
call waits out a 429 (and moves between the two Gemma models) instead of failing.
"""

import os
import re
import json
import time

import requests

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent"
ASSISTANT_MODELS = ["gemma-4-26b-a4b-it", "gemma-4-31b-it"]
PERSONA_MODELS = ["gemma-4-31b-it", "gemma-4-26b-a4b-it"]
KEEP_TOOL_RESULTS = 3


def _key():
    k = os.environ.get("GEMINI_API_KEY")
    if not k:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for line in open(os.path.join(here, ".env")):
            if line.startswith("GEMINI_API_KEY="):
                k = line.split("=", 1)[1].strip()
    return k


def gemini(body, models, max_wait=600):
    """POST generateContent, waiting out rate limits. Returns the candidate content."""
    key, waited, i = _key(), 0, 0
    while True:
        model = models[i % len(models)]
        try:
            r = requests.post(GEMINI_URL % model, params={"key": key}, json=body, timeout=120)
        except requests.RequestException:
            r = None
        if r is not None and r.status_code == 200:
            cands = r.json().get("candidates") or []
            if cands and cands[0].get("content"):
                return cands[0]["content"]
            body = dict(body)          # empty answer: nudge once more
        elif r is not None and r.status_code not in (429, 500, 503):
            raise RuntimeError("gemini %s %s: %s" % (model, r.status_code, r.text[:300]))
        i += 1
        if i % len(models) == 0:       # tried every model: wait before the next round
            m = re.search(r'"retryDelay":\s*"(\d+)', r.text) if r is not None else None
            d = min(int(m.group(1)) + 1 if m else 8, 60)
            if waited > max_wait:
                raise RuntimeError("rate limited for over %ds" % waited)
            time.sleep(d)
            waited += d


def _text(content):
    return "".join(p.get("text", "") for p in content.get("parts", []) if not p.get("thought")).strip()


def _schema(s):
    """MCP (JSON Schema from pydantic) -> the OpenAPI subset the Gemini API takes."""
    if not isinstance(s, dict):
        return s
    if "anyOf" in s:
        nn = [x for x in s["anyOf"] if x.get("type") != "null"]
        merged = dict(nn[0]) if nn else {"type": "string"}
        if s.get("description"):
            merged["description"] = s["description"]
        s = merged
    out = {}
    if "type" in s:
        out["type"] = s["type"].upper()
    for k in ("description", "enum", "required"):
        if k in s:
            out[k] = s[k]
    if s.get("properties"):
        out["properties"] = {k: _schema(v) for k, v in s["properties"].items()}
    if "items" in s:
        out["items"] = _schema(s["items"])
    return out


class GemmaAgent:
    def __init__(self, mcp_url, token, frame=""):
        self.url, self.token, self.contents = mcp_url, token, []
        init = self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "sewasetu-evals-gemma", "version": "1"}})
        self.instructions = (init.get("instructions") or "") + frame
        self.tools = []
        for t in self._rpc("tools/list")["tools"]:
            d = {"name": t["name"], "description": t.get("description", "")}
            p = _schema(t.get("inputSchema") or {})
            if p.get("properties"):
                d["parameters"] = p
            self.tools.append(d)

    def _rpc(self, method, params=None):
        r = requests.post(self.url, timeout=90, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                          headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                                   "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"})
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(body["error"].get("message"))
        return body["result"]

    def _call(self, name, args):
        try:
            res = self._rpc("tools/call", {"name": name, "arguments": args or {}})
        except Exception as e:
            return {"error": str(e)[:300]}
        if res.get("structuredContent") is not None:
            out = res["structuredContent"]
        else:
            txt = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
            try:
                out = json.loads(txt)
            except ValueError:
                out = {"text": txt or "(non-text result)"}
        if res.get("isError"):
            out = {"error": out}
        return out if isinstance(out, dict) else {"result": out}

    def _compact(self):
        seen, out = 0, []
        for c in reversed(self.contents):
            if any("functionResponse" in p for p in c.get("parts", [])):
                seen += 1
                if seen > KEEP_TOOL_RESULTS:
                    c = {"role": c["role"], "parts": [
                        {"functionResponse": dict(p["functionResponse"], response={"note": "earlier result, already used"})}
                        if "functionResponse" in p else p for p in c["parts"]]}
            out.append(c)
        return list(reversed(out))

    def say(self, text, max_steps=12):
        """One message from the person; returns (reply, tool names used)."""
        self.contents.append({"role": "user", "parts": [{"text": text}]})
        used = []
        for _ in range(max_steps):
            content = gemini({"systemInstruction": {"parts": [{"text": self.instructions}]},
                              "contents": self._compact(), "tools": [{"functionDeclarations": self.tools}],
                              "generationConfig": {"temperature": 0.3}}, ASSISTANT_MODELS)
            content["role"] = "model"
            self.contents.append(content)
            calls = [p["functionCall"] for p in content.get("parts", []) if "functionCall" in p]
            if not calls:
                return _text(content), used
            parts = []
            for fc in calls:
                used.append(fc["name"])
                fr = {"name": fc["name"], "response": self._call(fc["name"], fc.get("args"))}
                if fc.get("id"):
                    fr["id"] = fc["id"]
                parts.append({"functionResponse": fr})
            self.contents.append({"role": "user", "parts": parts})
        return "(the assistant used too many steps without answering)", used


def persona(brief, transcript):
    convo = "\n".join(("ASSISTANT: " if m["role"] == "assistant" else "YOU: ") + m["text"] for m in transcript)
    system = ("You are playing a person in a test of a government pension assistant. Never break character, "
              "never mention tests or AI. Follow this brief exactly.\n\n" + brief)
    prompt = ("Here is the conversation so far.\n\n%s\n\nWrite your next message to the assistant, and nothing "
              "else: no quotes, no labels, no stage directions. Stay in character." % convo)
    content = gemini({"systemInstruction": {"parts": [{"text": system}]},
                      "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": 0.7}}, PERSONA_MODELS)
    return _text(content).strip().strip('"').removeprefix("YOU:").strip()
