"""The play log.

History records what played, not what was chosen. The mixer writes one line to the
station's now file when a track starts, and the background loop is the only writer
of the ledger: one line is one airing, so a genuine repeat is kept and a track is
not recorded twice just because a page poll also saw it. The page only reads.
"""

from __future__ import annotations

from . import config, ledger, queue, store

# The last title an Icecast-only station showed us. Used only on the fallback path,
# so a station with no now file records each change once rather than every poll.
_observed: dict[str, str] = {}


def _key(artist: str, title: str) -> str:
    return store.norm(f"{artist} {title}")


def _record(st: config.Station, raw: str) -> None:
    artist, song = queue.split_title(raw)
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


def record(st: config.Station, raw_title: str) -> None:
    """Record one airing: one line from the mixer's now file."""
    raw = (raw_title or "").strip()
    if not raw or "://" in raw:
        return
    _record(st, raw)


def observe(st: config.Station, raw_title: str) -> None:
    """Record a title seen on Icecast, once per change.

    Only for a station without a now file. The first observation is seeded from the
    newest ledger entry, so a restart does not record the track that was already on
    air; after that, a change of title is a new airing.
    """
    raw = (raw_title or "").strip()
    if not raw or "://" in raw:
        return
    artist, song = queue.split_title(raw)
    key = _key(artist, song)
    if st.id not in _observed:
        last = queue.recent(st, 1)
        _observed[st.id] = (
            _key(last[-1].get("artist", ""), last[-1].get("title", "")) if last else ""
        )
    if _observed.get(st.id) == key:
        return
    _observed[st.id] = key
    _record(st, raw)
