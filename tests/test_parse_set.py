"""What the code accepts as an answer, and what it refuses.

Only facts are checked: the track exists, has a file, is on this station, is not
already airing or queued, and appears once. Nothing here tests length, artists, or any
other thing the prompt asks for — an operator may change the prompt, and the answer
must still be playable.
"""

from __future__ import annotations

import json

from spica.picker import loop


class FakeLibrary:
    """Just enough Library to answer for the ids a reply names."""

    def __init__(self, tracks: dict[str, str], station_ids: set[str] | None = None):
        self.tracks = tracks                      # id -> uri
        self.station_ids = set(tracks) if station_ids is None else station_ids

    def by_id(self, track_id):
        uri = self.tracks.get(track_id)
        if uri is None:
            return None
        return type("Track", (), {"id": track_id, "uri": uri})()

    def on_station(self, station, track_id):
        return track_id in self.station_ids


def library(**kwargs):
    return FakeLibrary(
        {"a" * 22: "http://x/a.flac", "b" * 22: "http://x/b.flac", "c" * 22: "http://x/c.flac"},
        **kwargs,
    )


T = "a" * 22
U = "b" * 22
V = "c" * 22


def parse(content, avoid=(), lib=None):
    return loop.parse_set(lib or library(), content, set(avoid), object())


def test_a_bare_object_is_read():
    theme, ids = parse(json.dumps({"theme": "a set", "ids": [T, U]}))
    assert theme == "a set"
    assert ids == [T, U]


def test_a_fenced_object_is_read():
    theme, ids = parse("```json\n" + json.dumps({"theme": "t", "ids": [T]}) + "\n```")
    assert ids == [T]


def test_prose_around_the_object_is_ignored():
    theme, ids = parse("Here is the set.\n" + json.dumps({"theme": "t", "ids": [U]}))
    assert ids == [U]


def test_the_first_usable_object_wins():
    content = json.dumps({"theme": "one", "ids": [T]}) + "\n" + json.dumps({"theme": "two", "ids": [U]})
    assert parse(content) == ("one", [T])


def test_ids_may_arrive_as_objects():
    """A model will sometimes answer with the row shape it just read."""
    _, ids = parse(json.dumps({"theme": "t", "ids": [{"id": T, "why": "x"}, {"id": U}]}))
    assert ids == [T, U]


def test_a_repeated_id_appears_once():
    _, ids = parse(json.dumps({"theme": "t", "ids": [T, T, U]}))
    assert ids == [T, U]


def test_an_unknown_id_is_dropped():
    _, ids = parse(json.dumps({"theme": "t", "ids": ["nope", T]}))
    assert ids == [T]


def test_an_id_from_another_station_is_dropped():
    _, ids = parse(json.dumps({"theme": "t", "ids": [U]}), lib=library(station_ids={T}))
    assert ids == []


def test_a_track_with_no_file_is_dropped():
    lib = FakeLibrary({T: ""})
    _, ids = parse(json.dumps({"theme": "t", "ids": [T]}), lib=lib)
    assert ids == []


def test_ids_already_airing_are_dropped():
    _, ids = parse(json.dumps({"theme": "t", "ids": [T, U]}), avoid={T})
    assert ids == [U]


def test_a_set_has_no_ceiling():
    """Three to ten is what the prompt asks for; the code does not enforce it."""
    many = [f"{i:022d}" for i in range(30)]
    lib = FakeLibrary({track_id: "http://x" for track_id in many})
    _, ids = parse(json.dumps({"theme": "t", "ids": many}), lib=lib)
    assert len(ids) == 30


def test_a_truncated_reply_yields_nothing():
    assert parse('{"theme": "t", "ids": ["' + T) == ("", [])


def test_an_empty_list_yields_nothing():
    assert parse(json.dumps({"theme": "t", "ids": []})) == ("", [])


def test_a_missing_ids_key_yields_nothing():
    assert parse(json.dumps({"theme": "t"})) == ("", [])


def test_prose_without_json_yields_nothing():
    assert parse("I would play something quiet next.") == ("", [])
