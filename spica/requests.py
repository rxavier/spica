"""Listener requests that outlive a single pick.

A request is not a one-shot: "play something milder" is an instruction about the
next few songs, and the DJ needs to know both WHAT was asked and HOW LONG AGO,
because a request from eight songs back should carry less weight than one from
two songs ago. So a request is stored with the number of plays that had happened
when it was made, and it expires after N plays rather than after a clock time —
pacing a station by songs is how a listener actually experiences it.

The DJ is told to accommodate these, including requests that ask for a change of
vibe, without ever changing the station's own identity.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import config

DEFAULT_SONGS = 3
MAX_SONGS = 30


def _path(station: config.Station) -> Path:
    return station.history_file.parent / "requests" / f"{station.id}.jsonl"


def _plays(station: config.Station) -> int:
    """How many tracks this station has played — the clock these requests run on."""
    try:
        with station.history_file.open(encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except FileNotFoundError:
        return 0


def _load(station: config.Station) -> list[dict]:
    try:
        lines = _path(station).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _store(station: config.Station, reqs: list[dict]) -> None:
    path = _path(station)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in reqs), encoding="utf-8"
    )
    tmp.replace(path)


def record(station: config.Station, text: str, songs: int = DEFAULT_SONGS) -> dict:
    """Register a request. `songs` is how many tracks it stays in force for."""
    text = (text or "").strip()
    songs = max(1, min(MAX_SONGS, int(songs or DEFAULT_SONGS)))
    req = {
        "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "text": text,
        "songs": songs,
        "at_play": _plays(station),
    }
    # Asking for the same thing again replaces it rather than stacking duplicates.
    key = text.casefold()
    reqs = [
        r
        for r in _load(station)
        if _remaining(r, _plays(station)) > 0 and str(r.get("text", "")).casefold() != key
    ]
    reqs.append(req)
    _store(station, reqs)
    return req


def attach_track(station: config.Station, req: dict, track) -> None:
    """Point the request at the track it produced, for the list to show."""
    reqs = _load(station)
    for entry in reqs:
        if entry.get("t") == req.get("t") and entry.get("text") == req.get("text"):
            entry["track"] = {"id": track.id, "title": track.title, "artist": track.artist}
            break
    else:
        reqs.append({**req, "track": {"id": track.id, "title": track.title, "artist": track.artist}})
    _store(station, reqs)


def _remaining(req: dict, plays_now: int) -> int:
    return int(req.get("songs", DEFAULT_SONGS)) - (plays_now - int(req.get("at_play", 0)))


def active(station: config.Station) -> list[dict]:
    """Requests still in force, newest last, with age and remaining plays."""
    plays_now = _plays(station)
    out = []
    for req in _load(station):
        remaining = _remaining(req, plays_now)
        if remaining <= 0:
            continue
        out.append(
            {
                "text": req.get("text", ""),
                "songs_ago": plays_now - int(req.get("at_play", 0)),
                "remaining": remaining,
                "track": req.get("track") or {},
            }
        )
    return out


def clear(station: config.Station) -> None:
    try:
        _path(station).unlink()
    except FileNotFoundError:
        pass
