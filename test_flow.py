"""End-to-end test of the hosted server with Spotify mocked out. Run: python test_flow.py"""
import base64, hashlib, json, os, secrets, sys, urllib.parse

os.environ.update(SPOTIFY_CLIENT_ID="testclient", PUBLIC_URL="https://example.onrender.com",
                  SECRET_KEY="test-secret", ALLOWED_SPOTIFY_USER_IDS="zohaib")

import httpx
from starlette.testclient import TestClient

import app as A

CALLS = []


def fake_response(status, data, url="https://x"):
    return httpx.Response(status, json=data, request=httpx.Request("GET", url))


def spotify_sync(method, url, **kw):
    CALLS.append((method, url, kw.get("json")))
    if url.endswith("/me/playlists"):
        return fake_response(200, {"items": [{"name": "BEST FOR ME", "id": "PL1", "uri": "spotify:playlist:PL1",
                                              "owner": {"display_name": "Zohaib"}, "public": False}]})
    if "/playlists/PL1/items" in url:
        return fake_response(404, {"error": {"message": "not found"}})  # force the /tracks fallback
    if "/playlists/PL1/tracks" in url:
        items = [{"track": {"name": f"Song {i}", "uri": f"spotify:track:T{i}", "artists": [{"name": "A"}],
                            "album": {"name": "Alb"}, "external_urls": {"spotify": "u"}}} for i in range(12)]
        return fake_response(200, {"items": items, "next": None})
    if url.endswith("/me/player/play"):
        return httpx.Response(204, request=httpx.Request("PUT", url))
    return fake_response(200, {"id": "zohaib"})


def spotify_post(url, data=None, **kw):
    assert "accounts.spotify.com" in url
    return fake_response(200, {"access_token": "SP_ACCESS", "refresh_token": "SP_REFRESH", "expires_in": 3600})


class FakeAsync:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, data=None, **kw):
        assert data["code_verifier"] and data["redirect_uri"].endswith("/spotify/callback"), data
        return fake_response(200, {"access_token": "SP_ACCESS", "refresh_token": "SP_REFRESH", "expires_in": 3600})
    async def get(self, url, headers=None, **kw):
        return fake_response(200, {"id": "zohaib"})


httpx.request = spotify_sync
httpx.post = spotify_post
httpx.AsyncClient = FakeAsync
A.httpx = httpx


def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


REDIRECT = "https://claude.ai/api/mcp/auth_callback"
verifier = secrets.token_urlsafe(64)
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

with TestClient(A.mcp.streamable_http_app(), base_url="https://example.onrender.com", follow_redirects=False) as c:
    r = c.get("/.well-known/oauth-authorization-server")
    ok(r.status_code == 200 and "registration_endpoint" in r.json(), "OAuth metadata published")

    r = c.get("/.well-known/oauth-protected-resource/mcp")
    ok(r.status_code == 200, f"protected-resource metadata ({r.status_code})")

    r = c.post("/register", json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
                                  "client_name": "Claude", "grant_types": ["authorization_code", "refresh_token"],
                                  "response_types": ["code"]})
    ok(r.status_code in (200, 201), f"dynamic client registration ({r.status_code})")
    cid = r.json()["client_id"]

    bad = c.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": "https://evil.com/cb",
                                      "code_challenge": challenge, "code_challenge_method": "S256", "state": "s"})
    ok(bad.status_code == 400 or "evil.com" not in bad.headers.get("location", ""), "evil redirect_uri rejected")

    r = c.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
                                    "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz",
                                    "scope": "spotify"})
    loc = r.headers.get("location", "")
    ok(r.status_code == 302 and "accounts.spotify.com/authorize" in loc, "authorize redirects to Spotify login")
    sp_state = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["state"][0]

    r = c.get("/spotify/callback", params={"code": "SPCODE", "state": sp_state})
    loc = r.headers.get("location", "")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    ok(r.status_code == 302 and loc.startswith(REDIRECT) and q.get("state") == ["xyz"] and "code" in q,
       "callback returns code + original state to Claude")
    code = q["code"][0]

    r = c.post("/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                               "client_id": cid, "redirect_uri": REDIRECT})
    ok(r.status_code == 200 and "access_token" in r.json(), f"token exchange ({r.status_code} {r.text[:120]})")
    tok = r.json()

    r = c.post("/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": "wrong" * 12,
                               "client_id": cid, "redirect_uri": REDIRECT})
    ok(r.status_code == 400, "wrong PKCE verifier rejected")

    r = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid})
    ok(r.status_code == 200 and "access_token" in r.json(), "refresh token grant")

    r = c.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    ok(r.status_code == 401, "MCP rejects requests without a token")

    AH = {**H, "Authorization": f"Bearer {tok['access_token']}"}
    r = c.post("/mcp", headers=AH, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    ok(r.status_code == 200, f"initialize ({r.status_code} {r.text[:120]})")

    r = c.post("/mcp", headers=AH, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in r.json()["result"]["tools"]]
    ok(len(names) >= 30 and "play_random_from_playlist" in names, f"tools/list returns {len(names)} tools")

    r = c.post("/mcp", headers=AH, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "play_random_from_playlist", "arguments": {"playlist": "spotify:playlist:PL1", "count": 4}}})
    res = r.json()["result"]
    body = json.loads(res["content"][0]["text"]) if res.get("content") else {}
    play = [x for x in CALLS if x[1].endswith("/me/player/play")]
    ok(not res.get("isError") and play and len(play[-1][2]["uris"]) == 4, f"random playlist playback: {play[-1][2] if play else res}")

    r = c.post("/mcp", headers=AH, json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
        "name": "delete_playlist", "arguments": {"playlist": "PL1"}}})
    ok("confirm=True" in r.text, "delete_playlist refuses without confirm")

print("ALL TESTS PASSED")
