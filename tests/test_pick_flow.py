"""The picker's flow: a set is served, a request replaces it, and the model is only
asked when there is nothing left to play.

Everything outside the picker — Icecast, the mixer, the request file, the network — is
stubbed, so these run in milliseconds and cannot reach a real station.
"""

from __future__ import annotations

import json

import pytest

from spica import config, plan
from spica.picker import loop

T = "a" * 22
U = "b" * 22
V = "c" * 22


class FakeLibrary:
    draw_seed = 0

    def __init__(self, ids):
        self.ids = list(ids)

    def by_id(self, track_id):
        if track_id not in self.ids:
            return None
        return type(
            "Track",
            (),
            {
                "id": track_id,
                "uri": f"http://navidrome/{track_id}.flac",
                "title": f"title {track_id[:2]}",
                "artist": f"artist {track_id}",
                "album_artist": f"artist {track_id}",
                "as_line": lambda self=None: "line",
            },
        )()

    def on_station(self, station, track_id):
        return track_id in self.ids

    def recent_labels(self, station, n=5):
        return ["Rock"], ["Someone"]

    def catalog_summary(self, station=None, limit=60):
        return {"tracks": 3, "artists": [("Someone", 3)], "artists_total": 1,
                "labels": [("Rock", 3)], "labels_total": 1}

    def run_tool(self, name, args, station=None):
        return []

def fake_shelf(lib, station):
    """Every track on the fake station, one artist each, so sets survive validation."""
    return [{"artist": f"artist {i}", "reason": "candidate"} for i in lib.ids]


@pytest.fixture
def flow(monkeypatch, station):
    """A picker wired to stubs, plus the list of messages it sent to the model."""
    lib = FakeLibrary([T, U, V])
    sent: list[list[dict]] = []
    replies: list[str] = []

    def fake_chat(messages, tools, no_tools=False, require_call=False):
        sent.append([dict(m) for m in messages])
        if not replies:
            return {"content": ""}
        return {"content": replies.pop(0)}

    monkeypatch.setattr(loop, "chat", fake_chat)
    monkeypatch.setattr(loop, "already_airing", lambda lib, st: set())
    monkeypatch.setattr(loop.artists, "build", fake_shelf)
    monkeypatch.setattr(loop.queue, "recent", lambda st, n=20: [])
    monkeypatch.setattr(loop.mixer, "queued_ids", lambda st: [])
    monkeypatch.setattr(loop.icecast, "listen_states", lambda: {})
    monkeypatch.setattr(loop.icecast, "live_mount", lambda states, st: None)
    monkeypatch.setattr(loop.requests_mod, "active", lambda st: [])
    monkeypatch.setattr(config, "llm_settings",
                        lambda: {"base": "http://stub", "key": "k", "model": "m", "headers": {}})
    return lib, sent, replies


def a_set(*ids, theme="a line about the set"):
    return json.dumps({"theme": theme, "ids": list(ids)})


def test_a_stored_set_is_served_without_asking_the_model(flow, station, monkeypatch):
    lib, sent, replies = flow
    plan.save(station, "from earlier", [U])
    monkeypatch.setattr(loop, "chat", lambda *a, **k: pytest.fail("the model was asked"))

    pick = loop.pick_next(lib, station, None, set())
    assert pick is not None
    assert pick.track.id == U
    assert pick.set_rationale == "from earlier"
    assert pick.reason == "from earlier"
    assert plan.load(station) is None


def test_a_set_survives_a_model_that_is_not_configured(flow, station, monkeypatch):
    """An outage stops planning, not the night."""
    lib, _, _ = flow
    plan.save(station, "from earlier", [T, U])
    monkeypatch.setattr(config, "llm_settings",
                        lambda: {"base": "", "key": "", "model": "", "headers": {}})

    first = loop.pick_next(lib, station, None, set())
    second = loop.pick_next(lib, station, None, {T})
    assert first is not None and second is not None
    assert (first.track.id, second.track.id) == (T, U)


def test_nothing_to_play_and_no_model_is_a_clean_none(flow, station, monkeypatch):
    lib, _, _ = flow
    monkeypatch.setattr(config, "llm_settings",
                        lambda: {"base": "", "key": "", "model": "", "headers": {}})
    assert loop.pick_next(lib, station, None, set()) is None


def test_the_first_new_reply_is_planned_and_the_rest_is_kept(flow, station):
    lib, sent, replies = flow
    replies.append(a_set(T, U, V))

    pick = loop.pick_next(lib, station, None, set())
    assert pick is not None
    assert pick.track.id == T
    held = plan.load(station)
    assert held is not None
    assert held["ids"] == [U, V]
    assert held["set_rationale"] == pick.set_rationale


def test_a_request_drops_the_set_and_asks_again(flow, station):
    lib, sent, replies = flow
    plan.save(station, "the old mood", [U, V])
    replies.append(a_set(T, U, theme="something heavier"))

    pick = loop.pick_next(lib, station, "play something heavier", set())

    assert pick is not None
    assert pick.set_rationale == "something heavier", "a request is a new decision"
    held = plan.load(station)
    assert held is not None
    assert held["ids"] == [U], "the old set does not survive behind it"
    assert any(
        "Listener request: play something heavier" in m["content"]
        for m in sent[0]
        if m["role"] == "user"
    )


def test_the_brief_names_the_shelf_and_nothing_else(flow, station):
    lib, sent, replies = flow
    replies.append(a_set(T))

    loop.pick_next(lib, station, None, set())

    brief = next(m["content"] for m in sent[0] if m["role"] == "user")
    assert "Artist candidates for this set, and nothing else" in brief
    for track_id in lib.ids:
        assert f"artist {track_id}" in brief
    # What played is not repeated: the artists that aired are simply not on the shelf.
    assert "haven't played" not in brief
    assert "times in the last hour" not in brief


def test_a_request_is_not_policed_by_the_shelf(flow, station):
    lib, sent, replies = flow
    replies.append(a_set(T))

    loop.pick_next(lib, station, "play something heavier", set())

    brief = next(m["content"] for m in sent[0] if m["role"] == "user")
    assert "Artist candidates" not in brief


def test_a_track_off_the_shelf_is_dropped(flow, station, monkeypatch):
    lib, sent, replies = flow
    monkeypatch.setattr(
        loop.artists, "build",
        lambda l, st: [{"artist": f"artist {T}", "reason": "candidate"}],
    )
    replies.append(a_set(T, U, V))

    pick = loop.pick_next(lib, station, None, set())

    assert pick is not None and pick.track.id == T
    assert not (plan.load(station) or {}).get("ids"), "only the artist on the shelf got through"


def test_an_answer_that_ignores_the_prompt_is_still_taken(flow, station):
    """The prompt is the operator's; the code does not police it."""
    lib, sent, replies = flow
    replies.append(a_set(T, U))

    pick = loop.pick_next(lib, station, None, set())

    assert pick is not None
    assert len(plan.load(station)["ids"]) == 1


def test_the_pick_records_what_it_read(flow, station):
    lib, sent, replies = flow
    replies.append(a_set(T))

    pick = loop.pick_next(lib, station, None, set())
    assert pick is not None
    assert pick.prompt_context and "Choose the next set" in pick.prompt_context
    assert pick.reply.startswith("{")


def test_off_mode_never_asks_the_model(flow, station, monkeypatch):
    lib, sent, _ = flow
    monkeypatch.setattr(config, "dj_mode", lambda: "off")
    monkeypatch.setattr(loop, "chat", lambda *a, **k: pytest.fail("the model was asked"))

    assert loop.pick_next(lib, station, None, set()) is None
    assert sent == []


def test_artist_mode_skips_the_table(flow, station, monkeypatch):
    lib, sent, replies = flow
    monkeypatch.setattr(config, "dj_mode", lambda: "artist")
    called = []
    monkeypatch.setattr(loop.tracks, "build",
                        lambda lib, st: called.append(True) or None)
    replies.append(a_set(T))

    pick = loop.pick_next(lib, station, None, set())
    assert pick is not None and pick.track.id == T
    assert called == [], "artist mode must not draw a table"


def test_track_mode_tries_the_table(flow, station, monkeypatch):
    lib, sent, replies = flow
    monkeypatch.setattr(config, "dj_mode", lambda: "track")
    called = []
    monkeypatch.setattr(loop.tracks, "build",
                        lambda lib, st: called.append(True) or None)
    replies.append(a_set(T))

    loop.pick_next(lib, station, None, set())
    assert called == [True]
