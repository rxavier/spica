"""Process-wide state shared by the API, the UI and the pick loop.

One module, so `python -m spica.app` (running as `__main__`) and the importable
`spica.app` do not end up with two copies of the same globals.
"""

from __future__ import annotations

import sqlite3

from . import config, navidrome


def db_path():
    return config.STATE_DIR / "index.db"


_conn: sqlite3.Connection | None = None
_stations: list[config.Station] | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = navidrome.connect(db_path())
    return _conn


def stations() -> list[config.Station]:
    """The station rack, read once and refreshed on demand."""
    global _stations
    if _stations is None:
        _stations = config.load_stations()
    return _stations


def reload_stations() -> list[config.Station]:
    global _stations
    _stations = config.load_stations()
    return _stations


_tracks: dict | None = None


def norm(text: str) -> str:
    """Compare titles the way Icecast mangles them: it turns "Jane’s" into
    "Jane*s", so anything outside letters and digits is noise."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


# Letters that are alphanumeric but not ASCII: Icecast turns punctuation into
# "*", and a title with a Polish ł then has nothing left to match on.
_FOLD = str.maketrans({
    "ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ð": "d",
    "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss", "þ": "th", "ı": "i",
})


def norm_loose(text: str) -> str:
    """norm() with the extended-Latin letters folded to ASCII."""
    return norm((text or "").translate(_FOLD))


def _wildcard_match(mangled: str, candidate: str) -> bool:
    """Icecast replaces a character it cannot carry with "*", so a title comes
    back with a hole in it: "Fikołek" arrives as "Fiko*ek". Same length, every
    position equal except where the mangled copy holds a non-letter."""
    a, b = (mangled or "").casefold(), (candidate or "").casefold()
    if not a or len(a) != len(b):
        return False
    return all(x == y or not x.isalnum() for x, y in zip(a, b))


def tracks() -> dict:
    """(artist, title) and (title) indexes over the library, built once."""
    global _tracks
    if _tracks is None:
        by_both, by_title, by_id = {}, {}, {}
        loose, loose_title, by_artist = {}, {}, {}
        for row in conn().execute("SELECT id, title, artist, album_id FROM tracks"):
            key = (norm(row["artist"]), norm(row["title"]))
            by_both.setdefault(key, row)
            by_title.setdefault(norm(row["title"]), row)
            loose.setdefault((norm_loose(row["artist"]), norm_loose(row["title"])), row)
            loose_title.setdefault(norm_loose(row["title"]), row)
            by_artist.setdefault(norm(row["artist"]), []).append(row)
            by_id[row["id"]] = row
        _tracks = {
            "both": by_both,
            "title": by_title,
            "id": by_id,
            "loose": loose,
            "loose_title": loose_title,
            "by_artist": by_artist,
        }
    return _tracks


def forget_tracks() -> None:
    global _tracks
    _tracks = None


def cover_for(album_id: str) -> str:
    """The id Subsonic wants for an album's art, falling back to the album id."""
    if not album_id:
        return album_id
    try:
        row = conn().execute(
            "SELECT cover_id FROM album_art WHERE album_id = ?", (album_id,)
        ).fetchone()
    except sqlite3.Error:
        return album_id
    return row["cover_id"] if row else album_id


def cover_tag(album_id: str) -> str:
    """Navidrome's cover id ends in a hash of the image, so it doubles as a version."""
    cover = cover_for(album_id)
    return cover.rsplit("_", 1)[-1][:10] if "_" in cover else "0"


def album_for(track_id: str) -> str:
    """Cover id for a played track, so the list can show artwork."""
    row = tracks()["id"].get(track_id or "")
    return row["album_id"] if row else ""


def find_track(artist: str, title: str):
    """The library row for a track named by Icecast, or None."""
    idx = tracks()
    if not title:
        return None
    hit = (
        idx["both"].get((norm(artist), norm(title)))
        or idx["title"].get(norm(title))
        or idx["loose"].get((norm_loose(artist), norm_loose(title)))
        or idx["loose_title"].get(norm_loose(title))
    )
    if hit:
        return hit
    # Last resort: the same artist, and a title that matches once the holes
    # Icecast punched are allowed to stand for anything.
    for row in idx["by_artist"].get(norm(artist), ()):
        if _wildcard_match(title, row["title"]):
            return row
    return None


_lib = None


def library():
    """The query layer over the index, shared so every caller sees one handle."""
    global _lib
    if _lib is None:
        from . import picker

        _lib = picker.Library(conn())
    return _lib


def station(station_id: str) -> config.Station | None:
    for st in stations():
        if st.id == station_id:
            return st
    return None
