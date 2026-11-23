"""The request path: a request drops the rest of the set, and only a request does."""

from __future__ import annotations

from spica import mixer, plan, queue


def write_requests(station, lines):
    queue._write_request_lines(station, lines)


def test_slot_and_request_temp_files_differ(station):
    """A pick and a request are written in parallel; one temp name would let them
    clobber each other mid-write."""
    slot_tmp = queue._tmp(station.queue_file)
    request_tmp = queue._tmp(queue.due_file(station))
    assert slot_tmp != request_tmp


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


def test_set_queue_reports_command_errors(station, monkeypatch):
    """An ERROR reply is a failed push: the caller must not mark requests aired."""
    monkeypatch.setattr(mixer, "_pending_rids", lambda st: ["1"])
    monkeypatch.setattr(mixer, "command", lambda line: "ERROR no such request")

    assert mixer.set_queue(station, ["http://navidrome/req.flac"]) is False


def test_no_control_server_leaves_the_set_alone(station, monkeypatch):
    plan.save(station, "still playing", ["a", "b"])
    write_requests(station, [{"uri": "http://navidrome/req.flac", "label": "a request"}])
    monkeypatch.setattr(mixer, "pending_uris", lambda st: None)

    assert queue.promote_requests(station) == 0
    assert plan.load(station) is not None
