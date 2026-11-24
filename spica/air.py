"""The one place a pick goes from the picker's head onto the station.

Every path that airs a pick — the background loop, the REST request, an operator's
forced pick and the listener form — has to touch the same four things: the mixer
queue, the fallback rotation, the pick log and the station's last-airing summary. They
were assembled by hand in each path and had already drifted, so they live here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import icecast, pool, presence, queue


@dataclass(frozen=True)
class Aired:
    ok: bool
    position: int = 0
    displaced: str = ""
    why: str = ""


LAST: dict[str, dict] = {}


def air_pick(st, pick, *, displace: bool = False, as_request: bool = False,
             log=print) -> Aired:
    """Put `pick` on `st`, record it, and update the station's last-airing summary.

    Three ways to take the slot, each preserving what the caller means:
      - `displace`: a person asked for this, so it goes next and the track that was
        waiting returns to the head of the pool.
      - `as_request`: a listener request, which is appended behind other requests
        and promoted ahead of the picker's pick.
      - neither: the background loop, which may wait for a free slot and never
        preempts one. A full slot is a refusal, not an airing.
    """
    uri = pick.track.uri
    line = pick.track.as_line()
    displaced = ""
    position = 0

    if as_request:
        position = queue.append_request(st, uri, line)
        queue.promote_requests(st)
    elif displace:
        displaced = queue.replace(st, uri, line)
        if displaced:
            pool.rewind_to(st, displaced, log=log)
        else:
            pool.skip_past([st], pick.track.id, log=log)
    else:
        if not queue.push(st, uri, line):
            return Aired(False, why="one already waiting — pick dropped")
        pool.skip_past([st], pick.track.id, log=log)

    states = icecast.listen_states()
    listeners = icecast.total_listeners(states, st)
    queue.record_pick(
        st,
        pick.track.id,
        pick.track.title,
        pick.track.artist,
        pick.reason,
        presence=presence.reason(st.id, listeners),
        detail=pick.detail(),
    )
    LAST[st.id] = {
        "listeners": listeners,
        "pushed": time.time(),
        "queued": line,
        "why": pick.reason,
        "at": time.strftime("%H:%M:%S"),
    }
    return Aired(True, position=position, displaced=displaced)
