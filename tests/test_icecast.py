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
