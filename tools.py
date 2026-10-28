"""Spotify tools, registered on an MCP server. `get_token` returns a valid Spotify access token."""
import random
import time
from typing import Callable

import httpx

API = "https://api.spotify.com/v1"


def _track(t):
    if not t:
        return None
    return {
        "name": t.get("name"), "uri": t.get("uri"),
        "artists": ", ".join(a["name"] for a in t.get("artists", [])),
        "album": (t.get("album") or {}).get("name"),
        "url": (t.get("external_urls") or {}).get("spotify"),
    }


def _pid(x: str) -> str:
    x = x.strip()
    if x.startswith("spotify:playlist:"):
        return x.split(":")[-1]
    if "open.spotify.com/playlist/" in x:
        return x.split("playlist/")[1].split("?")[0]
    return x


def _ids(uris, n):
    return ",".join(u.split(":")[-1] for u in uris[:n])


def register(mcp, get_token: Callable[[], str]) -> None:
    def req(method, path, params=None, body=None):
        url = path if path.startswith("http") else f"{API}{path}"
        r = None
        for attempt in range(2):
            r = httpx.request(
                method, url,
                params={k: v for k, v in (params or {}).items() if v is not None},
                json=body, headers={"Authorization": f"Bearer {get_token()}"}, timeout=20,
            )
            if r.status_code == 429 and attempt == 0:
                time.sleep(min(int(r.headers.get("Retry-After", "1")), 5))
                continue
            break
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message", r.text)
            except Exception:
                msg = r.text
            return {"error": f"{r.status_code}: {msg}", "_status": r.status_code}
        if not r.content:
            return {"ok": True}
        try:
            return r.json()
        except Exception:
            return {"ok": True}

    def pl_req(method, pid, params=None, body=None):
        """Playlist item endpoints: Spotify renamed /tracks to /items; try new name, fall back."""
        res = req(method, f"/playlists/{pid}/items", params, body)
        if isinstance(res, dict) and res.get("_status") in (404, 405):
            res = req(method, f"/playlists/{pid}/tracks", params, body)
        return res

    def paged(fetch, limit):
        out, offset = [], 0
        while len(out) < limit:
            page = fetch(min(50, limit - len(out)), offset)
            if "error" in page:
                return [page]
            items = page.get("items", [])
            out.extend(items)
            if not page.get("next") or not items:
                break
            offset += len(items)
        return out

    def item_track(i):
        return i.get("track") or i.get("item")

    # ----------------------------------------------------------- account / search
    @mcp.tool()
    def get_profile() -> dict:
        """Get the Spotify profile of the signed-in user (name, country, plan)."""
        return req("GET", "/me")

    @mcp.tool()
    def search(query: str, types: str = "track", limit: int = 10) -> dict:
        """Search Spotify. `types` is comma separated: track,artist,album,playlist,show,episode,audiobook."""
        res = req("GET", "/search", {"q": query, "type": types, "limit": min(limit, 10)})
        if "error" in res:
            return res
        out = {}
        for k, v in res.items():
            items = [i for i in v.get("items", []) if i]
            if k == "tracks":
                out[k] = [_track(i) for i in items]
            else:
                out[k] = [{"name": i.get("name"), "uri": i.get("uri"),
                           "url": (i.get("external_urls") or {}).get("spotify")} for i in items]
        return out

    # ----------------------------------------------------------- playback
    @mcp.tool()
    def get_currently_playing() -> dict:
        """What is playing right now."""
        res = req("GET", "/me/player/currently-playing", {"additional_types": "track,episode"})
        if res.get("item"):
            return {"is_playing": res.get("is_playing"), "progress_ms": res.get("progress_ms"),
                    "track": _track(res["item"])}
        return res

    @mcp.tool()
    def get_playback_state() -> dict:
        """Full playback state: device, shuffle, repeat, volume, track."""
        return req("GET", "/me/player")

    @mcp.tool()
    def get_devices() -> dict:
        """List available Spotify Connect devices."""
        return req("GET", "/me/player/devices")

    @mcp.tool()
    def play(uri: str = "", device_id: str = "", position_ms: int = 0) -> dict:
        """Start or resume playback. `uri` can be a track/episode URI or a context URI
        (album/playlist/artist). Leave empty to resume."""
        body: dict = {}
        if uri.startswith(("spotify:track:", "spotify:episode:")):
            body["uris"] = [uri]
        elif uri:
            body["context_uri"] = uri
        if position_ms:
            body["position_ms"] = position_ms
        return req("PUT", "/me/player/play", {"device_id": device_id or None}, body or None)

    @mcp.tool()
    def play_tracks(uris: list[str], device_id: str = "") -> dict:
        """Play a specific list of track URIs in order."""
        return req("PUT", "/me/player/play", {"device_id": device_id or None}, {"uris": uris})

    @mcp.tool()
    def pause(device_id: str = "") -> dict:
        """Pause playback."""
        return req("PUT", "/me/player/pause", {"device_id": device_id or None})

    @mcp.tool()
    def next_track(device_id: str = "") -> dict:
        """Skip to the next track."""
        return req("POST", "/me/player/next", {"device_id": device_id or None})

    @mcp.tool()
    def previous_track(device_id: str = "") -> dict:
        """Go back to the previous track."""
        return req("POST", "/me/player/previous", {"device_id": device_id or None})

    @mcp.tool()
    def seek(position_ms: int) -> dict:
        """Seek within the current track (milliseconds)."""
        return req("PUT", "/me/player/seek", {"position_ms": position_ms})

    @mcp.tool()
    def set_volume(percent: int) -> dict:
        """Set volume 0-100."""
        return req("PUT", "/me/player/volume", {"volume_percent": max(0, min(100, percent))})

    @mcp.tool()
    def set_shuffle(on: bool) -> dict:
        """Turn shuffle on or off."""
        return req("PUT", "/me/player/shuffle", {"state": str(on).lower()})

    @mcp.tool()
    def set_repeat(mode: str) -> dict:
        """Set repeat mode: 'off', 'track' or 'context'."""
        return req("PUT", "/me/player/repeat", {"state": mode})

    @mcp.tool()
    def add_to_queue(uri: str, device_id: str = "") -> dict:
        """Add a track or episode URI to the queue."""
        return req("POST", "/me/player/queue", {"uri": uri, "device_id": device_id or None})

    @mcp.tool()
    def get_queue() -> dict:
        """Show the playback queue."""
        res = req("GET", "/me/player/queue")
        if "error" in res:
            return res
        return {"now_playing": _track(res.get("currently_playing")),
                "queue": [_track(t) for t in res.get("queue", [])[:25]]}

    @mcp.tool()
    def transfer_playback(device_id: str, play_now: bool = True) -> dict:
        """Move playback to another device."""
        return req("PUT", "/me/player", body={"device_ids": [device_id], "play": play_now})

    # ----------------------------------------------------------- playlists
    @mcp.tool()
    def get_my_playlists(limit: int = 50) -> list:
        """List your playlists (name, id, uri)."""
        items = paged(lambda n, o: req("GET", "/me/playlists", {"limit": n, "offset": o}), limit)
        return [p if "error" in p else {
            "name": p.get("name"), "id": p.get("id"), "uri": p.get("uri"),
            "owner": (p.get("owner") or {}).get("display_name"), "public": p.get("public")}
            for p in items]

    @mcp.tool()
    def get_playlist_tracks(playlist: str, limit: int = 100) -> list:
        """List tracks in a playlist. `playlist` can be an ID, URI or URL."""
        pid = _pid(playlist)
        items = paged(lambda n, o: pl_req("GET", pid, {"limit": n, "offset": o}), limit)
        return [i if "error" in i else _track(item_track(i)) for i in items]

    @mcp.tool()
    def play_random_from_playlist(playlist: str, count: int = 5, device_id: str = "") -> dict:
        """Pick `count` random songs from a playlist and play them now."""
        pid = _pid(playlist)
        items = paged(lambda n, o: pl_req("GET", pid, {"limit": n, "offset": o}), 500)
        if items and "error" in items[0]:
            return items[0]
        tracks = [t for t in (item_track(i) for i in items)
                  if t and str(t.get("uri", "")).startswith("spotify:track:")]
        if not tracks:
            return {"error": "No playable tracks found in that playlist."}
        picks = random.sample(tracks, min(count, len(tracks)))
        res = req("PUT", "/me/player/play", {"device_id": device_id or None},
                  {"uris": [t["uri"] for t in picks]})
        return {"result": res, "playing": [_track(t) for t in picks]}

    @mcp.tool()
    def create_playlist(name: str, description: str = "", public: bool = False) -> dict:
        """Create a new playlist."""
        res = req("POST", "/me/playlists", body={"name": name, "description": description, "public": public})
        if isinstance(res, dict) and res.get("_status") in (404, 405):
            me = req("GET", "/me")
            if "error" not in me:
                res = req("POST", f"/users/{me['id']}/playlists",
                          body={"name": name, "description": description, "public": public})
        return res

    @mcp.tool()
    def update_playlist_details(playlist: str, name: str = "", description: str = "",
                                public: bool | None = None) -> dict:
        """Rename a playlist or change its description / visibility."""
        body = {k: v for k, v in {"name": name or None, "description": description or None,
                                  "public": public}.items() if v is not None}
        return req("PUT", f"/playlists/{_pid(playlist)}", body=body)

    @mcp.tool()
    def add_to_playlist(playlist: str, uris: list[str], position: int | None = None) -> dict:
        """Add track URIs to a playlist (max 100 per call)."""
        body: dict = {"uris": uris[:100]}
        if position is not None:
            body["position"] = position
        return pl_req("POST", _pid(playlist), body=body)

    @mcp.tool()
    def remove_from_playlist(playlist: str, uris: list[str]) -> dict:
        """Remove all occurrences of the given track URIs from a playlist."""
        body = {"tracks": [{"uri": u} for u in uris[:100]]}
        return pl_req("DELETE", _pid(playlist), body=body)

    @mcp.tool()
    def delete_playlist(playlist: str, confirm: bool = False) -> dict:
        """Delete (unfollow) a playlist. Requires confirm=True; ask the user first."""
        if not confirm:
            return {"error": "Refusing to delete without confirm=True. Ask the user first."}
        return req("DELETE", f"/playlists/{_pid(playlist)}/followers")

    # ----------------------------------------------------------- library
    @mcp.tool()
    def get_saved_tracks(limit: int = 50) -> list:
        """Your Liked Songs."""
        items = paged(lambda n, o: req("GET", "/me/tracks", {"limit": n, "offset": o}), limit)
        return [i if "error" in i else _track(i.get("track")) for i in items]

    @mcp.tool()
    def save_tracks(uris: list[str]) -> dict:
        """Like tracks (add to Liked Songs). Track URIs or IDs."""
        return req("PUT", "/me/tracks", {"ids": _ids(uris, 50)})

    @mcp.tool()
    def remove_saved_tracks(uris: list[str]) -> dict:
        """Unlike tracks (remove from Liked Songs)."""
        return req("DELETE", "/me/tracks", {"ids": _ids(uris, 50)})

    @mcp.tool()
    def get_saved_albums(limit: int = 50) -> list:
        """Your saved albums."""
        items = paged(lambda n, o: req("GET", "/me/albums", {"limit": n, "offset": o}), limit)
        return [i if "error" in i else {
            "name": i["album"]["name"], "uri": i["album"]["uri"],
            "artists": ", ".join(a["name"] for a in i["album"]["artists"])} for i in items]

    @mcp.tool()
    def save_albums(uris: list[str]) -> dict:
        """Save albums to your library."""
        return req("PUT", "/me/albums", {"ids": _ids(uris, 20)})

    @mcp.tool()
    def remove_albums(uris: list[str]) -> dict:
        """Remove albums from your library."""
        return req("DELETE", "/me/albums", {"ids": _ids(uris, 20)})

    @mcp.tool()
    def get_followed_artists(limit: int = 50) -> dict:
        """Artists you follow."""
        return req("GET", "/me/following", {"type": "artist", "limit": min(limit, 50)})

    @mcp.tool()
    def follow_artists(uris: list[str]) -> dict:
        """Follow artists by URI or ID."""
        return req("PUT", "/me/following", {"type": "artist", "ids": _ids(uris, 50)})

    @mcp.tool()
    def unfollow_artists(uris: list[str]) -> dict:
        """Unfollow artists by URI or ID."""
        return req("DELETE", "/me/following", {"type": "artist", "ids": _ids(uris, 50)})

    # ----------------------------------------------------------- history
    @mcp.tool()
    def get_top_items(kind: str = "tracks", time_range: str = "medium_term", limit: int = 20) -> dict:
        """Your top tracks or artists. kind: tracks|artists. time_range: short_term|medium_term|long_term."""
        res = req("GET", f"/me/top/{kind}", {"time_range": time_range, "limit": min(limit, 50)})
        if "error" in res:
            return res
        if kind == "tracks":
            return {"items": [_track(t) for t in res.get("items", [])]}
        return {"items": [{"name": a["name"], "uri": a["uri"], "genres": a.get("genres", [])}
                          for a in res.get("items", [])]}

    @mcp.tool()
    def get_recently_played(limit: int = 20) -> list:
        """Recently played tracks."""
        res = req("GET", "/me/player/recently-played", {"limit": min(limit, 50)})
        if "error" in res:
            return [res]
        return [{**_track(i["track"]), "played_at": i["played_at"]} for i in res.get("items", [])]
