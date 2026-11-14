"""The shelf: what the DJ is offered, and what happens to what it offers back.

Several tracks now share an artist on purpose — the shelf is a list of artists, and
"no artist twice in a set" is half of what it enforces — so the fakes here give every
track its own artist unless a test says otherwise.
"""

from __future__ import annotations

import json
from collections import Counter

from spica import config, navidrome
from spica.picker import loop, shelf
from spica.picker.schemas import Pick


class FakeTrack:
    def __init__(self, track_id, artist):
        self.id = track_id
        self.uri = f"http://navidrome/{track_id}.flac"
        self.title = f"title {track_id}"
        self.artist = artist
        self.album_artist = artist

    def as_line(self):
        return f"{self.artist} - {self.title}"


class FakeLib:
    draw_seed = 0

    def __init__(self, tracks):
        self.tracks = {t.id: t for t in tracks}

    def by_id(self, track_id):
        return self.tracks.get(track_id)

    def on_station(self, station, track_id):
        return track_id in self.tracks


def profile(name, label, decade, tier, tracks=1):
    return {"key": navidrome.name_key(name), "name": name, "label": label,
            "decade": decade, "tier": tier, "tracks": tracks,
            "labels": Counter({label: tracks}), "years": [decade + 5], "pop": [0.5]}


# ------------------------------------------------------------------ quotas


def test_quotas_follow_weight_under_a_cap():
    quota = shelf._quotas(Counter({"Rock": 27, "Indie": 6, "Disco": 5}), 7, 3)
    assert quota == {"Rock": 3, "Indie": 2, "Disco": 2}
    assert max(quota.values()) <= 3


def test_quotas_never_hand_one_label_the_list():
    """Two labels and a cap of three cannot pay for ten names; the caller tops up."""
    quota = shelf._quotas(Counter({"Rock": 100, "Disco": 1}), 10, 3)
    assert quota == {"Rock": 3, "Disco": 3}
    assert sum(quota.values()) < 10


# ------------------------------------------------------------------ the draw


def a_pool():
    pool = []
    for i in range(15):
        pool.append(profile(f"Rock {i}", "Rock", 1990, i % 3))
    for i in range(8):
        pool.append(profile(f"Pop {i}", "Pop", 2000, i % 3))
    for i in range(7):
        pool.append(profile(f"Disco {i}", "Disco", 2010, i % 3))
    for i in range(6):
        pool.append(profile(f"Funk {i}", "Funk", 2020, i % 3))
    return pool


def test_the_shelf_is_a_small_version_of_the_pool():
    drawn = shelf._stratified(a_pool(), shelf.SHELF, seed=7)

    assert len(drawn) == shelf.SHELF
    assert len({p["key"] for p in drawn}) == shelf.SHELF, "no artist twice"
    per_label = Counter(p["label"] for p in drawn)
    assert per_label.most_common(1)[0][1] <= shelf.PER_LABEL, "one label cannot own it"
    assert len(per_label) >= 2, "the shelf is not one corner of the station"
    assert len({p["decade"] for p in drawn}) >= 2, "two eras"
    assert {p["tier"] for p in drawn} == {0, 1, 2}, "both ends of the roster"


def test_the_cap_yields_before_the_list_does():
    """Two labels cannot fill ten names three at a time; a short shelf is worse."""
    pool = [profile(f"Rock {i}", "Rock", 1990, i % 3) for i in range(10)]
    pool += [profile(f"Pop {i}", "Pop", 2000, i % 3) for i in range(10)]

    drawn = shelf._stratified(pool, shelf.SHELF, seed=4)

    assert len(drawn) == shelf.SHELF
    assert Counter(p["label"] for p in drawn).most_common(1)[0][1] > shelf.PER_LABEL


def test_a_thin_pool_comes_back_whole():
    pool = a_pool()[:4]
    drawn = shelf._stratified(pool, shelf.SHELF, seed=1)
    assert len(drawn) == 4


def test_the_shelf_a_set_ago_is_not_offered_again(monkeypatch, tmp_path):
    """Rotation memory: offered once, not again next set, played or not."""
    from spica import config

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    station = config.Station(id="rot", name="Rotation")
    station.history_file.parent.mkdir(parents=True, exist_ok=True)
    station.history_file.with_suffix(".picks.jsonl").write_text(
        json.dumps({"t": "2026-11-14T00:00:00Z", "id": "x", "shelf": ["Old One", "Old Two"]}) + "\n",
        encoding="utf-8",
    )
    assert shelf.queue.last_shelf(station) == ["Old One", "Old Two"]


def test_a_shelf_too_small_to_choose_from_is_no_shelf_at_all(monkeypatch):
    """Two names is a wall, not a choice: the restriction is dropped, not tightened."""
    class Tiny:
        draw_seed = 5

        def _station_rows(self, station):
            return [object(), object()]

    two = [profile("One", "Rock", 1990, 1), profile("Two", "Pop", 2000, 2)]
    monkeypatch.setattr(shelf, "_profiles", lambda lib, rows: {p["key"]: p for p in two})
    monkeypatch.setattr(shelf, "_blocked", lambda lib, station, profiles: set())
    monkeypatch.setattr(shelf, "_longest_absent", lambda lib, station, pool, k: [])

    assert shelf.build(Tiny(), object()) == []


def test_the_draw_changes_with_the_seed():
    first = {p["key"] for p in shelf._stratified(a_pool(), shelf.SHELF, seed=1)}
    second = {p["key"] for p in shelf._stratified(a_pool(), shelf.SHELF, seed=2)}
    assert first != second


# ------------------------------------------------------------- longest absent


def test_the_two_extra_slots_are_whoever_has_been_off_longest(monkeypatch):
    tracks = [FakeTrack(f"t{i:02d}", f"Artist {i}") for i in range(8)]
    lib = FakeLib(tracks)
    lib.draw_seed = 3
    # Newest last: Artist 0 played most recently, Artist 7 never played at all.
    plays = [{"id": f"t{i:02d}", "artist": f"Artist {i}"} for i in range(7)]
    monkeypatch.setattr(shelf.queue, "recent", lambda station, n=20: plays)

    pool = [{"key": navidrome.name_key(f"Artist {i}"), "name": f"Artist {i}"} for i in range(8)]
    extra = shelf._longest_absent(lib, object(), pool, 2)

    names = [p["name"] for p in extra]
    assert "Artist 7" in names, "never played is the longest absence there is"
    assert "Artist 0" not in names and "Artist 6" not in names, "recent ones do not qualify"
    assert all(p["why"] == "absent" for p in extra)


# ------------------------------------------------------------------ enforcement


def a_set(*artists_ids):
    return json.dumps({"theme": "a line", "ids": [i for i, _ in artists_ids]})


def test_a_set_may_not_leave_the_shelf(station):
    lib = FakeLib([
        FakeTrack("a" * 22, "On The Shelf"),
        FakeTrack("b" * 22, "Off The Shelf"),
    ])
    allowed = {navidrome.name_key("On The Shelf")}

    theme, ids = loop.parse_set(
        lib, a_set(("a" * 22, "on"), ("b" * 22, "off")), set(), station, allowed=allowed
    )

    assert theme == "a line"
    assert ids == ["a" * 22]


def test_no_artist_twice_in_one_set(station):
    lib = FakeLib([
        FakeTrack("a" * 22, "One Artist"),
        FakeTrack("b" * 22, "One Artist"),
        FakeTrack("c" * 22, "Another Artist"),
    ])
    allowed = {navidrome.name_key("One Artist"), navidrome.name_key("Another Artist")}

    _, ids = loop.parse_set(
        lib, a_set(("a" * 22, ""), ("b" * 22, ""), ("c" * 22, "")), set(), station,
        allowed=allowed,
    )

    assert ids == ["a" * 22, "c" * 22]


def test_without_a_shelf_nothing_is_restricted(station):
    """A listener request does not go through the shelf at all."""
    lib = FakeLib([
        FakeTrack("a" * 22, "One Artist"),
        FakeTrack("b" * 22, "One Artist"),
    ])

    _, ids = loop.parse_set(lib, a_set(("a" * 22, ""), ("b" * 22, "")), set(), station)

    assert ids == ["a" * 22, "b" * 22]


def test_the_pick_carries_the_shelf_it_was_given():
    pick = Pick(track=FakeTrack("a" * 22, "One Artist"), why="line")
    assert pick.shelf == ()
