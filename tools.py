"""Spotify tools, registered on an MCP server. `get_token` returns a valid Spotify access token."""
import base64
import binascii
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


def _spotify_id(value: str, kind: str) -> str:
    value = value.strip()
    prefix = f"spotify:{kind}:"
    if value.startswith(prefix):
        return value[len(prefix):]
    if "open.spotify.com/" in value:
        parts = value.split("open.spotify.com/", 1)[1].split("?", 1)[0].strip("/").split("/")
        if kind in parts and parts.index(kind) + 1 < len(parts):
            return parts[parts.index(kind) + 1]
    marker = f"open.spotify.com/{kind}/"
    if marker in value:
        return value.split(marker, 1)[1].split("?", 1)[0].split("/", 1)[0]
    return value


def _artist(artist):
    if not artist:
        return None
    images = artist.get("images") or []
    return {
        "name": artist.get("name"), "id": artist.get("id"), "uri": artist.get("uri"),
        "genres": artist.get("genres", []), "popularity": artist.get("popularity"),
        "followers": (artist.get("followers") or {}).get("total"),
        "image": images[0].get("url") if images else None,
        "url": (artist.get("external_urls") or {}).get("spotify"),
    }


def _album(album):
    if not album:
        return None
    images = album.get("images") or []
    return {
        "name": album.get("name"), "id": album.get("id"), "uri": album.get("uri"),
        "artists": [_artist(a) for a in album.get("artists", [])],
        "release_date": album.get("release_date"), "total_tracks": album.get("total_tracks"),
        "image": images[0].get("url") if images else None,
        "url": (album.get("external_urls") or {}).get("spotify"),
    }


def _episode(episode):
    if not episode:
        return None
    show = episode.get("show") or {}
    return {
        "name": episode.get("name"), "id": episode.get("id"), "uri": episode.get("uri"),
        "description": episode.get("description"), "release_date": episode.get("release_date"),
        "duration_ms": episode.get("duration_ms"), "show": show.get("name"),
        "url": (episode.get("external_urls") or {}).get("spotify"),
    }


def _show(show):
    if not show:
        return None
    images = show.get("images") or []
    return {
        "name": show.get("name"), "id": show.get("id"), "uri": show.get("uri"),
        "publisher": show.get("publisher"), "description": show.get("description"),
        "total_episodes": show.get("total_episodes"),
        "image": images[0].get("url") if images else None,
        "url": (show.get("external_urls") or {}).get("spotify"),
    }


def _audiobook(book):
    if not book:
        return None
    authors = book.get("authors") or []
    narrators = book.get("narrators") or []
    images = book.get("images") or []
    return {
        "name": book.get("name"), "id": book.get("id"), "uri": book.get("uri"),
        "authors": [author.get("name") for author in authors],
        "narrators": [narrator.get("name") for narrator in narrators],
        "publisher": book.get("publisher"), "total_chapters": book.get("total_chapters"),
        "image": images[0].get("url") if images else None,
        "url": (book.get("external_urls") or {}).get("spotify"),
    }


def _pid(x: str) -> str:
    x = x.strip()
    if x.startswith("spotify:playlist:"):
        return x.split(":")[-1]
    if "open.spotify.com/playlist/" in x:
        return x.split("playlist/")[1].split("?")[0]
    return x


def _ids(uris, n):
    ids = []
    for uri in uris[:n]:
        value = uri.split("?", 1)[0].rstrip("/")
        ids.append(value.rsplit("/", 1)[-1] if "open.spotify.com/" in value else value.split(":")[-1])
    return ",".join(ids)


def register(mcp, get_token: Callable[[], str]) -> None:
    def req(method, path, params=None, body=None, content=None, content_type=None):
        url = path if path.startswith("http") else f"{API}{path}"
        r = None
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {get_token()}"}
            if content is not None:
                headers["Content-Type"] = content_type or "application/octet-stream"
            r = httpx.request(
                method, url,
                params={k: v for k, v in (params or {}).items() if v is not None},
                json=body, content=content, headers=headers, timeout=20,
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

    def contains(path: str, ids: list[str], limit: int = 50) -> dict:
        return req("GET", path, {"ids": _ids(ids, limit)})

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

    # ----------------------------------------------------------- catalog
    @mcp.tool()
    def get_artist(artist: str) -> dict:
        """Get an artist's profile, genres, popularity, and followers. Accepts an ID, URI, or URL."""
        res = req("GET", f"/artists/{_spotify_id(artist, 'artist')}")
        return _artist(res) if "error" not in res else res

    @mcp.tool()
    def get_artists(artists: list[str]) -> dict:
        """Get details for multiple artists by Spotify ID, URI, or URL (up to 50)."""
        res = req("GET", "/artists", {"ids": _ids(artists, 50)})
        return {"artists": [_artist(a) for a in res.get("artists", [])]} if "error" not in res else res

    @mcp.tool()
    def get_artist_albums(artist: str, include_groups: str = "album,single", limit: int = 20) -> list:
        """List an artist's albums and singles. `include_groups` accepts album,single,compilation,appears_on."""
        artist_id = _spotify_id(artist, "artist")
        items = paged(lambda n, o: req("GET", f"/artists/{artist_id}/albums", {
            "include_groups": include_groups, "limit": n, "offset": o}), min(max(limit, 0), 50))
        return [i if "error" in i else _album(i) for i in items]

    @mcp.tool()
    def get_artist_top_tracks(artist: str, market: str = "") -> list:
        """Get an artist's top tracks, optionally for a two-letter market code."""
        res = req("GET", f"/artists/{_spotify_id(artist, 'artist')}/top-tracks", {"market": market or None})
        return [_track(t) for t in res.get("tracks", [])] if "error" not in res else res

    @mcp.tool()
    def get_related_artists(artist: str) -> list:
        """Find artists related to an artist. Spotify may restrict this endpoint for some apps."""
        res = req("GET", f"/artists/{_spotify_id(artist, 'artist')}/related-artists")
        return [_artist(a) for a in res.get("artists", [])] if "error" not in res else res

    @mcp.tool()
    def get_album(album: str, market: str = "") -> dict:
        """Get album details by Spotify ID, URI, or URL."""
        res = req("GET", f"/albums/{_spotify_id(album, 'album')}", {"market": market or None})
        return _album(res) if "error" not in res else res

    @mcp.tool()
    def get_albums(albums: list[str], market: str = "") -> dict:
        """Get multiple albums by Spotify ID, URI, or URL (up to 20)."""
        res = req("GET", "/albums", {"ids": _ids(albums, 20), "market": market or None})
        return {"albums": [_album(a) for a in res.get("albums", [])]} if "error" not in res else res

    @mcp.tool()
    def get_album_tracks(album: str, limit: int = 50, market: str = "") -> list:
        """List the tracks on an album."""
        album_id = _spotify_id(album, "album")
        items = paged(lambda n, o: req("GET", f"/albums/{album_id}/tracks", {
            "limit": n, "offset": o, "market": market or None}), min(max(limit, 0), 50))
        return [i if "error" in i else _track(i) for i in items]

    @mcp.tool()
    def get_track(track: str, market: str = "") -> dict:
        """Get track details by Spotify ID, URI, or URL."""
        res = req("GET", f"/tracks/{_spotify_id(track, 'track')}", {"market": market or None})
        return _track(res) if "error" not in res else res

    @mcp.tool()
    def get_tracks(tracks: list[str], market: str = "") -> dict:
        """Get multiple tracks by Spotify ID, URI, or URL (up to 50)."""
        res = req("GET", "/tracks", {"ids": _ids(tracks, 50), "market": market or None})
        return {"tracks": [_track(t) for t in res.get("tracks", [])]} if "error" not in res else res

    @mcp.tool()
    def get_track_audio_features(track: str) -> dict:
        """Get a track's audio features such as tempo, energy, and danceability; may be unavailable to some apps."""
        return req("GET", f"/audio-features/{_spotify_id(track, 'track')}")

    @mcp.tool()
    def get_tracks_audio_features(tracks: list[str]) -> dict:
        """Get audio features for multiple tracks (up to 100); may be unavailable to some apps."""
        return req("GET", "/audio-features", {"ids": _ids(tracks, 100)})

    @mcp.tool()
    def get_track_audio_analysis(track: str) -> dict:
        """Get detailed section, bar, beat, and segment analysis for a track; may be unavailable to some apps."""
        return req("GET", f"/audio-analysis/{_spotify_id(track, 'track')}")

    @mcp.tool()
    def get_recommendation_genres() -> dict:
        """List genre seeds for Spotify recommendations; the endpoint may be unavailable to some apps."""
        return req("GET", "/recommendations/available-genre-seeds")

    @mcp.tool()
    def get_recommendations(seed_artists: list[str] | None = None, seed_genres: list[str] | None = None,
                            seed_tracks: list[str] | None = None, limit: int = 20,
                            target_energy: float | None = None, target_danceability: float | None = None,
                            target_popularity: int | None = None) -> dict:
        """Get Spotify recommendations from 1-5 artist, genre, or track seeds; endpoint access may be restricted."""
        seed_artists = seed_artists or []
        seed_genres = seed_genres or []
        seed_tracks = seed_tracks or []
        if not 1 <= len(seed_artists) + len(seed_genres) + len(seed_tracks) <= 5:
            return {"error": "Provide between 1 and 5 total artist, genre, or track seeds."}
        params = {
            "seed_artists": _ids(seed_artists, 5) or None,
            "seed_genres": ",".join(seed_genres[:5]) or None,
            "seed_tracks": _ids(seed_tracks, 5) or None,
            "limit": min(max(limit, 1), 100),
            "target_energy": target_energy,
            "target_danceability": target_danceability,
            "target_popularity": target_popularity,
        }
        return req("GET", "/recommendations", params)

    @mcp.tool()
    def get_show(show: str, market: str = "") -> dict:
        """Get podcast show details by Spotify ID, URI, or URL."""
        res = req("GET", f"/shows/{_spotify_id(show, 'show')}", {"market": market or None})
        return _show(res) if "error" not in res else res

    @mcp.tool()
    def get_shows(shows: list[str], market: str = "") -> dict:
        """Get multiple podcast shows by Spotify ID, URI, or URL (up to 50)."""
        res = req("GET", "/shows", {"ids": _ids(shows, 50), "market": market or None})
        return {"shows": [_show(s) for s in res.get("shows", [])]} if "error" not in res else res

    @mcp.tool()
    def get_show_episodes(show: str, limit: int = 20, market: str = "") -> list:
        """List episodes of a podcast show."""
        show_id = _spotify_id(show, "show")
        items = paged(lambda n, o: req("GET", f"/shows/{show_id}/episodes", {
            "limit": n, "offset": o, "market": market or None}), min(max(limit, 0), 50))
        return [i if "error" in i else _episode(i) for i in items]

    @mcp.tool()
    def get_episode(episode: str, market: str = "") -> dict:
        """Get podcast episode details by Spotify ID, URI, or URL."""
        res = req("GET", f"/episodes/{_spotify_id(episode, 'episode')}", {"market": market or None})
        return _episode(res) if "error" not in res else res

    @mcp.tool()
    def get_episodes(episodes: list[str], market: str = "") -> dict:
        """Get multiple podcast episodes by Spotify ID, URI, or URL (up to 50)."""
        res = req("GET", "/episodes", {"ids": _ids(episodes, 50), "market": market or None})
        return {"episodes": [_episode(e) for e in res.get("episodes", [])]} if "error" not in res else res

    @mcp.tool()
    def get_audiobook(audiobook: str, market: str = "") -> dict:
        """Get audiobook details by Spotify ID, URI, or URL."""
        res = req("GET", f"/audiobooks/{_spotify_id(audiobook, 'audiobook')}", {"market": market or None})
        return _audiobook(res) if "error" not in res else res

    @mcp.tool()
    def get_audiobooks(audiobooks: list[str], market: str = "") -> dict:
        """Get multiple audiobooks by Spotify ID, URI, or URL (up to 50)."""
        res = req("GET", "/audiobooks", {"ids": _ids(audiobooks, 50), "market": market or None})
        return {"audiobooks": [_audiobook(b) for b in res.get("audiobooks", [])]} if "error" not in res else res

    @mcp.tool()
    def get_audiobook_chapters(audiobook: str, limit: int = 50, market: str = "") -> list:
        """List chapters in an audiobook."""
        audiobook_id = _spotify_id(audiobook, "audiobook")
        items = paged(lambda n, o: req("GET", f"/audiobooks/{audiobook_id}/chapters", {
            "limit": n, "offset": o, "market": market or None}), min(max(limit, 0), 50))
        return items

    @mcp.tool()
    def get_chapter(chapter: str, market: str = "") -> dict:
        """Get audiobook chapter details by Spotify ID, URI, or URL."""
        return req("GET", f"/chapters/{_spotify_id(chapter, 'chapter')}", {"market": market or None})

    @mcp.tool()
    def get_chapters(chapters: list[str], market: str = "") -> dict:
        """Get multiple audiobook chapters by Spotify ID, URI, or URL (up to 50)."""
        return req("GET", "/chapters", {"ids": _ids(chapters, 50), "market": market or None})

    # ----------------------------------------------------------- discovery
    @mcp.tool()
    def get_new_releases(country: str = "", limit: int = 20) -> dict:
        """Browse new album releases, optionally filtered by country."""
        return req("GET", "/browse/new-releases", {"country": country or None, "limit": min(max(limit, 1), 50)})

    @mcp.tool()
    def get_featured_playlists(country: str = "", limit: int = 20) -> dict:
        """Browse Spotify's featured playlists."""
        return req("GET", "/browse/featured-playlists", {"country": country or None, "limit": min(max(limit, 1), 50)})

    @mcp.tool()
    def get_categories(country: str = "", limit: int = 20) -> dict:
        """List Spotify browse categories."""
        return req("GET", "/browse/categories", {"country": country or None, "limit": min(max(limit, 1), 50)})

    @mcp.tool()
    def get_category(category: str, country: str = "") -> dict:
        """Get details for a Spotify browse category by category ID."""
        return req("GET", f"/browse/categories/{category}", {"country": country or None})

    @mcp.tool()
    def get_category_playlists(category: str, country: str = "", limit: int = 20) -> dict:
        """Browse playlists in a category by category ID."""
        return req("GET", f"/browse/categories/{category}/playlists", {
            "country": country or None, "limit": min(max(limit, 1), 50)})

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
    def get_playlist(playlist: str, market: str = "") -> dict:
        """Get playlist details including owner, description, visibility, and follower count."""
        return req("GET", f"/playlists/{_pid(playlist)}", {"market": market or None})

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
    def reorder_playlist_tracks(playlist: str, range_start: int, insert_before: int,
                                range_length: int = 1, snapshot_id: str = "") -> dict:
        """Move a range of playlist tracks to a new position."""
        body = {"range_start": range_start, "insert_before": insert_before, "range_length": range_length}
        if snapshot_id:
            body["snapshot_id"] = snapshot_id
        return pl_req("PUT", _pid(playlist), body=body)

    @mcp.tool()
    def replace_playlist_tracks(playlist: str, uris: list[str], confirm: bool = False) -> dict:
        """Replace all playlist tracks (up to 100). Requires confirm=True because this overwrites the playlist."""
        if not confirm:
            return {"error": "Refusing to replace playlist tracks without confirm=True. Ask the user first."}
        return pl_req("PUT", _pid(playlist), body={"uris": uris[:100]})

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

    @mcp.tool()
    def follow_playlist(playlist: str, public: bool = False) -> dict:
        """Follow a playlist. Set public=True to make it visible on your profile."""
        return req("PUT", f"/playlists/{_pid(playlist)}/followers", body={"public": public})

    @mcp.tool()
    def unfollow_playlist(playlist: str, confirm: bool = False) -> dict:
        """Unfollow a playlist. Requires confirm=True; ask the user first."""
        if not confirm:
            return {"error": "Refusing to unfollow playlist without confirm=True. Ask the user first."}
        return req("DELETE", f"/playlists/{_pid(playlist)}/followers")

    @mcp.tool()
    def set_playlist_cover(playlist: str, image_base64: str) -> dict:
        """Set a playlist cover from a base64-encoded JPEG image (maximum 256 KB)."""
        try:
            image = base64.b64decode(image_base64, validate=True)
        except (binascii.Error, ValueError):
            return {"error": "image_base64 must contain valid base64-encoded JPEG data."}
        if len(image) > 256_000:
            return {"error": "The JPEG image must be no larger than 256 KB."}
        if not image.startswith(b"\xff\xd8\xff"):
            return {"error": "The uploaded cover must be a JPEG image."}
        return req("PUT", f"/playlists/{_pid(playlist)}/images", content=image_base64, content_type="image/jpeg")

    @mcp.tool()
    def get_playlist_cover(playlist: str) -> dict:
        """Get a playlist's cover image URLs and dimensions."""
        return req("GET", f"/playlists/{_pid(playlist)}/images")

    @mcp.tool()
    def check_playlist_followed(playlist: str, user_ids: list[str] | None = None) -> dict:
        """Check whether specified Spotify users follow a playlist; omit IDs to check your own account."""
        if not user_ids:
            profile = req("GET", "/me")
            if "error" in profile:
                return profile
            user_ids = [profile["id"]]
        params = {"ids": ",".join(user_ids[:5])}
        return req("GET", f"/playlists/{_pid(playlist)}/followers/contains", params)

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
    def check_saved_tracks(uris: list[str]) -> dict:
        """Check which tracks are in your Liked Songs."""
        return req("GET", "/me/tracks/contains", {"ids": _ids(uris, 50)})

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
    def check_saved_albums(uris: list[str]) -> dict:
        """Check which albums are saved in your library."""
        return req("GET", "/me/albums/contains", {"ids": _ids(uris, 20)})

    @mcp.tool()
    def get_saved_shows(limit: int = 50) -> list:
        """List podcast shows saved in your library."""
        items = paged(lambda n, o: req("GET", "/me/shows", {"limit": n, "offset": o}),
                      min(max(limit, 0), 50))
        return [i if "error" in i else {"added_at": i.get("added_at"), "show": _show(i.get("show"))}
                for i in items]

    @mcp.tool()
    def save_shows(shows: list[str]) -> dict:
        """Save podcast shows by Spotify ID, URI, or URL."""
        return req("PUT", "/me/shows", {"ids": _ids(shows, 50)})

    @mcp.tool()
    def remove_shows(shows: list[str]) -> dict:
        """Remove podcast shows from your library."""
        return req("DELETE", "/me/shows", {"ids": _ids(shows, 50)})

    @mcp.tool()
    def check_saved_shows(shows: list[str]) -> dict:
        """Check which podcast shows are saved in your library."""
        return req("GET", "/me/shows/contains", {"ids": _ids(shows, 50)})

    @mcp.tool()
    def get_saved_episodes(limit: int = 50) -> list:
        """List podcast episodes saved in your library."""
        items = paged(lambda n, o: req("GET", "/me/episodes", {"limit": n, "offset": o}),
                      min(max(limit, 0), 50))
        return [i if "error" in i else {"added_at": i.get("added_at"), "episode": _episode(i.get("episode"))}
                for i in items]

    @mcp.tool()
    def save_episodes(episodes: list[str]) -> dict:
        """Save podcast episodes by Spotify ID, URI, or URL."""
        return req("PUT", "/me/episodes", {"ids": _ids(episodes, 50)})

    @mcp.tool()
    def remove_episodes(episodes: list[str]) -> dict:
        """Remove podcast episodes from your library."""
        return req("DELETE", "/me/episodes", {"ids": _ids(episodes, 50)})

    @mcp.tool()
    def check_saved_episodes(episodes: list[str]) -> dict:
        """Check which podcast episodes are saved in your library."""
        return req("GET", "/me/episodes/contains", {"ids": _ids(episodes, 50)})

    @mcp.tool()
    def get_saved_audiobooks(limit: int = 50) -> list:
        """List audiobooks saved in your library."""
        items = paged(lambda n, o: req("GET", "/me/audiobooks", {"limit": n, "offset": o}),
                      min(max(limit, 0), 50))
        return [i if "error" in i else {"added_at": i.get("added_at"), "audiobook": _audiobook(i.get("audiobook"))}
                for i in items]

    @mcp.tool()
    def save_audiobooks(audiobooks: list[str]) -> dict:
        """Save audiobooks by Spotify ID, URI, or URL."""
        return req("PUT", "/me/audiobooks", {"ids": _ids(audiobooks, 50)})

    @mcp.tool()
    def remove_audiobooks(audiobooks: list[str]) -> dict:
        """Remove audiobooks from your library."""
        return req("DELETE", "/me/audiobooks", {"ids": _ids(audiobooks, 50)})

    @mcp.tool()
    def check_saved_audiobooks(audiobooks: list[str]) -> dict:
        """Check which audiobooks are saved in your library."""
        return req("GET", "/me/audiobooks/contains", {"ids": _ids(audiobooks, 50)})

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

    @mcp.tool()
    def check_followed_artists(uris: list[str]) -> dict:
        """Check which artists you follow."""
        return req("GET", "/me/following/contains", {"type": "artist", "ids": _ids(uris, 50)})

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
