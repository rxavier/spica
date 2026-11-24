"""Presence is one reading; listening and reason are views of it."""

from __future__ import annotations

from spica import presence


def test_an_open_page_is_listening(monkeypatch):
    monkeypatch.setattr(presence, "_last_heard", {})
    monkeypatch.setattr(presence, "_sessions", {})
    presence.heard("alt")
    report = presence.report("alt", 0)
    assert report == {"listening": True, "reason": "listening (page open)"}
    assert presence.listening("alt", 0) is True
    assert presence.reason("alt", 0) == "listening (page open)"


def test_a_foreign_mount_connection_is_external(monkeypatch):
    monkeypatch.setattr(presence, "_last_heard", {})
    monkeypatch.setattr(presence, "_sessions", {})
    assert presence.reason("alt", 2) == "listening (external player)"


def test_nobody_is_idle(monkeypatch):
    monkeypatch.setattr(presence, "_last_heard", {})
    monkeypatch.setattr(presence, "_sessions", {})
    assert presence.report("alt", 0) == {"listening": False, "reason": "idle (nobody listening)"}
