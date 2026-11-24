"""The play ledger: one airing, one line."""

from __future__ import annotations

import json
import time

import pytest

from spica import ledger, plays


@pytest.fixture(autouse=True)
def clear_play_memory():
    plays._played.clear()
    plays._played_at.clear()
    yield
    plays._played.clear()
    plays._played_at.clear()


def _append(station, seconds_ago: float, artist: str, title: str, track_id: str) -> None:
    station.history_file.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "t": ledger.utc_stamp(time.time() - seconds_ago),
        "id": track_id,
        "title": title,
        "artist": artist,
        "source": "fallback_rotation",
        "why": "",
    }
    with station.history_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _lines(station) -> list[dict]:
    return [
        json.loads(line)
        for line in station.history_file.read_text(encoding="utf-8").splitlines()
    ]


@pytest.fixture
def quiet_lookup(monkeypatch):
    monkeypatch.setattr(plays.store, "find_track", lambda artist, title: None)
    monkeypatch.setattr(plays.ledger, "reason_for", lambda station, title: "")


def test_a_track_still_on_air_is_recorded_once(station, quiet_lookup):
    _append(station, 180, "Roosevelt", "Getaway", "getaway")
    plays.note(station, "Roosevelt - Getaway")
    assert len(_lines(station)) == 1


def test_a_restart_does_not_duplicate_the_airing(station, quiet_lookup):
    _append(station, 180, "Roosevelt", "Getaway", "getaway")
    plays._played.clear()
    plays._played_at.clear()
    plays.note(station, "Roosevelt - Getaway")
    assert len(_lines(station)) == 1


def test_a_new_track_is_recorded(station, quiet_lookup):
    _append(station, 180, "Roosevelt", "Getaway", "getaway")
    plays.note(station, "Incubus - Pendulous Threads")
    rows = _lines(station)
    assert len(rows) == 2
    assert rows[-1]["title"] == "Pendulous Threads"


def test_a_track_can_play_again_after_the_window(station, quiet_lookup):
    _append(station, 16 * 60, "Roosevelt", "Getaway", "getaway")
    plays.note(station, "Roosevelt - Getaway")
    assert len(_lines(station)) == 2
