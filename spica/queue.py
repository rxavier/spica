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

import calendar
import datetime
import json
import time
from pathlib import Path

from . import config, plan, subsonic


def _tmp(path: Path) -> Path:
    """A temp name unique to the full file name, so the slot (<id>.txt) and the
    request list (<id>.request) cannot share one temp file."""
    return path.with_name(path.name + ".tmp")


def _write(station: config.Station, uri: str, label: str) -> None:
    """Fill the slot. The label is written first, so the queue file existing means
    its label exists too — the other order left a waiting track with no name after a
    crash, and the page reported nothing queued."""
    station.queue_file.parent.mkdir(parents=True, exist_ok=True)
    station.queue_file.with_suffix(".label").write_text(
        (label or "").strip() + "\n", encoding="utf-8"
    )
    tmp = _tmp(station.queue_file)
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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp(path)
    tmp.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    tmp.replace(path)


def waiting_requests(station: config.Station) -> int:
    """How many listener requests are waiting to air, in the order asked."""
    return len(_request_lines(station))


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
    plan.clear(station, "listener request")
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


def last_shelf(station: config.Station, look: int = 6) -> list[str]:
    """The artists offered for the last set that was planned, if the log has them.

    Rotation memory: an artist that was on the shelf a set ago has had its turn at
    being offered, whether or not the DJ played it. Re-offering the same twelve names
    every set would make the shelf a fixed menu again, only smaller.
    """
    for line in reversed(_tail_lines(_picks_path(station), look)):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        names = entry.get("shelf")
        if isinstance(names, list) and names:
            return [str(name) for name in names]
    return []


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
        "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "id": track_id,
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
    path = _picks_path(station)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    try:
        if path.stat().st_size > 8_000_000:
            keep = _tail_lines(path, PICKS_KEPT)
            path.write_text("".join(line + "\n" for line in keep), encoding="utf-8")
    except OSError:
        pass


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


# The play ledger and the pick log are two different memories, and repeats need both.
# A track the DJ chose stays in the pick log even if it never aired, and the standby
# pool's airings are only in the play ledger — so a window of one is never the window
# of the other.
def recent_pick_ids(station: config.Station, n: int = 60) -> set[str]:
    """The ids the DJ chose in its last n picks."""
    return {str(entry["id"]) for entry in recent_picks(station, n) if entry.get("id")}


def recent_pick_artists(station: config.Station, n: int = 12) -> list[str]:
    """The artists behind the last n picks, newest first."""
    out: list[str] = []
    for entry in reversed(recent_picks(station, n)):
        name = str(entry.get("artist") or "").strip()
        if name and name not in out:
            out.append(name)
    return out


# Entries read per hour of history. A station airs about thirteen tracks an hour; the
# headroom covers a shorter one.
_AIRINGS_PER_HOUR = 40


def aired_within(station: config.Station, seconds: float) -> set[str]:
    """Ids that aired inside the last `seconds`, from the play ledger.

    `recent()` counts *airings*, and about half of them are the standby pool: forty
    airings is under three hours, so a DJ that came back after a quiet night had
    forgotten its own set and could open with it again. This is the same ledger asked
    in hours instead, which is the unit a listener hears in.
    """
    cutoff = time.time() - seconds
    limit = int(seconds / 3600 * _AIRINGS_PER_HOUR) + 40
    ids: set[str] = set()
    for line in reversed(_tail_lines(station.history_file, limit)):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            stamp = calendar.timegm(time.strptime(str(entry.get("t")), "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            continue
        if stamp < cutoff:
            break
        if entry.get("id"):
            ids.add(str(entry["id"]))
    return ids


def offered_ids(station: config.Station, sets: int = 4) -> set[str]:
    """Ids the DJ was offered in its last `sets` sets, played or not.

    A record only leaves the table by *playing*, so the familiar block kept handing
    over the same records it kept refusing — one was on better than a table in five
    and never picked. Being offered is having had a turn too.
    """
    out: set[str] = set()
    found = 0
    for line in reversed(_tail_lines(_picks_path(station), max(1, sets) * 8)):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ids = entry.get("offered")
        if not isinstance(ids, list) or not ids:
            continue
        out.update(str(track_id) for track_id in ids if track_id)
        found += 1
        if found >= sets:
            break
    return out


# Repeat memory, in one place because two callers need the same answer: the table draws
# its offer from it, and the DJ's tool rows report it. A record may not come round again
# for this many picks, nor inside this many hours of air. Forty airings was the old
# window and it failed twice over: under three hours, and half of it the standby pool, so
# a station that sat idle since the morning had forgotten the set it played then.
PICK_MEMORY = 60
AIR_HOURS = 8


def recently_out(station: config.Station, picks: int = PICK_MEMORY,
                 hours: float = AIR_HOURS) -> set[str]:
    """Ids that should not play again yet: what aired, and what the DJ chose."""
    return (recent_ids(station, 40)
            | recent_pick_ids(station, picks)
            | aired_within(station, hours * 3600))


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
