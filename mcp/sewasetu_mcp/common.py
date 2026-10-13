# -*- coding: utf-8 -*-
"""
What the two MCP servers share: how a token from the portal is checked, and how a
tool call becomes a call to the portal's JSON API in the signed-in person's name.

Each server is an OAuth resource server (RFC 9728). It tells clients that the portal
is the authorization server; the client signs the person in on the portal's own
pages and comes back with a JWT whose audience is this server. We check the
signature against the portal's JWKS, the issuer, the audience and the scope, then
forward the same token to the portal API, which checks it again before touching
the database. The MCP server therefore holds no secrets and no data of its own.
"""

import os
import time
import logging
from typing import Any

import httpx
import jwt

from mcp.server.mcpserver import MCPServer
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.middleware.auth_context import get_access_token

log = logging.getLogger("sewasetu")

# Where people reach the portal (the token issuer) and where this container reaches it.
PORTAL_PUBLIC_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
PORTAL_INTERNAL_URL = os.environ.get("PORTAL_INTERNAL_URL", PORTAL_PUBLIC_URL).rstrip("/")


class PortalTokenVerifier:
    """Verifies the portal's RS256 access tokens with its published keys."""

    def __init__(self, resource_url: str, scope: str):
        self.resource_url = resource_url.rstrip("/")
        self.scope = scope
        self._jwks: dict[str, Any] = {}
        self._fetched = 0.0

    async def _keys(self, force=False):
        if force or not self._jwks or time.time() - self._fetched > 3600:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(PORTAL_INTERNAL_URL + "/.well-known/jwks.json")
                r.raise_for_status()
            self._jwks = {k["kid"]: jwt.algorithms.RSAAlgorithm.from_jwk(k) for k in r.json()["keys"]}
            self._fetched = time.time()
        return self._jwks

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            return None
        keys = await self._keys()
        if kid not in keys:
            keys = await self._keys(force=True)   # the portal may have rotated its key
        key = keys.get(kid)
        if key is None:
            return None
        try:
            claims = jwt.decode(token, key, algorithms=["RS256"], issuer=PORTAL_PUBLIC_URL,
                                audience=[self.resource_url, self.resource_url + "/"])
        except jwt.PyJWTError as e:
            log.info("token refused: %s", e)
            return None
        scopes = (claims.get("scope") or "").split()
        if self.scope not in scopes or claims.get("role") != self.scope:
            return None
        return AccessToken(token=token, client_id=claims.get("client_id", ""), scopes=scopes,
                           expires_at=claims.get("exp"), resource=self.resource_url,
                           subject=claims.get("sub"), claims=claims)


class PortalError(Exception):
    """The portal said no; the message is already in plain words for the person."""


class Portal:
    """The portal's JSON API, called with the signed-in person's own token."""

    def __init__(self):
        self.base = PORTAL_INTERNAL_URL + "/api/v1"

    async def call(self, method: str, path: str, **kw) -> dict:
        tok = get_access_token()
        if tok is None:
            raise PortalError("not signed in; the person must sign in to Sewa Setu first")
        headers = {"Authorization": "Bearer " + tok.token}
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.request(method, self.base + path, headers=headers, **kw)
        try:
            body = r.json()
        except ValueError:
            body = {"error": "portal_error", "message": r.text[:300]}
        if r.status_code >= 400:
            msg = body.get("message") or body.get("error") or "the portal refused the request"
            if body.get("problems"):
                msg += ": " + "; ".join(body["problems"])
            raise PortalError(msg)
        return body


def make_server(name: str, instructions: str, scope: str, path: str, default_port: int) -> MCPServer:
    """One resource server per audience. The public URL of the server is its RFC 8707
    resource identifier and must match what the token's `aud` says."""
    self_url = os.environ.get("MCP_%s_URL" % scope.upper(),
                              "http://localhost:%d%s" % (default_port, path)).rstrip("/")
    return MCPServer(
        name,
        instructions=instructions,
        token_verifier=PortalTokenVerifier(self_url, scope),
        # plain strings, not AnyHttpUrl(...): the settings model keeps a path-less URL
        # as given, so the advertised issuer matches the portal's `iss` exactly
        auth=AuthSettings(
            issuer_url=PORTAL_PUBLIC_URL,
            resource_server_url=self_url,
            required_scopes=[scope],
            # our verifier checks `aud` itself (and accepts the trailing-slash spelling)
            validate_token_resource=False,
            service_documentation_url=PORTAL_PUBLIC_URL + "/agents",
        ),
    )


def serve(server: MCPServer, path: str, default_port: int):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server.run("streamable-http", host="0.0.0.0", port=int(os.environ.get("PORT", default_port)),
               streamable_http_path=path, stateless_http=True, json_response=True)


def ok(message: str, **fields) -> dict:
    out = {"message": message}
    out.update(fields)
    return out


def problem(message: str, **fields) -> dict:
    out = {"problem": message}
    out.update(fields)
    return out
