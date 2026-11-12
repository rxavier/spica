"""Icecast status: who is listening, and to what.

One read of the status page costs about 200 ms of wall clock — most of it Icecast
pacing itself, only ~5 ms of its CPU — and the page poll, /status, the pick loop
and the DJ's brief all want the same answer within a few seconds. So it is read
once, on a timer, into a snapshot every caller shares; nobody waits on Icecast
inside a request any more.

The snapshot is a plain dict of frozen values, swapped whole, so a reader always
sees one consistent picture and never a half-updated one.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx

from . import config


@dataclass(frozen=True)
class MountState:
    mount: str
    listeners: int
    title: str


# A snapshot older than this is no longer evidence about now. With Icecast down,
# "no mounts" is the answer the pick loop wants: nobody can hear anything, so it
# stops picking until Icecast is back.
STALE_S = max(6.0, config.ICECAST_POLL_S * 3)

_snapshot: dict[str, MountState] = {}
_fetched_at = 0.0
_thread: threading.Thread | None = None


def _as_list(source) -> list[dict]:
    if source is None:
        return []
    return source if isinstance(source, list) else [source]


def refresh() -> dict[str, MountState]:
    """Read the status page now and publish it. Returns the new (or previous) snapshot."""
    global _snapshot, _fetched_at
    try:
        r = httpx.get(f"{config.ICECAST_URL}/status-json.xsl", timeout=5.0)
        r.raise_for_status()
        body = r.json().get("icestats", {})
    except Exception:
        # An Icecast hiccup is not news: keep what we had and let it go stale.
        return _snapshot
    out: dict[str, MountState] = {}
    for src in _as_list(body.get("source")):
        listen_url = src.get("listenurl") or ""
        mount = "/" + listen_url.rsplit("/", 1)[-1] if listen_url else ""
        if not mount:
            continue
        out[mount] = MountState(
            mount=mount,
            listeners=int(src.get("listeners") or 0),
            title=str(src.get("title") or src.get("yp_currently_playing") or ""),
        )
    _snapshot, _fetched_at = out, time.monotonic()
    return out


def listen_states() -> dict[str, MountState]:
    """The current snapshot. Never blocks, never raises, never goes to Icecast."""
    if time.monotonic() - _fetched_at > STALE_S:
        return {}
    return _snapshot


def start_refresher() -> None:
    """Keep the snapshot current from a daemon thread. Idempotent."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return

    def run() -> None:
        while True:
            refresh()
            time.sleep(config.ICECAST_POLL_S)

    _thread = threading.Thread(target=run, name="icecast-status", daemon=True)
    _thread.start()


def listeners(states: dict[str, MountState], mount: str) -> int:
    st = states.get(mount)
    return st.listeners if st else 0


def live_mount(states: dict[str, MountState], station) -> tuple[str, MountState] | None:
    """The first of the station's streams that is actually on air.

    A station has one mount per stream, and which of them is open depends on what
    somebody asked for. Anything that wants "what is this station doing" — the
    ledger's title lookup, the presence check — goes through here.
    """
    for mount in station.mounts:
        state = states.get(mount)
        if state is not None:
            return mount, state
    return None


def total_listeners(states: dict[str, MountState], station) -> int:
    return sum(states[m].listeners for m in station.mounts if m in states)
