"""A set is only planned against a record that will still be on air when it is chosen."""
from __future__ import annotations

import time

from spica import queue
from spica.picker import loop


class Track:
    duration = 200


class Lib:
    def by_id(self, track_id):
        return Track()


def stamp(ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago))


def test_a_track_near_its_end_is_not_a_stable_anchor(station, monkeypatch):
    monkeypatch.setattr(queue, "recent", lambda st, n=20: [{"id": "x", "t": stamp(170)}])
    assert loop._enough_runway(Lib(), station) is False  # 30 s left, under the lead


def test_a_fresh_track_is_a_stable_anchor(station, monkeypatch):
    monkeypatch.setattr(queue, "recent", lambda st, n=20: [{"id": "x", "t": stamp(5)}])
    assert loop._enough_runway(Lib(), station) is True


def test_no_history_or_no_duration_does_not_block(station, monkeypatch):
    monkeypatch.setattr(queue, "recent", lambda st, n=20: [])
    assert loop._enough_runway(Lib(), station) is True
    monkeypatch.setattr(queue, "recent", lambda st, n=20: [{"id": "x"}])
    assert loop._enough_runway(Lib(), station) is True
