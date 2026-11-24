"""The status reader survives one malformed mount."""

from __future__ import annotations

from spica import icecast


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


def test_a_bad_listener_count_does_not_drop_the_snapshot(monkeypatch):
    monkeypatch.setattr(
        icecast.httpx, "get",
        lambda url, timeout=5.0: _Response({
            "icestats": {"source": [
                {"listenurl": "http://mixer:8000/alt", "listeners": "not-a-number",
                 "title": "Artist - Song"},
                {"listenurl": "http://mixer:8000/all", "listeners": "3"},
            ]}
        }),
    )

    states = icecast.refresh()

    assert states["/alt"].listeners == 0
    assert states["/alt"].title == "Artist - Song"
    assert states["/all"].listeners == 3


def test_starting_the_refresher_twice_starts_one_thread(monkeypatch):
    started: list[object] = []

    class _Thread:
        def __init__(self, *args, **kwargs) -> None:
            self.args = args
            self.alive = False
            started.append(self)

        def start(self) -> None:
            self.alive = True

        def is_alive(self) -> bool:
            return self.alive

    monkeypatch.setattr(icecast, "_thread", None)
    monkeypatch.setattr(icecast.threading, "Thread", _Thread)

    icecast.start_refresher()
    icecast.start_refresher()

    assert len(started) == 1
