"""Icecast status: who is listening, and to what."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from . import config


@dataclass
class MountState:
    mount: str
    listeners: int
    title: str


def _as_list(source) -> list[dict]:
    if source is None:
        return []
    return source if isinstance(source, list) else [source]


def listen_states() -> dict[str, MountState]:
    """mount -> state, from the public status page (no admin auth needed)."""
    try:
        r = httpx.get(f"{config.ICECAST_URL}/status-json.xsl", timeout=5.0)
        r.raise_for_status()
        body = r.json().get("icestats", {})
    except Exception:
        return {}
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
    return out


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
