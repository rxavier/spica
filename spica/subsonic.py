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
import os
import re
import secrets
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config
from .atomic import atomic_write

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
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_hex(8)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Somebody else won the creation; use theirs.
        _SALT = path.read_text(encoding="utf-8").strip()
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(salt + "\n")
        _SALT = salt
    if not _SALT:
        # The file exists but is empty: give it one and keep the mode tight.
        _SALT = salt
        atomic_write(path, salt + "\n", mode=0o600)
    return _SALT


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


_RETRYABLE = {429, 500, 502, 503, 504}


def _open(url: str, *, timeout: float, attempts: int = 1):
    """Open a Navidrome URL, retrying only the statuses that mean "later"."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return urllib.request.urlopen(url, timeout=timeout)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in _RETRYABLE:
                raise
        except Exception as exc:  # timeout, DNS reset
            last = exc
        if attempt + 1 < attempts:
            time.sleep(2 + attempt * 4)
    raise last if last is not None else RuntimeError("no Navidrome response")


def call(method: str, **params) -> dict:
    """One API call, returning the `subsonic-response` body. Raises on failure."""
    query = urllib.parse.urlencode({**_auth(), **params})
    url = f"{_base()}/rest/{method}?{query}"
    try:
        with _open(url, timeout=30, attempts=3) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as exc:  # includes 401 on bad credentials
        raise RuntimeError(f"{method}: HTTP {exc.code}") from exc
    payload = body.get("subsonic-response") or {}
    if payload.get("status") != "ok":
        err = payload.get("error") or {}
        raise RuntimeError(f"{method}: {err.get('code')} {err.get('message')}")
    return payload


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
    these URLs, and the id is the only part anyone needs from them. A request can
    arrive wrapped in Liquidsoap's ``annotate:key="value":`` prefix (the per-track
    gain lives there), so the target URL is found before it is parsed.
    """
    url = url or ""
    if url.startswith("annotate:"):
        target = re.search(r"https?://", url)
        if target:
            url = url[target.start():]
    query = urllib.parse.urlparse(url).query
    return urllib.parse.parse_qs(query).get("id", [""])[0]


class CoverUnavailable(RuntimeError):
    """Navidrome could not answer for this cover. Says nothing about the artwork."""


def scan_status() -> dict:
    """Navidrome's own view of the library: cheap proof that the index is stale.

    Raises when Navidrome cannot answer. An outage is not the same fact as "no scan
    running": treating them alike told the worker the index was current for as long
    as the outage lasted, and it never rebuilt once the library came back.
    """
    return call("getScanStatus").get("scanStatus", {}) or {}


def cover_art(cover_id: str, size: int = 300) -> bytes:
    """Album cover bytes, or b"" when the album genuinely has none.

    A failed request is not a cover-less album, and the caller caches "no cover" on
    disk for a day — so a timeout raises instead of returning nothing.
    """
    if not cover_id:
        return b""
    query = urllib.parse.urlencode({**_auth(), "id": cover_id, "size": str(size)})
    try:
        with _open(f"{_base()}/rest/getCoverArt?{query}", timeout=15, attempts=2) as r:
            return r.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return b""
        raise CoverUnavailable(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CoverUnavailable(str(exc)) from exc


def download(song_id: str, target: Path) -> Path:
    """Fetch a track to disk, for anything that needs the bytes rather than a stream."""
    with _open(stream_url(song_id), timeout=120, attempts=2) as r, target.open("wb") as fh:
        shutil.copyfileobj(r, fh)
    return target


def download_transcoded(song_id: str, target: Path, bitrate: int = 96,
                        fmt: str = "mp3") -> Path:
    """Fetch a track for analysis rather than for listening.

    Analysis works at 16 kHz mono, so asking Navidrome for a small MP3 costs a
    fraction of the bytes of the original FLAC and changes nothing the models see.
    """
    query = urllib.parse.urlencode({
        **_auth(), "id": song_id, "format": fmt, "maxBitRate": str(int(bitrate)),
    })
    with _open(f"{_base()}/rest/stream?{query}", timeout=120, attempts=2) as r, target.open("wb") as fh:
        shutil.copyfileobj(r, fh)
    return target
