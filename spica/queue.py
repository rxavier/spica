"""Per-station queue + history on disk.

The mixer reads queue/<id>.txt on a one-second timer, pushes what it finds into a
Liquidsoap request.queue and removes the file. That keeps the hand-off a plain
file write — no sockets, no RPC, nothing to restart.

One file is one slot: whoever writes first wins, and a second writer is refused
rather than overwriting a track the mixer has not taken yet.
"""

from __future__ import annotations

import datetime
import json
import time
from pathlib import Path

from . import config, subsonic


def _write(station: config.Station, uri: str, label: str) -> None:
    """Fill the slot. The label is written first, so the queue file existing means
    its label exists too — the other order left a waiting track with no name after a
    crash, and the page reported nothing queued."""
    station.queue_file.parent.mkdir(parents=True, exist_ok=True)
    station.queue_file.with_suffix(".label").write_text(
        (label or "").strip() + "\n", encoding="utf-8"
    )
    tmp = station.queue_file.with_suffix(".tmp")
    tmp.write_text(uri + "\n", encoding="utf-8")
    tmp.replace(station.queue_file)


def push(station: config.Station, uri: str, label: str = "") -> bool:
    """Hand the mixer the next track. False when one is already waiting.

    First writer wins, because the mixer takes the file on its next tick and
    whatever it takes is what airs: a second push must not overwrite an unconsumed
    one, which used to drop a track with no trace anywhere. This is how the pick
    loop feeds the station — a background pick may wait, but it never preempts.
    """
    if station.queue_file.exists():
        return False
    _write(station, uri, label)
    return True


def replace(station: config.Station, uri: str, label: str = "") -> str:
    """Take the slot whether or not it is free. Returns the track id it displaced.

    For the two things a person asked for — a listener request and an operator's
    forced pick. They are instructions, not suggestions, so they go next; the caller
    puts the displaced id back at the head of the pool, since a track that never
    aired should not be a full rotation away from playing.
    """
    displaced = ""
    if station.queue_file.exists():
        try:
            displaced = subsonic.song_id(station.queue_file.read_text(encoding="utf-8"))
        except OSError:
            displaced = ""
    _write(station, uri, label)
    return displaced


def queued(station: config.Station) -> str:
    """The track waiting for the mixer, by name — empty once it has been taken.

    The file disappearing is what says the mixer has it; until then the label beside
    it is the only thing worth putting in front of a listener.
    """
    if not station.queue_file.exists():
        return ""
    try:
        return station.queue_file.with_suffix(".label").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def record(station: config.Station, track_id: str, title: str, artist: str, source: str, why: str = "") -> None:
    station.history_file.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "id": track_id,
        "title": title,
        "artist": artist,
        "source": source,
        "why": why,
    }
    with station.history_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def split_title(raw: str) -> tuple[str, str]:
    """Icecast reports one string, "Artist - Song"; the pages want them apart."""
    raw = (raw or "").strip()
    if " - " in raw:
        artist, song = raw.split(" - ", 1)
        return artist.strip(), song.strip()
    return "", raw


def _picks_path(station: config.Station) -> Path:
    return station.history_file.with_suffix(".picks.jsonl")


def record_pick(station: config.Station, track_id: str, title: str, artist: str, why: str = "",
                presence: str = "") -> None:
    """What the DJ chose. Kept apart from history, which only records plays.

    The presence reading is logged with it: "why was the DJ picking at 3 a.m.?" is a
    question that comes back, and the answer has to be in the record.
    """
    station.history_file.parent.mkdir(parents=True, exist_ok=True)
    entry = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "id": track_id,
             "title": title, "artist": artist, "why": why, "presence": presence}
    with _picks_path(station).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _tail_lines(path: Path, count: int, block: int = 65536) -> list[str]:
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


def recent_picks(station: config.Station, n: int = 12) -> list[dict]:
    out = []
    for line in _tail_lines(_picks_path(station), n):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def why_for(station: config.Station, title: str) -> str:
    """The reason the DJ gave for a title, if it picked it recently."""
    from .store import norm

    key = norm(title)
    for entry in reversed(recent_picks(station, 25)):
        if norm(entry.get("title")) == key:
            return entry.get("why", "")
    return ""


# A restart re-announces the track that is already playing, usually seconds later.
# Two plays of the same track further apart than this are two plays: a station that
# resumes hours later picks up exactly where it stopped.
DUPE_ANNOUNCE_S = 120


def _when(entry: dict) -> float | None:
    raw = str(entry.get("t") or "").strip()
    if not raw:
        return None
    try:
        stamp = datetime.datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return stamp.replace(tzinfo=datetime.timezone.utc).timestamp()


def recent(station: config.Station, n: int = 20) -> list[dict]:
    """The last n plays, newest last, with a restart's re-announce collapsed.

    Only the tail of the file is read, and the window grows until n real plays come
    back — de-duplicating after the slice used to return fewer than asked for.
    """
    want = max(1, n)
    grab = want + 4
    while True:
        out: list[dict] = []
        lines = _tail_lines(station.history_file, grab)
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if out and (out[-1].get("artist"), out[-1].get("title")) == (
                entry.get("artist"), entry.get("title")
            ):
                first, second = _when(out[-1]), _when(entry)
                if first is None or second is None or second - first <= DUPE_ANNOUNCE_S:
                    continue  # the same announce twice, not a second play
            out.append(entry)
        if len(out) >= want or len(lines) < grab:
            return out[-want:]
        grab *= 2


def recent_ids(station: config.Station, n: int = 40) -> set[str]:
    return {e["id"] for e in recent(station, n) if e.get("id")}
