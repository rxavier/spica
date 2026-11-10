"""The play log.

History records what played, not what was chosen: Icecast names the track that is
on air, so a change there is a real play. Both the background loop and the page use
this, because the page polls every few seconds and would otherwise show yesterday's
reason under today's song until the loop caught up.
"""

from __future__ import annotations

from datetime import datetime

import time

from . import config, queue, store

# A track cannot legitimately come round again inside this window, so seeing the
# same title sooner than this is the poll loop, not a new play. Past it, the same
# title is the station coming back on air — a restart, or a mount that closed
# overnight and reopened on the track it was holding. That distinction is the
# difference between a ledger that is honest and one stuck on yesterday.
REPEAT_WINDOW_S = 15 * 60

_played: dict[str, str] = {}
_played_at: dict[str, float] = {}


def _stamp(row: dict) -> float:
    try:
        return datetime.fromisoformat(str(row.get("t", "")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _key(artist: str, title: str) -> str:
    return store.norm(f"{artist} {title}")


def note(st: config.Station, raw_title: str) -> None:
    """Record a play if the track on air is not the one already recorded."""
    raw = (raw_title or "").strip()
    if not raw:
        return

    # A restart must not look like a new play, so start from the last entry on
    # disk — but remember when it was, because an old entry means the station has
    # been off air since and the same title now is a fresh play.
    if st.id not in _played:
        last = queue.recent(st, 1)
        if last:
            _played[st.id] = _key(last[-1].get("artist", ""), last[-1].get("title", ""))
            _played_at[st.id] = _stamp(last[-1])
        else:
            _played[st.id] = ""
            _played_at[st.id] = 0.0

    artist, song = queue.split_title(raw)
    key = _key(artist, song)
    if _played.get(st.id) == key and time.time() - _played_at.get(st.id, 0.0) < REPEAT_WINDOW_S:
        return
    _played[st.id] = key
    _played_at[st.id] = time.time()

    row = store.find_track(artist, song or raw)
    why = queue.why_for(st, song or raw)
    queue.record(
        st,
        row["id"] if row else "",
        (row["title"] if row else song) or raw,
        (row["artist"] if row else artist),
        "dj" if why else "standby",
        why,
    )
