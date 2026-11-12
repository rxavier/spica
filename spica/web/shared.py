"""Templates, the station page's data, and the small helpers every route shares."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from collections import Counter
from pathlib import Path

from .. import config
from .. import icecast
from .. import mixer
from .. import navidrome
from .. import plays
from .. import queue
from .. import store
from .. import requests as requests_mod
from fastapi import HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

PACKAGE = Path(__file__).resolve().parent.parent


static = StaticFiles(directory=PACKAGE / "static")


COVER_SIZE = 600  # the hero shows it up to 320px on a phone; 600 survives retina


THUMB_SIZE = 96  # the ledger and the request slips render at 38px; 96 survives retina


ALBUM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


templates = Jinja2Templates(directory=str(PACKAGE / "templates"))
# The sizes the templates ask the art route for: the hero wants the big one, a
# 38-pixel ledger stamp does not. Two `templates.env.globals[...]` lines used to sit
# in web.py beside this constructor and the split dropped them without a word, so
# every <img> asked for `size=` and FastAPI answered 422 — no artwork anywhere.
templates.env.globals["hero"] = COVER_SIZE
templates.env.globals["thumb"] = THUMB_SIZE


ART_DIR = config.STATE_DIR / "art"


def stations() -> list[config.Station]:
    return store.stations()


def station_or_404(station_id: str) -> config.Station:
    for st in stations():
        if st.id == station_id:
            return st
    raise HTTPException(404, f"unknown station {station_id}")


def now_playing(st) -> dict:
    live = icecast.live_mount(icecast.listen_states(), st)
    state = live[1] if live else None
    raw = ((state.title if state else "") or "").strip()
    if not raw:
        # No ICY to read: Ogg streams carry their metadata inside the stream, so a
        # FLAC-only station leaves the mount blank. The mixer stamps a file the
        # instant a track starts, which is the freshest answer for every codec —
        # the ledger below is the picker's view and lands on its own 20-second tick.
        raw = mixer.now_tail(st)
    # A station's title should be "Artist - Title". A URI here means the mixer
    # announced the request rather than the track, and printing it would put a wall
    # of credentials under the cover.
    if "://" in raw:
        raw = ""
    # Log it here too: the page polls every five seconds and must not show the
    # previous track's line under the new one while the background loop catches up.
    plays.note(st, raw)
    artist, song = queue.split_title(raw)

    # Icecast rewrites punctuation ("Jane's" becomes "Jane*s"), so prefer the
    # library's own spelling when the track is known.
    found = store.find_track(artist, song)
    if found:
        artist, song = found["artist"], found["title"]
        album_id = found["album_id"]
    else:
        album_id = ""

    # history records plays, so its last entry is the track on air. It is also the
    # only witness when the mount is closed: the station keeps playing with nobody
    # listening, and Icecast has no title to give then.
    played = queue.recent(st, 1)
    last = played[-1] if played else {}

    if not raw and last:
        artist = last.get("artist", "")
        song = last.get("title", "")
        album_id = store.album_for(last.get("id", ""))
        entry = last
    else:
        # Only claim a reason when it belongs to this track.
        entry = last if last and store.norm(
            f"{last.get('artist', '')} {last.get('title', '')}"
        ) == store.norm(f"{artist} {song}") else {}

    track_id = entry.get("id", "")
    if not album_id and track_id:
        album_id = store.album_for(track_id)
    if not album_id and song:
        found = store.find_track(artist, song)
        album_id = found["album_id"] if found else ""

    album = ""
    if album_id:
        try:
            row = store.conn().execute(
                "SELECT album FROM tracks WHERE album_id = ? LIMIT 1", (album_id,)
            ).fetchone()
            album = (row["album"] if row else "") or ""
        except Exception:
            album = ""

    return {
        "listeners": state.listeners if state else 0,
        "title": raw,
        "artist": artist,
        "song": song,
        "album": album,
        "queued": queue.queued(st),
        "why": entry.get("why", ""),
        "track_id": track_id,
        "album_id": album_id,
        "cover_tag": store.cover_tag(album_id),
    }


SOURCE_LABELS = {
    "dj": "DJ pick",
    "llm": "DJ pick",          # entries written before the rename
    "standby": "Standby pick",
    "pool": "Standby pick",
    "request": "Request",
    "manual": "Manual",
}


def _local_tz():
    """The clock the ledger speaks in when the browser cannot answer."""
    from zoneinfo import ZoneInfo

    return ZoneInfo(os.environ.get("SPICA_TZ", "America/Montevideo"))


LOCAL_TZ = _local_tz()


def history_of(st, limit: int = 25, now: dict | None = None) -> list[dict]:
    """Most recent first, with the cover and a flag on the track that is playing."""
    live = store.norm(f"{now.get('artist', '')} {now.get('song', '')}") if now else ""
    rows = list(reversed(queue.recent(st, limit)))
    for row in rows:
        # the ledger prints a time in the margin, and the browser corrects it to
        # the listener's own clock; the server value is the fallback, in the
        # station's timezone rather than UTC.
        stamp = row.get("t") or ""
        row["stamp"] = stamp
        try:
            moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            row["clock"] = moment.astimezone(LOCAL_TZ).strftime("%H:%M")
        except (TypeError, ValueError):
            row["clock"] = ""
        album_id = store.album_for(row.get("id", ""))
        # Plays that come from the mixer's own rotation carry no id, so look the
        # track up by name before giving up on the artwork.
        if not album_id:
            found = store.find_track(row.get("artist", ""), row.get("title", ""))
            if found:
                album_id = found["album_id"]
                row["artist"] = found["artist"]
                row["title"] = found["title"]
        row["album_id"] = album_id
        row["cover_tag"] = store.cover_tag(row["album_id"])
        row["current"] = bool(live) and store.norm(f"{row.get('artist', '')} {row.get('title', '')}") == live
        row["source"] = SOURCE_LABELS.get(row.get("source", ""), row.get("source", ""))
    return rows


def request_rows(st, limit: int = 5) -> list[dict]:
    rows = list(reversed(requests_mod.active(st)))[:limit]
    for row in rows:
        track = row.get("track") or {}
        row["album_id"] = store.album_for(track.get("id", ""))
        row["cover_tag"] = store.cover_tag(row["album_id"])
    return rows


def pool_preview(st, top: int = 15) -> dict:
    """What the station's rules actually select, so the admin can see the result."""
    conn = _conn()
    rows = navidrome.rows_for_station(conn, st)
    artists = Counter((r["artist"] or "?") for r in rows)
    albums = Counter((r["artist"] or "?") + " — " + (r["album"] or "?") for r in rows)
    return {
        "tracks": len(rows),
        "artists": len(artists),
        "albums": len(albums),
        "top_artists": artists.most_common(top),
        "top_albums": albums.most_common(top),
        "years": (min((r["year"] for r in rows if r["year"]), default=0),
                  max((r["year"] for r in rows if r["year"]), default=0)),
    }


def station_context(st) -> dict:
    """Everything the station body needs, in one place for page and swap."""
    now = now_playing(st)
    return {
        "station": st,
        "stations": stations(),
        "now": now,
        "labels": config.CODEC_LABEL,
        "history": history_of(st, 15, now),
        "requests": request_rows(st),
        "pool": pool_size(st),
    }


def pool_size(st) -> int:
    try:
        return sum(1 for line in st.pool_file.read_text(encoding="utf-8").splitlines() if line.strip())
    except FileNotFoundError:
        return 0


def write_stations(items: list[dict]) -> None:
    path = config.CONFIG_DIR / "stations.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"stations": items}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _raw_stations() -> list[dict]:
    path = config.CONFIG_DIR / "stations.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("stations", [])
    except (OSError, ValueError):
        return []


def _slug(text: str) -> str:
    """A station id: lowercase letters, digits and single dashes."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return cleaned[:32]


def _move_state(old: str, new: str) -> list[str]:
    """Carry a station's own files when its id changes: pool, history, queue."""
    moved = []
    for folder, suffix in (
        ("pools", ".m3u"),
        ("history", ".jsonl"),
        ("history", ".picks.jsonl"),
        ("queue", ".txt"),
    ):
        src = config.STATE_DIR / folder / f"{old}{suffix}"
        dst = config.STATE_DIR / folder / f"{new}{suffix}"
        if src.exists() and not dst.exists():
            src.rename(dst)
            moved.append(f"{folder}/{new}{suffix}")
    reqs = config.STATE_DIR / "history" / "requests" / f"{old}.jsonl"
    if reqs.exists():
        reqs.rename(config.STATE_DIR / "history" / "requests" / f"{new}.jsonl")
        moved.append(f"history/requests/{new}.jsonl")
    return moved


def _apply_station_script() -> str:
    """Regenerate spica.liq and ask the mixer to come back with it."""
    import os

    from .. import liqgen

    password = os.environ.get("ICECAST_SOURCE_PASSWORD", "")
    (config.CONFIG_DIR / "spica.liq").write_text(
        liqgen.render(store.reload_stations(), password), encoding="utf-8"
    )
    return _restart_mixer()


def _int_or_none(value: str) -> int | None:
    value = (value or "").strip()
    return int(value) if value.lstrip("-").isdigit() else None


def _restart_mixer() -> str:
    """Ask Liquidsoap to close; docker restarts it with the regenerated script."""
    try:
        mixer.close()
        return "The mixer was told to restart; every station returns in a few seconds."
    except Exception as exc:
        return f"mixer restart failed: {exc}"


def _conn():
    return store.conn()


def known_labels(names: list[str]) -> list[str]:
    """Only labels the library actually has; a typo would otherwise select nothing."""
    conn = _conn()
    known = {row["label"] for row in conn.execute("SELECT DISTINCT label FROM track_genres")}
    seen, out = set(), []
    for name in names:
        name = str(name or "").strip()
        if name in known and name not in seen:
            seen.add(name)
            out.append(name)
    return out
