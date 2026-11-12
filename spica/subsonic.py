"""Navidrome over the Subsonic API.

The radio treats Navidrome as a service, not as a set of files: the index is
paged out of `getAlbumList2` + `getAlbum`, and tracks are played by handing
Liquidsoap a `rest/stream?id=...` URL. Nothing here needs Navidrome's database,
its storage layout, or a mount of the music tree.

Credentials are the operator's: NAVIDROME_URL, NAVIDROME_USER, NAVIDROME_PASS.
Requests use Subsonic token auth (a salted md5) so the password is never in a
URL, which matters because the URLs it produces end up in pool files and the
queue file that Liquidsoap reads.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

API_VERSION = "1.16.1"
CLIENT = "radio"
ALBUM_PAGE = 500


def _salt_file() -> Path:
    return config.STATE_DIR / ".subsonic-salt"


_SALT: str | None = None


def _salt() -> str:
    """One salt per installation, kept on disk so URLs stay valid across restarts.

    Cached in memory: it was read off disk on every API call and every stream URL.
    Creation is exclusive, so the two processes that start together agree on one
    salt instead of each writing its own and leaving the file holding whichever
    finished last.
    """
    global _SALT
    if _SALT:
        return _SALT
    path = _salt_file()
    try:
        _SALT = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        _SALT = ""
    if not _SALT:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8") as fh:
                fh.write(secrets.token_hex(8) + "\n")
        except FileExistsError:
            pass  # somebody else won the creation; use theirs
        _SALT = path.read_text(encoding="utf-8").strip() or _new_salt(path)
    return _SALT


def _new_salt(path: Path) -> str:
    """Last resort when the file exists but is empty: rewrite it in place."""
    salt = secrets.token_hex(8)
    path.write_text(salt + "\n", encoding="utf-8")
    return salt


def _auth() -> dict[str, str]:
    salt = _salt()
    token = hashlib.md5((config.NAVIDROME_PASS + salt).encode("utf-8")).hexdigest()
    return {
        "u": config.NAVIDROME_USER,
        "t": token,
        "s": salt,
        "v": API_VERSION,
        "c": CLIENT,
        "f": "json",
    }


def _base() -> str:
    return config.NAVIDROME_URL.rstrip("/")


def call(method: str, **params) -> dict:
    """One API call, returning the `subsonic-response` body. Raises on failure."""
    query = urllib.parse.urlencode({**_auth(), **params})
    url = f"{_base()}/rest/{method}?{query}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as exc:  # includes 401 on bad credentials
        raise RuntimeError(f"{method}: HTTP {exc.code}") from exc
    payload = body.get("subsonic-response") or {}
    if payload.get("status") != "ok":
        err = payload.get("error") or {}
        raise RuntimeError(f"{method}: {err.get('code')} {err.get('message')}")
    return payload


def ping() -> dict:
    return call("ping")


def iter_albums(page: int = ALBUM_PAGE):
    """Every album, paged. Album first, because Subsonic has no 'all songs' call."""
    offset = 0
    while True:
        payload = call("getAlbumList2", type="alphabeticalByName", size=page, offset=offset)
        albums = (payload.get("albumList2") or {}).get("album") or []
        for album in albums:
            yield album
        if len(albums) < page:
            return
        offset += page


def album_songs(album_id: str) -> list[dict]:
    album = call("getAlbum", id=album_id).get("album") or {}
    return album.get("song") or []


def get_artist(artist_id: str) -> dict:
    """One artist, including the `musicBrainzId` Navidrome holds for it."""
    return call("getArtist", id=artist_id).get("artist") or {}


def song_genres(song: dict) -> list[str]:
    """OpenSubsonic gives a `genres` array; stock Subsonic gives one string."""
    out = []
    for item in song.get("genres") or []:
        name = item.get("name") if isinstance(item, dict) else item
        if name:
            out.append(str(name))
    if not out and song.get("genre"):
        out.append(str(song["genre"]))
    return out


def stream_url(song_id: str) -> str:
    """A URL Liquidsoap can play directly. Token auth, so no password in the URL."""
    query = urllib.parse.urlencode({**_auth(), "id": song_id})
    return f"{_base()}/rest/stream?{query}"


def song_id(url: str) -> str:
    """The id inside a stream URL this module produced, or "".

    Pool files, the queue file and Liquidsoap's own request metadata all hand back
    these URLs, and the id is the only part anyone needs from them.
    """
    query = urllib.parse.urlparse(url or "").query
    return urllib.parse.parse_qs(query).get("id", [""])[0]


def scan_status() -> dict:
    """Navidrome's own view of the library: cheap proof that the index is stale."""
    query = urllib.parse.urlencode({**_auth()})
    try:
        with urllib.request.urlopen(f"{_base()}/rest/getScanStatus?{query}", timeout=10) as r:
            data = json.loads(r.read())
    except Exception:
        return {}
    return data.get("subsonic-response", {}).get("scanStatus", {}) or {}


def cover_art(album_id: str, size: int = 300) -> bytes:
    """Album cover from Navidrome, so the browser never sees API credentials."""
    query = urllib.parse.urlencode({**_auth(), "id": album_id, "size": str(size)})
    try:
        with urllib.request.urlopen(f"{_base()}/rest/getCoverArt?{query}", timeout=15) as r:
            return r.read()
    except Exception:
        return b""


def download(song_id: str, target: Path) -> Path:
    """Fetch a track to disk, for anything that needs the bytes rather than a stream."""
    with urllib.request.urlopen(stream_url(song_id), timeout=120) as r:
        target.write_bytes(r.read())
    return target
