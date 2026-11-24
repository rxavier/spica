"""The play log.

History records what played, not what was chosen: Icecast names the track that is
on air, so a change there is a real play. Both the background loop and the page use
this, because the page polls every few seconds and would otherwise show yesterday's
reason under today's song until the loop caught up.
"""

from __future__ import annotations

import time

from . import config, ledger, queue, store

_played: dict[str, str] = {}
_played_at: dict[str, float] = {}

# A track cannot legitimately come round again inside this window, so the same
# title still on air is the poll loop, not a second play. This is much longer than
# queue.DUPE_ANNOUNCE_S: that one only collapses a restart's re-announce, while a
# normal track can stay on air for several minutes.
REPEAT_WINDOW_S = 15 * 60


def _stamp(row: dict) -> float:
    return ledger.parse_stamp(row.get("t")) or 0.0


def _key(artist: str, title: str) -> str:
    return store.norm(f"{artist} {title}")


def note(st: config.Station, raw_title: str) -> None:
    """Record a play if the track on air is not the one already recorded."""
    raw = (raw_title or "").strip()
    if not raw:
        return

    artist, song = queue.split_title(raw)
    key = _key(artist, song)
    now = time.time()
    # This process already noted the same airing and it is still inside the window.
    if _played.get(st.id) == key and now - _played_at.get(st.id, 0.0) < REPEAT_WINDOW_S:
        return
    # The in-memory key can be missing or stale after a restart, or while an old
    # process is shutting down. Ask the ledger before writing a second copy.
    last = queue.recent(st, 1)
    if last:
        entry = last[-1]
        if _key(entry.get("artist", ""), entry.get("title", "")) == key:
            at = _stamp(entry)
            if at and now - at < REPEAT_WINDOW_S:
                _played[st.id] = key
                _played_at[st.id] = at
                return
    _played[st.id] = key
    _played_at[st.id] = now

    row = store.find_track(artist, song or raw)
    why = ledger.reason_for(st, song or raw)
    queue.record(
        st,
        row["id"] if row else "",
        (row["title"] if row else song) or raw,
        (row["artist"] if row else artist),
        "dj" if why else "fallback_rotation",
        why,
    )
