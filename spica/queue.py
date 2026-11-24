"""Per-station queue + history on disk.

The mixer reads queue/<id>.txt on a one-second timer, pushes what it finds into a
Liquidsoap request.queue and removes the file. That keeps the hand-off a plain
file write — no sockets, no RPC, nothing to restart.

One file is one slot: whoever writes first wins, and a second writer is refused
rather than overwriting a track the mixer has not taken yet.

A listener request is the exception, and it does not take the slot either: it goes
into its own ordered list and is then promoted into the mixer's queue, ahead of the
pick waiting there. That pick is dropped rather than moved — "play something
heavier" is about the mood the listener just changed, and the pick in hand was
chosen for the mood they changed from — and so is the rest of the DJ's set, which
was chosen for that same mood. The loop picks a new set within a tick, with the
request still remembered.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import config, plan, subsonic
from .atomic import atomic_write
from .ledger import (
    parse_stamp,
    utc_stamp,
    aired_within,
    last_shelf,
    offered_ids,
    picks_path,
    read_tail,
    recent_pick_artists,
    recent_pick_ids,
    recent_picks,
    recently_out,
    why_for,
)


def _write(station: config.Station, uri: str, label: str) -> None:
    """Fill the slot. The label is written first, so the queue file existing means
    its label exists too — the other order left a waiting track with no name after a
    crash, and the page reported nothing queued."""
    atomic_write(station.queue_file.with_suffix(".label"), (label or "").strip() + "\n")
    atomic_write(station.queue_file, uri + "\n")


def push(station: config.Station, uri: str, label: str = "") -> bool:
    """Hand the mixer the next track. False when one is already waiting.

    First writer wins, because the mixer takes the file on its next tick and
    whatever it takes is what airs: a second push must not overwrite an unconsumed
    one and silently drop a track. This is how the pick loop feeds the station — a
    background pick may wait, but it never preempts.
    """
    if station.queue_file.exists():
        return False
    _write(station, uri, label)
    return True


def due_file(station: config.Station) -> Path:
    return station.queue_file.with_suffix(".request")


def _request_lines(station: config.Station) -> list[dict]:
    try:
        raw = due_file(station).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in raw:
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _write_request_lines(station: config.Station, lines: list[dict]) -> None:
    path = due_file(station)
    if not lines:
        path.unlink(missing_ok=True)
        return
    atomic_write(path, "".join(json.dumps(line) + "\n" for line in lines))


def append_request(station: config.Station, uri: str, label: str = "") -> int:
    """Queue a listener request behind any already waiting. Returns its position."""
    lines = _request_lines(station) + [{"uri": uri, "label": (label or "").strip()}]
    _write_request_lines(station, lines)
    return len(lines)


def take_request(station: config.Station) -> dict | None:
    """Claim the oldest waiting request, for the slot, when the mixer is unreachable."""
    lines = _request_lines(station)
    if not lines:
        return None
    _write_request_lines(station, lines[1:])
    return lines[0]


def promote_requests(station: config.Station) -> int:
    """Put the waiting requests ahead of the DJ's pick, in the order they were asked.

    The station keeps one track waiting so a changeover never falls back to the
    pool. A request supersedes that track rather than queueing behind it: "play
    something heavier" is about the mood the listener has just changed, and the pick
    waiting there was chosen for the mood they changed from. It is dropped and the
    loop chooses again within a tick, and the request is still in the brief.

    Liquidsoap's queue can only push onto the end and remove by id, so this is a
    rewrite — take everything out, push the requests, in order, and nothing else.
    Returns how many requests are now waiting.
    """
    from . import mixer

    pending = mixer.pending_uris(station)
    if pending is None:  # no control server: the loop's slot hand-over still works
        return 0
    # Compare by track id: a request URI now carries a per-track gain annotation,
    # and Liquidsoap's initial_uri may or may not echo the prefix.
    pending_ids = [subsonic.song_id(uri) for uri in pending]
    # A request that has left the queue has aired; one that was never pushed is
    # still owed, and stays for the next attempt.
    lines = [
        l for l in _request_lines(station)
        if not l.get("sent") or subsonic.song_id(l["uri"]) in pending_ids
    ]
    if not lines:
        _write_request_lines(station, [])
        return 0
    if not any(not l.get("sent") for l in lines):
        # Nothing new since the last promotion. Leave the queue alone: it has a
        # fresh pick of its own behind the requests, and rewriting would drop it.
        _write_request_lines(station, lines)
        return 0
    want = [l["uri"] for l in lines]
    if pending_ids != [subsonic.song_id(uri) for uri in want] and not mixer.set_queue(station, want):
        _write_request_lines(station, lines)
        return 0
    # The DJ's set was chosen for the mood the listener has just changed, and the
    # queue no longer holds the track it was going to play next. Its remaining
    # tracks go with it; the loop asks for a new set with the request in the brief,
    # which is the point of the request.
    plan.clear(station)
    for line in lines:
        line["sent"] = True
    _write_request_lines(station, lines)
    return len(lines)


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
        "t": utc_stamp(),
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


# How many picks the log keeps. Small enough to read in the admin, long enough that
# last night is still there; the file is rewritten only when it outgrows this twice.
PICKS_KEPT = 500


def record_pick(station: config.Station, track_id: str, title: str, artist: str, why: str = "",
                presence: str = "", detail: dict | None = None) -> None:
    """What the DJ chose. Kept apart from history, which only records plays.

    The presence reading is logged with it: "why was the DJ picking at 3 a.m.?" is a
    question that comes back, and the answer has to be in the record. `detail` is
    what the picker itself saw — the brief, the tools it called, its reply — so the
    admin can answer "why *that* track?" without reproducing it.
    """
    station.history_file.parent.mkdir(parents=True, exist_ok=True)
    entry: dict[str, object] = {
        "t": utc_stamp(), "id": track_id,
        "title": title, "artist": artist, "why": why, "presence": presence,
    }
    if detail:
        if detail.get("brief"):
            # The brief carries the table and the timeline; 2,000 characters cut it
            # in the middle of the offer. Keep the whole thing the DJ read.
            entry["brief"] = str(detail["brief"])[:12000]
        if detail.get("tools"):
            entry["tools"] = list(detail["tools"])[:8]
        if detail.get("shelf"):
            entry["shelf"] = list(detail["shelf"])[:16]
        if detail.get("offered"):
            # Every id the table carried, played or not: the next few sets read this
            # to leave a record alone for a while even when nobody wanted it.
            entry["offered"] = [str(track_id) for track_id in detail["offered"]][:64]
        if detail.get("path"):
            entry["path"] = str(detail["path"])[:24]
        if detail.get("coverage") is not None:
            entry["coverage"] = detail["coverage"]
        if detail.get("reply"):
            entry["reply"] = str(detail["reply"])[:2000]
    path = picks_path(station)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    try:
        if path.stat().st_size > 8_000_000:
            keep = read_tail(path, PICKS_KEPT)
            atomic_write(path, "".join(line + "\n" for line in keep))
    except OSError:
        pass


# A restart re-announces the track that is already playing, usually seconds later.
# Two plays of the same track further apart than this are two plays: a station that
# resumes hours later picks up exactly where it stopped.
DUPE_ANNOUNCE_S = 120


def recent(station: config.Station, n: int = 20) -> list[dict]:
    """The last n plays, newest last, with a restart's re-announce collapsed.

    Only the tail of the file is read, and the window grows until n real plays come
    back — de-duplicating after the slice used to return fewer than asked for.
    """
    want = max(1, n)
    grab = want + 4
    while True:
        out: list[dict] = []
        lines = read_tail(station.history_file, grab)
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
