"""The provider HTTP client: retries, and the errors it must not retry."""

from __future__ import annotations

import json
import urllib.error

from spica import enrich


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def _http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "nope", {}, None)


def test_a_retryable_status_is_tried_again(monkeypatch):
    calls: list[str] = []

    def fake_urlopen(request, timeout=0):
        calls.append(request.full_url)
        if len(calls) < 3:
            raise _http_error(request.full_url, 503)
        return _Response({"ok": True})

    monkeypatch.setattr(enrich.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(enrich.time, "sleep", lambda seconds: None)

    assert enrich._http_json("https://provider/x", attempts=3) == {"ok": True}
    assert len(calls) == 3


def test_a_client_error_is_raised_at_once(monkeypatch):
    calls: list[str] = []

    def fake_urlopen(request, timeout=0):
        calls.append(request.full_url)
        raise _http_error(request.full_url, 404)

    monkeypatch.setattr(enrich.urllib.request, "urlopen", fake_urlopen)

    try:
        enrich._http_json("https://provider/x", attempts=3)
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    else:
        raise AssertionError("a 404 must not be retried or swallowed")
    assert len(calls) == 1
