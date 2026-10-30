"""
Hosted Spotify MCP server (for Render / any HTTPS host).

Claude, ChatGPT, and Grok connect to https://<your-service>/mcp as custom connectors.
This server acts as its own OAuth authorization server, and delegates the actual login to
Spotify. It keeps NO database: every token and code is an encrypted, signed blob (Fernet),
so restarts and free-tier spin-downs never log you out.

Required env:  SPOTIFY_CLIENT_ID, SECRET_KEY, PUBLIC_URL (or Render's RENDER_EXTERNAL_URL)
Optional env:  ALLOWED_SPOTIFY_USER_IDS (comma list), ALLOWED_REDIRECTS (comma list), PORT
"""
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import time
import urllib.parse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from pydantic import AnyHttpUrl, AnyUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken, TokenError,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull, OAuthToken
from mcp.server.transport_security import TransportSecuritySettings

import tools

log = logging.getLogger("spotify-mcp")
logging.basicConfig(level=logging.INFO)

# ------------------------------------------------------------------ config
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
_secret = os.environ.get("SECRET_KEY", "")
if not _secret:
    _secret = secrets.token_urlsafe(32)
    log.warning("SECRET_KEY not set: using a random one. Logins will break on every restart.")
FERNET = Fernet(base64.urlsafe_b64encode(hashlib.sha256(_secret.encode()).digest()))

ALLOWED_USERS = {u.strip() for u in os.environ.get("ALLOWED_SPOTIFY_USER_IDS", "").split(",") if u.strip()}
ALLOWED_REDIRECTS = {u.strip() for u in os.environ.get(
    "ALLOWED_REDIRECTS",
    "https://claude.ai/api/mcp/auth_callback,"
    "https://claude.com/api/mcp/auth_callback,"
    "https://chatgpt.com/api/mcp/auth_callback,"
    "https://chat.openai.com/api/mcp/auth_callback,"
    "https://grok.com/api/mcp/auth_callback,"
    "https://grok.com/connectors-oauth-exchange-code/,"
    "https://x.ai/api/mcp/auth_callback",
).split(",") if u.strip()}
LOOPBACK = re.compile(r"^http://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(/|$)")

ACCOUNTS = "https://accounts.spotify.com"
SPOTIFY_SCOPES = " ".join([
    "user-read-private", "user-read-email",
    "user-read-playback-state", "user-modify-playback-state", "user-read-currently-playing",
    "user-read-recently-played", "user-top-read",
    "user-library-read", "user-library-modify",
    "playlist-read-private", "playlist-read-collaborative",
    "playlist-modify-public", "playlist-modify-private",
    "user-follow-read", "user-follow-modify",
])
ACCESS_TTL = 3600
REFRESH_TTL = 60 * 24 * 3600
CODE_TTL = 300
STATE_TTL = 600
SCOPE = "spotify"


def seal(data: dict) -> str:
    return FERNET.encrypt(json.dumps(data, separators=(",", ":")).encode()).decode()


def unseal(token: str, ttl: int | None = None) -> dict | None:
    try:
        return json.loads(FERNET.decrypt(token.encode(), ttl=ttl))
    except (InvalidToken, ValueError, TypeError):
        return None


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def callback_url() -> str:
    return f"{PUBLIC_URL}/spotify/callback"


# ------------------------------------------------------------------ OAuth client model
class OpenClient(OAuthClientInformationFull):
    """Any dynamically registered client is accepted, but only with approved redirect URIs."""

    def validate_redirect_uri(self, redirect_uri):
        if redirect_uri is None:
            raise InvalidRedirectUriError("redirect_uri is required")
        u = str(redirect_uri)
        if u in ALLOWED_REDIRECTS or LOOPBACK.match(u):
            return redirect_uri
        raise InvalidRedirectUriError(f"Redirect URI '{u}' is not allowed")

    def validate_scope(self, requested_scope):
        return [SCOPE]


def _client(client_id: str) -> OpenClient:
    return OpenClient(
        client_id=client_id, redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")],
        token_endpoint_auth_method="none", grant_types=["authorization_code", "refresh_token"],
        response_types=["code"], scope=SCOPE,
    )


# ------------------------------------------------------------------ OAuth provider
class SpotifyBackedProvider:
    async def get_client(self, client_id: str):
        return _client(client_id)

    async def register_client(self, client_info) -> None:
        return None  # stateless: every client is accepted by get_client

    async def authorize(self, client, params: AuthorizationParams) -> str:
        verifier = secrets.token_urlsafe(64)
        challenge = b64url(hashlib.sha256(verifier.encode()).digest())
        state = seal({
            "c": client.client_id, "ru": str(params.redirect_uri),
            "ex": params.redirect_uri_provided_explicitly, "cc": params.code_challenge,
            "s": params.state, "res": params.resource, "v": verifier,
        })
        return f"{ACCOUNTS}/authorize?" + urllib.parse.urlencode({
            "client_id": SPOTIFY_CLIENT_ID, "response_type": "code",
            "redirect_uri": callback_url(), "scope": SPOTIFY_SCOPES, "state": state,
            "code_challenge_method": "S256", "code_challenge": challenge,
        })

    async def load_authorization_code(self, client, authorization_code: str):
        d = unseal(authorization_code, ttl=CODE_TTL)
        if not d or d.get("t") != "code" or d.get("c") != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code, scopes=[SCOPE], expires_at=time.time() + CODE_TTL,
            client_id=client.client_id, code_challenge=d["cc"], redirect_uri=AnyUrl(d["ru"]),
            redirect_uri_provided_explicitly=d["ex"], resource=d.get("res"), subject=d.get("sub"),
        )

    def _issue(self, client_id: str, rt: str, sub: str | None, resource: str | None) -> OAuthToken:
        now = int(time.time())
        access = seal({"t": "a", "rt": rt, "sub": sub, "c": client_id, "res": resource, "exp": now + ACCESS_TTL})
        refresh = seal({"t": "r", "rt": rt, "sub": sub, "c": client_id, "res": resource, "exp": now + REFRESH_TTL})
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          scope=SCOPE, refresh_token=refresh)

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        d = unseal(authorization_code.code, ttl=CODE_TTL)
        if not d:
            raise TokenError(error="invalid_grant", error_description="authorization code expired")
        return self._issue(client.client_id, d["rt"], d.get("sub"), d.get("res"))

    async def load_refresh_token(self, client, refresh_token: str):
        d = unseal(refresh_token)
        if not d or d.get("t") != "r" or d.get("c") != client.client_id or d.get("exp", 0) < time.time():
            return None
        return RefreshToken(token=refresh_token, client_id=client.client_id, scopes=[SCOPE],
                            expires_at=d["exp"], resource=d.get("res"), subject=d.get("sub"))

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        d = unseal(refresh_token.token)
        if not d:
            raise TokenError(error="invalid_grant", error_description="invalid refresh token")
        return self._issue(client.client_id, d["rt"], d.get("sub"), d.get("res"))

    async def load_access_token(self, token: str):
        d = unseal(token)
        if not d or d.get("t") != "a" or d.get("exp", 0) < time.time():
            return None
        return AccessToken(token=token, client_id=d["c"], scopes=[SCOPE], expires_at=d["exp"],
                           resource=d.get("res"), subject=d.get("sub"))

    async def revoke_token(self, token) -> None:
        return None  # stateless; revoke access from your Spotify account page to cut it off for good


provider = SpotifyBackedProvider()

# ------------------------------------------------------------------ MCP server
mcp = FastMCP(
    "spotify",
    auth_server_provider=provider,
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(PUBLIC_URL or "http://localhost:10000"),
        resource_server_url=AnyHttpUrl((PUBLIC_URL or "http://localhost:10000") + "/mcp"),
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[],
        validate_token_resource=False,
    ),
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "10000")),
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

_spotify_cache: dict[str, tuple[str, float]] = {}


def spotify_token() -> str:
    at = get_access_token()
    if at is None:
        raise RuntimeError("Not authenticated")
    d = unseal(at.token)
    if not d:
        raise RuntimeError("Invalid token")
    rt = d["rt"]
    key = hashlib.sha256(rt.encode()).hexdigest()
    cached = _spotify_cache.get(key)
    if cached and cached[1] > time.time():
        return cached[0]
    r = httpx.post(f"{ACCOUNTS}/api/token", data={
        "grant_type": "refresh_token", "refresh_token": rt, "client_id": SPOTIFY_CLIENT_ID}, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Spotify refresh failed ({r.status_code}). Reconnect the connector.")
    new = r.json()
    _spotify_cache[key] = (new["access_token"], time.time() + new.get("expires_in", 3600) - 60)
    return new["access_token"]


tools.register(mcp, spotify_token)


def _back(st: dict, **params) -> RedirectResponse:
    if st.get("s") is not None:
        params["state"] = st["s"]
    sep = "&" if "?" in st["ru"] else "?"
    return RedirectResponse(st["ru"] + sep + urllib.parse.urlencode(params), status_code=302)


@mcp.custom_route("/spotify/callback", methods=["GET"])
async def spotify_callback(request: Request):
    q = request.query_params
    st = unseal(q.get("state", ""), ttl=STATE_TTL)
    if not st:
        return PlainTextResponse("Login expired or invalid. Start the connection again from Claude.", status_code=400)
    if "error" in q or "code" not in q:
        return _back(st, error="access_denied", error_description=q.get("error", "cancelled"))
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{ACCOUNTS}/api/token", data={
            "grant_type": "authorization_code", "code": q["code"], "redirect_uri": callback_url(),
            "client_id": SPOTIFY_CLIENT_ID, "code_verifier": st["v"]})
        if r.status_code != 200:
            log.warning("Spotify token exchange failed: %s %s", r.status_code, r.text[:200])
            return _back(st, error="server_error", error_description="Spotify token exchange failed")
        tok = r.json()
        me = await c.get("https://api.spotify.com/v1/me",
                         headers={"Authorization": f"Bearer {tok['access_token']}"})
    uid = me.json().get("id") if me.status_code == 200 else None
    if ALLOWED_USERS and uid not in ALLOWED_USERS:
        log.warning("Rejected Spotify user %s", uid)
        return _back(st, error="access_denied", error_description="This Spotify account is not allowed")
    if "refresh_token" not in tok:
        return _back(st, error="server_error", error_description="Spotify returned no refresh token")
    code = seal({"t": "code", "c": st["c"], "ru": st["ru"], "ex": st["ex"], "cc": st["cc"],
                 "res": st.get("res"), "rt": tok["refresh_token"], "sub": uid})
    return _back(st, code=code)


@mcp.custom_route("/", methods=["GET"])
async def root(request: Request):
    return JSONResponse({"service": "spotify-mcp", "mcp_endpoint": f"{PUBLIC_URL}/mcp", "status": "ok"})


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request):
    return PlainTextResponse("ok")


def check_config() -> None:
    missing = [n for n, v in {"SPOTIFY_CLIENT_ID": SPOTIFY_CLIENT_ID, "PUBLIC_URL": PUBLIC_URL}.items() if not v]
    if missing:
        sys.exit(f"Missing required env: {', '.join(missing)}")
    if not PUBLIC_URL.startswith("https://") and "localhost" not in PUBLIC_URL:
        log.warning("PUBLIC_URL is not https; Claude requires HTTPS for remote connectors.")


KEEPALIVE_SECONDS = int(os.environ.get("KEEPALIVE_SECONDS", "600"))  # 0 disables; Render sleeps after 15 min idle


def keepalive_loop(url: str, interval: int, sleep=time.sleep, get=None) -> None:
    """Ping our own public URL so Render sees inbound traffic and keeps the free instance awake."""
    get = get or httpx.get
    while True:
        sleep(interval)
        try:
            r = get(url, timeout=30)
            log.info("keepalive %s -> %s", url, r.status_code)
        except Exception as e:  # never let the pinger die
            log.warning("keepalive failed: %s", e)


def start_keepalive() -> None:
    import threading
    if KEEPALIVE_SECONDS <= 0:
        log.info("Keep-alive disabled")
        return
    if not PUBLIC_URL.startswith("https://"):
        log.info("Keep-alive skipped (PUBLIC_URL is not a public https URL)")
        return
    threading.Thread(target=keepalive_loop, args=(f"{PUBLIC_URL}/healthz", KEEPALIVE_SECONDS),
                     daemon=True, name="keepalive").start()
    log.info("Keep-alive started: pinging %s/healthz every %ss", PUBLIC_URL, KEEPALIVE_SECONDS)


if __name__ == "__main__":
    check_config()
    start_keepalive()
    log.info("MCP endpoint: %s/mcp   Spotify redirect URI to register: %s", PUBLIC_URL, callback_url())
    mcp.run(transport="streamable-http")