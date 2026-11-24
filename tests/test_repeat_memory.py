"""The two memories repeats are judged against: what the DJ chose, and when it aired.

The play ledger and the pick log disagree exactly when the station is quiet, which is
when the old window failed: forty airings of the standby pool pushed a whole session out
of memory. These tests write both files and read them back through the helpers.
"""

from __future__ import annotations

import json
import time

from spica import queue


def write(station, name: str, rows: list[dict]) -> None:
    path = station.history_file.with_suffix(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def stamp(seconds_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def test_aired_within_counts_hours_not_airings(station):
    write(station, ".jsonl", [
        {"t": stamp(9 * 3600), "id": "old" * 8, "artist": "A", "title": "one", "source": "standby"},
        {"t": stamp(5 * 3600), "id": "middle" * 4, "artist": "B", "title": "two", "source": "standby"},
        {"t": stamp(600), "id": "fresh" * 5, "artist": "C", "title": "three", "source": "dj"},
    ])
    assert queue.aired_within(station, 8 * 3600) == {"middle" * 4, "fresh" * 5}
    assert queue.aired_within(station, 3600) == {"fresh" * 5}


def test_aired_within_walks_back_only_as_far_as_the_window(station):
    """Eight hours of a station that airs thirteen an hour is about a hundred lines."""
    rows = [{"t": stamp(hours * 3600 + 60), "id": f"{hours:03d}" + "x" * 19,
             "artist": "A", "title": f"{hours}", "source": "standby"}
            for hours in reversed(range(12))]  # the ledger is append-only: oldest first
    write(station, ".jsonl", rows)
    ids = queue.aired_within(station, 8 * 3600)
    assert ids == {f"{hours:03d}" + "x" * 19 for hours in range(8)}


def test_recent_picks_are_not_airings(station):
    write(station, ".picks.jsonl", [
        {"t": stamp(9000), "id": "a" * 22, "artist": "One", "title": "x", "why": "w"},
        {"t": stamp(6000), "id": "b" * 22, "artist": "Two", "title": "y", "why": "w"},
        {"t": stamp(60), "id": "c" * 22, "artist": "One", "title": "z", "why": "w"},
    ])
    assert queue.recent_pick_ids(station, 2) == {"b" * 22, "c" * 22}
    assert queue.recent_pick_artists(station, 3) == ["One", "Two"]


def test_the_offer_is_remembered_with_the_pick(station):
    """A record offered and refused still had its turn, so the pick carries the offer."""
    now = {"t": stamp(60), "id": "a" * 22, "title": "x", "artist": "One", "why": "w"}
    write(station, ".picks.jsonl", [
        {**now, "offered": ["a" * 22, "b" * 22]},
        {**now, "id": "c" * 22},
        {**now, "id": "d" * 22, "offered": ["d" * 22, "e" * 22]},
    ])
    # The newest two offers, and a pick without one is skipped rather than counted.
    assert queue.offered_ids(station, 2) == {"a" * 22, "b" * 22, "d" * 22, "e" * 22}


def test_recently_out_unions_the_ledger_and_the_picks(station):
    """One answer for both callers: the table's offer and the tool rows read this."""
    write(station, ".jsonl", [
        {"t": stamp(600), "id": "aired" * 4, "artist": "A", "title": "x", "source": "standby"},
    ])
    write(station, ".picks.jsonl", [
        # Chosen four hours ago and never aired: only the pick log knows about it.
        {"t": stamp(4 * 3600), "id": "picked" * 3, "artist": "B", "title": "y", "why": "w"},
    ])
    assert queue.recently_out(station) == {"aired" * 4, "picked" * 3}
    # A pick nobody heard is still a pick: the ledger alone would let it come straight
    # back, which is the failure the union is here to stop.


def test_record_pick_keeps_what_was_offered(station):
    queue.record_pick(station, "a" * 22, "x", "One", "why", detail={"offered": ["a" * 22] * 70})
    entry = json.loads(station.history_file.with_suffix(".picks.jsonl").read_text())
    assert entry["offered"] == ["a" * 22] * 64
    assert entry["id"] == "a" * 22


def test_a_pick_without_an_offer_logs_none(station):
    queue.record_pick(station, "a" * 22, "x", "One", "why", detail={"path": "shelf"})
    entry = json.loads(station.history_file.with_suffix(".picks.jsonl").read_text())
    assert "offered" not in entry
    assert entry["path"] == "shelf"


def test_ledger_stamps_round_trip_in_utc():
    from spica import ledger

    raw = ledger.utc_stamp(1700000000)
    assert raw == "2023-11-14T22:13:20Z"
    assert ledger.parse_stamp(raw) == 1700000000
    assert ledger.parse_stamp("2023-11-14T23:13:20+01:00") == 1700000000
    assert ledger.parse_stamp("not a stamp") is None
