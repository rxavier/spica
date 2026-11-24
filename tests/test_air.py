"""One airing path: the slot, the pool, the log and the summary move together."""

from __future__ import annotations

import pytest

from spica import air, config, navidrome
from spica.picker.schemas import Pick


def make_pick(track_id: str = "t1") -> Pick:
    return Pick(
        track=navidrome.Track(
            id=track_id, uri=f"http://navidrome/{track_id}", title="Title",
            artist="Artist", album="Album", album_id="al1", track_no=1, year=2000,
            genre="Rock", duration=200,
        ),
        why="Because.",
    )


@pytest.fixture
def station():
    return config.Station(id="alt", name="Alternative")


@pytest.fixture
def wired(monkeypatch):
    calls: dict[str, list] = {}
    air.LAST.clear()

    def record(name):
        def inner(*args, **kwargs):
            calls.setdefault(name, []).append((args, kwargs))
            return None
        return inner

    monkeypatch.setattr(air.queue, "push", lambda st, uri, label="": calls.setdefault("push", []).append(uri) or True)
    monkeypatch.setattr(air.queue, "replace", lambda st, uri, label="": calls.setdefault("replace", []).append(uri) or "displaced-id")
    monkeypatch.setattr(air.queue, "append_request", lambda st, uri, label="": calls.setdefault("append_request", []).append(uri) or 2)
    monkeypatch.setattr(air.queue, "promote_requests", lambda st: calls.setdefault("promote", []).append(1))
    monkeypatch.setattr(air.queue, "record_pick", record("record_pick"))
    monkeypatch.setattr(air.pool, "rewind_to", record("rewind_to"))
    monkeypatch.setattr(air.pool, "skip_past", record("skip_past"))
    monkeypatch.setattr(air.icecast, "listen_states", lambda: {})
    monkeypatch.setattr(air.icecast, "total_listeners", lambda states, st: 0)
    monkeypatch.setattr(air.presence, "reason", lambda station_id, n: "quiet")
    calls["monkeypatch"] = monkeypatch
    return calls


def test_a_background_pick_waits_for_a_free_slot(station, wired, monkeypatch):
    monkeypatch.setattr(air.queue, "push", lambda st, uri, label="": False)

    aired = air.air_pick(station, make_pick(), log=lambda *a: None)

    assert aired.ok is False
    assert "already waiting" in aired.why
    assert "record_pick" not in wired, "a dropped pick is not logged as played"
    assert station.id not in air.LAST


def test_a_forced_pick_displaces_and_returns_the_old_track_to_the_pool(station, wired):
    aired = air.air_pick(station, make_pick(), displace=True, log=lambda *a: None)

    assert aired.ok is True
    assert aired.displaced == "displaced-id"
    assert wired["replace"] and wired["rewind_to"]
    assert "skip_past" not in wired
    assert wired["record_pick"]
    assert air.LAST[station.id]["queued"].startswith("Title")


def test_a_listener_request_is_appended_and_promoted(station, wired):
    aired = air.air_pick(station, make_pick(), as_request=True, log=lambda *a: None)

    assert aired.ok is True
    assert aired.position == 2
    assert wired["append_request"] and wired["promote"]
    assert "push" not in wired and "replace" not in wired and "skip_past" not in wired
    assert wired["record_pick"]
