"""The play ledger: one mixer line is one airing, and repeats are kept."""

from __future__ import annotations

import json
import time

import pytest

from spica import ledger, plays


@pytest.fixture(autouse=True)
def clear_play_memory():
    plays._observed.clear()
    yield
    plays._observed.clear()


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


def test_each_mixer_line_is_one_airing_even_when_it_repeats(station, quiet_lookup):
    plays.record(station, "Roosevelt - Getaway")
    plays.record(station, "Roosevelt - Getaway")
    assert len(_lines(station)) == 2


def test_observe_records_a_title_once_per_change(station, quiet_lookup):
    plays.observe(station, "Roosevelt - Getaway")
    plays.observe(station, "Roosevelt - Getaway")
    assert len(_lines(station)) == 1
    plays.observe(station, "Incubus - Pendulous Threads")
    assert len(_lines(station)) == 2


def test_observe_seeds_from_the_ledger_on_restart(station, quiet_lookup):
    _append(station, 180, "Roosevelt", "Getaway", "getaway")
    plays._observed.clear()
    plays.observe(station, "Roosevelt - Getaway")
    assert len(_lines(station)) == 1


def test_a_rotation_play_keeps_the_track_id(station, monkeypatch):
    monkeypatch.setattr(
        plays.store, "find_track",
        lambda artist, title: {"id": "track-1", "title": "Getaway", "artist": "Roosevelt"},
    )
    monkeypatch.setattr(plays.ledger, "reason_for", lambda station, title: "")
    plays.record(station, "Roosevelt - Getaway")
    entry = _lines(station)[0]
    assert entry["id"] == "track-1"
    assert entry["source"] == "fallback_rotation"


def test_a_dj_pick_keeps_its_line(station, quiet_lookup, monkeypatch):
    monkeypatch.setattr(plays.ledger, "reason_for", lambda station, title: "Same line")
    plays.record(station, "Roosevelt - Getaway")
    entry = _lines(station)[0]
    assert entry["source"] == "dj"
    assert entry["why"] == "Same line"
