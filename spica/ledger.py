"""The append-only ledgers: what played, what the picker picked, which candidates it saw.

All three are JSONL files under the station's history directory. They only grow, so
every reader here walks back from the end instead of parsing the whole file, and
the repeat-memory policy lives here rather than being spread across `queue`,
`plays` and the picker.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from . import config

# A record may not come round again for this many picks, nor inside this many
# hours of air. The play window is counted in airings, like the candidates'; the air
# window is in hours because that is the unit a listener hears in.
PLAY_MEMORY = 40
PICK_MEMORY = 60
AIR_HOURS = 8

# Entries read per hour of history. A station airs about thirteen tracks an hour;
# the headroom covers a shorter one.
AIRINGS_PER_HOUR = 40


STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_stamp(when: float | None = None) -> str:
    """A ledger timestamp: UTC, second precision, one spelling everywhere."""
    return time.strftime(STAMP_FORMAT, time.gmtime(when))


def parse_stamp(raw) -> float | None:
    """A ledger timestamp to epoch seconds, or None when it is unusable."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def picks_path(station: config.Station) -> Path:
    return station.history_file.with_suffix(".picks.jsonl")


def read_tail(path: Path, count: int, block: int = 65536) -> list[str]:
    """The last `count` non-empty lines of a file, without reading the rest.

    These files are append-only and never trimmed, so "the last fifteen plays" used
    to mean parsing every line the station had ever aired — twice a minute, forever.
    Reading backwards from the end costs the same at two hundred lines or two
    million. A window that starts mid-line drops that line rather than decoding
    half a character.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            pos = fh.tell()
            buf = b""
            while pos > 0 and buf.count(b"\n") <= count:
                step = min(block, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
    except FileNotFoundError:
        return []
    lines = buf.split(b"\n")
    if pos > 0 and lines:
        lines = lines[1:]  # the first one was cut by the window
    if lines and not buf.endswith(b"\n"):
        lines = lines[:-1]  # a record still being written
    out = [line.decode("utf-8", "replace") for line in lines if line.strip()]
    return out[-count:] if count else out


def read_records(path: Path, count: int) -> list[dict]:
    """The last `count` JSON records, skipping a half-written or corrupt line."""
    out: list[dict] = []
    for line in read_tail(path, count):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def recent_picks(station: config.Station, n: int = 12) -> list[dict]:
    """The last n picks, oldest first."""
    return read_records(picks_path(station), n)


def recent_pick_ids(station: config.Station, n: int = PICK_MEMORY) -> set[str]:
    """The ids the picker chose in its last n picks."""
    return {str(entry["id"]) for entry in recent_picks(station, n) if entry.get("id")}


def recent_pick_artists(station: config.Station, n: int = 12) -> list[str]:
    """The artists behind the last n picks, newest first."""
    out: list[str] = []
    for entry in reversed(recent_picks(station, n)):
        name = str(entry.get("artist") or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def last_candidate_artists(station: config.Station, look: int = 6) -> list[str]:
    """The candidate artists for the last set that was planned, if the log has them.

    Rotation memory: an artist that was a candidate a set ago has had its turn at
    being offered, whether or not the picker played it. Re-offering the same twelve names
    every set would make the candidate list a fixed menu again, only smaller.
    """
    for entry in reversed(recent_picks(station, look)):
        names = entry.get("candidate_artists")
        if isinstance(names, list) and names:
            return [str(name) for name in names]
    return []


def aired_within(station: config.Station, seconds: float) -> set[str]:
    """Ids that aired inside the last `seconds`, from the play ledger.

    `recent()` counts *airings*, and about half of them are the fallback rotation: forty
    airings is under three hours, so a picker that came back after a quiet night had
    forgotten its own set and could open with it again. This is the same ledger asked
    in hours instead, which is the unit a listener hears in.
    """
    cutoff = time.time() - seconds
    limit = int(seconds / 3600 * AIRINGS_PER_HOUR) + 40
    ids: set[str] = set()
    for entry in reversed(read_records(station.history_file, limit)):
        stamp = parse_stamp(entry.get("t"))
        if stamp is None:
            continue
        if stamp < cutoff:
            break
        if entry.get("id"):
            ids.add(str(entry["id"]))
    return ids


def candidate_ids(station: config.Station, sets: int = 4) -> set[str]:
    """Ids the picker was offered in its last `sets` sets, played or not.

    A record only leaves the candidates by *playing*, so the familiar block kept handing
    over the same records it kept refusing — one was on better than a candidate set in five
    and never picked. Being offered is having had a turn too.
    """
    out: set[str] = set()
    found = 0
    for entry in reversed(recent_picks(station, max(1, sets) * 8)):
        ids = entry.get("candidate_ids")
        if not isinstance(ids, list) or not ids:
            continue
        out.update(str(track_id) for track_id in ids if track_id)
        found += 1
        if found >= sets:
            break
    return out


def recent_ids(station: config.Station, n: int = PLAY_MEMORY) -> set[str]:
    """The ids of the last n plays, from the play ledger."""
    from . import queue

    return {str(entry["id"]) for entry in queue.recent(station, n) if entry.get("id")}


def recently_out(station: config.Station) -> set[str]:
    """Ids that should not play again yet: what aired, and what the picker chose.

    The windows are the constants above — every caller wants the same memory, so
    there is no per-call tuning to get wrong.
    """
    from . import queue

    return (recent_ids(station)
            | recent_pick_ids(station, PICK_MEMORY)
            | aired_within(station, AIR_HOURS * 3600))


def pick_for(station: config.Station, track_id: str = "", title: str = "",
             within: float = 30 * 60) -> dict:
    """The most recent pick for this track or title, if it was chosen recently."""
    from .store import norm

    key = norm(title)
    cutoff = time.time() - within
    for entry in reversed(recent_picks(station, 40)):
        at = parse_stamp(entry.get("t"))
        if at is None:
            continue
        if at < cutoff:
            break
        if (track_id and str(entry.get("id") or "") == track_id) or (
            key and norm(entry.get("title")) == key
        ):
            return entry
    return {}


def reason_for(station: config.Station, title: str) -> str:
    """The reason the picker gave for a title, if it picked it recently."""
    return str(pick_for(station, title=title).get("reason") or "")
