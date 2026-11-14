"""The request path: a request drops the rest of the set, and only a request does."""

from __future__ import annotations

from spica import mixer, plan, queue


def write_requests(station, lines):
    queue._write_request_lines(station, lines)


def test_a_promoted_request_drops_the_set(station, monkeypatch):
    plan.save(station, "the mood they left", ["a", "b"])
    write_requests(station, [{"uri": "http://navidrome/req.flac", "label": "a request"}])
    monkeypatch.setattr(mixer, "pending_uris", lambda st: [])
    monkeypatch.setattr(mixer, "set_queue", lambda st, uris: True)

    assert queue.promote_requests(station) == 1
    assert plan.load(station) is None, "the rest of the set was chosen for the old mood"


def test_nothing_new_leaves_the_set_alone(station, monkeypatch):
    """The queue already holds a fresh pick: rewriting it would drop that instead."""
    plan.save(station, "still playing", ["a", "b"])
    write_requests(station, [{"uri": "http://navidrome/req.flac", "label": "a request",
                              "sent": True}])
    monkeypatch.setattr(mixer, "pending_uris", lambda st: ["http://navidrome/req.flac"])
    monkeypatch.setattr(mixer, "set_queue", lambda st, uris: True)

    assert queue.promote_requests(station) == 0
    assert plan.load(station) is not None


def test_no_control_server_leaves_the_set_alone(station, monkeypatch):
    plan.save(station, "still playing", ["a", "b"])
    write_requests(station, [{"uri": "http://navidrome/req.flac", "label": "a request"}])
    monkeypatch.setattr(mixer, "pending_uris", lambda st: None)

    assert queue.promote_requests(station) == 0
    assert plan.load(station) is not None
