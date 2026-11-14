"""The set on disk: order, expiry, and the ways it goes away."""

from __future__ import annotations

import json
import time

from spica import plan


def test_a_saved_set_comes_back_in_order(station):
    plan.save(station, "a line about the set", ["a", "b", "c"])
    step = plan.take(station)
    assert step is not None
    first, held = step
    assert first == "a"
    assert held["theme"] == "a line about the set"
    assert plan.remaining(station) == 2


def test_the_last_track_clears_the_file(station):
    plan.save(station, "t", ["only"])
    assert plan.take(station)[0] == "only"
    assert plan.load(station) is None
    assert not station.plan_file.exists()


def test_a_track_that_can_no_longer_play_is_skipped(station):
    """A set outlives the decision that made it: one dead id must not kill the set."""
    plan.save(station, "t", ["dead", "b", "c"])
    first, _ = plan.take(station, blocked={"dead"})
    assert first == "b"
    assert plan.remaining(station) == 1


def test_every_track_blocked_clears_the_set(station):
    plan.save(station, "t", ["a", "b"])
    assert plan.take(station, blocked={"a", "b"}) is None
    assert plan.load(station) is None


def test_an_old_set_expires(station):
    """Coming back after an outage should not play out a mood from before it."""
    plan.save(station, "t", ["a", "b"])
    data = json.loads(station.plan_file.read_text())
    data["made"] = time.time() - plan.TTL_SECONDS - 1
    station.plan_file.write_text(json.dumps(data))
    assert plan.take(station) is None
    assert not station.plan_file.exists()


def test_a_corrupt_set_is_not_a_crash(station):
    station.plan_file.parent.mkdir(parents=True, exist_ok=True)
    station.plan_file.write_text("{not json")
    assert plan.load(station) is None
    assert plan.take(station) is None


def test_no_set_is_not_an_error(station):
    assert plan.load(station) is None
    assert plan.take(station) is None
    assert plan.remaining(station) == 0


def test_stations_do_not_share_a_set(station, other_station):
    plan.save(station, "alt set", ["x"])
    plan.save(other_station, "other set", ["y"])
    assert plan.load(station)["theme"] == "alt set"
    assert plan.load(other_station)["theme"] == "other set"


def test_clear_is_quiet_when_there_is_nothing_to_clear(station):
    plan.clear(station)
    plan.clear(station, "listener request")
    assert plan.load(station) is None
