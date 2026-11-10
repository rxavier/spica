"""Who is actually listening.

Icecast counts open connections, which is not the same thing: a browser tab left in the
background, or a player paused mid-buffer, holds one for hours. So presence is built
from three honest signals instead:

  * a browser says it is playing — the page posts a heartbeat every 20 s;
  * a real player (VLC, Sonos, Music Assistant) is pulling bytes through the relay —
    a paused one keeps its socket open but stops reading, so a session that has not
    delivered a chunk for a while is treated as paused, not as a listener;
  * somebody connected straight to the mount, which only counts when the connection did
    not come from our relay. That case cannot be measured, because the bytes never pass
    through us.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict

HEARTBEAT_S = 60.0  # how long a heartbeat keeps a browser "listening"
STALL_S = 45.0  # a relay client that has not taken bytes this long is paused

_BROWSER_MARKS = ("mozilla", "chrome", "safari", "firefox", "edg/", "webkit", "opera")

_last_heard: dict[str, float] = {}
_sessions: dict[str, dict[str, dict]] = defaultdict(dict)


def heard(station_id: str) -> None:
    _last_heard[station_id] = time.time()


def heartbeat_age(station_id: str) -> float:
    return time.time() - _last_heard.get(station_id, 0.0)


def relay_open(station_id: str, user_agent: str) -> str:
    """Start a relay session and say what kind of client it is."""
    ua = (user_agent or "").lower()
    kind = "browser" if any(mark in ua for mark in _BROWSER_MARKS) else "player"
    token = uuid.uuid4().hex
    _sessions[station_id][token] = {"kind": kind, "last": time.time()}
    return token


def relay_tick(station_id: str, token: str) -> None:
    """A chunk went out to that client, so it is still reading."""
    session = _sessions.get(station_id, {}).get(token)
    if session is not None:
        session["last"] = time.time()


def relay_close(station_id: str, token: str) -> None:
    _sessions.get(station_id, {}).pop(token, None)


def relay_summary(station_id: str) -> dict[str, int]:
    now = time.time()
    open_all = _sessions.get(station_id, {})
    return {
        "all": len(open_all),
        "reading": sum(1 for s in open_all.values() if now - s["last"] <= STALL_S),
        "players": sum(
            1
            for s in open_all.values()
            if s["kind"] == "player" and now - s["last"] <= STALL_S
        ),
    }


def listening(station_id: str, mount_listeners: int) -> bool:
    """True when somebody is plausibly in front of the radio right now."""
    if heartbeat_age(station_id) <= HEARTBEAT_S:
        return True
    summary = relay_summary(station_id)
    if summary["players"] > 0:
        return True
    # Anything left on the mount that is not one of our own relay sessions is a
    # foreign connection (a player pointed straight at Icecast).
    return max(0, mount_listeners - summary["all"]) > 0


def reason(station_id: str, mount_listeners: int) -> str:
    if heartbeat_age(station_id) <= HEARTBEAT_S:
        return "listening (page open)"
    summary = relay_summary(station_id)
    if summary["players"] > 0:
        return "listening (player connected)"
    if max(0, mount_listeners - summary["all"]) > 0:
        return "listening (external player)"
    if summary["all"]:
        return "idle (client paused)"
    return "idle (nobody listening)"
