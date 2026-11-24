"""Who may not be offered a set, for the artist and track candidates alike.

The artist and track candidate builders share one gathering step, so they cannot
disagree about what has just played. The one difference is ``include_picks``, which is
explicit: the artists count the picker's recent choices, the tracks do not.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import mixer, navidrome, queue

# How many plays count as "off the air" when an offer is scoring absence.
ABSENCE = 40


def artist_of(lib, track_id: str, fallback: str = "") -> str:
    """The record's artist for a ledger id, or the ledger's own name when it is gone."""
    row = lib.by_id(track_id) if track_id else None
    if row is not None:
        return row.album_artist or row.artist or ""
    return fallback


def artist_absence(lib, station, look: int = ABSENCE * 6) -> dict[str, int]:
    """For each artist, how many songs ago it last aired (absent from the map = never)."""
    plays = queue.recent(station, look)
    artists = lib.artists_by_ids(str(entry.get("id") or "") for entry in plays)
    since: dict[str, int] = {}
    for ago, entry in enumerate(reversed(plays)):
        name = artists.get(str(entry.get("id") or "")) or str(entry.get("artist") or "")
        key = navidrome.name_key(name)
        if key and key not in since:
            since[key] = ago
    return since


def recent_artists(lib, station, n: int) -> list[str]:
    """The artists behind the last n plays, oldest first."""
    out: list[str] = []
    for entry in queue.recent(station, n):
        name = artist_of(lib, str(entry.get("id") or ""), str(entry.get("artist") or ""))
        if name:
            out.append(name)
    return out


def queued_artists(lib, station, limit: int | None = None) -> list[str]:
    """The artists of the tracks already waiting for the mixer."""
    try:
        waiting = mixer.queued_ids(station)
    except Exception:  # liquidsoap down: the candidates are still worth building
        return []
    if limit is not None:
        waiting = waiting[:limit]
    out: list[str] = []
    for track_id in waiting:
        name = artist_of(lib, track_id)
        if name:
            out.append(name)
    return out


def spread_decades(chosen, candidates, *, key, decade, decades=None,
                   accept=None, replaceable=None):
    """Put two eras in an offer when the candidates can pay for it.

    Replaces one item from `chosen` with the first candidate from a different era,
    returning `(index, previous, replacement)` or None. `accept` can veto a candidate
    (the track candidates exclude ids and artists already used); `replaceable` can veto the slot
    (the artist candidates keep the last record of a label). `decades` is the eras already on the
    offer when it spans more than one list.
    """
    seen = set(decades) if decades is not None else {
        decade(item) for item in chosen if decade(item)
    }
    if len(seen) >= 2:
        return None
    taken = {key(item) for item in chosen}
    for candidate in candidates:
        era = decade(candidate)
        if not era or key(candidate) in taken or era in seen:
            continue
        if accept is not None and not accept(candidate):
            continue
        for i in range(len(chosen) - 1, -1, -1):
            if replaceable is not None and not replaceable(i, chosen):
                continue
            previous = chosen[i]
            chosen[i] = candidate
            return i, previous, candidate
    return None


@dataclass(frozen=True)
class Blocklist:
    keys: set[str]          # folded artist keys that may not be offered
    played: list[str]       # the anchor names, oldest first
    queued: list[str]       # the artists that play after this set


def blocked_artists(lib, station, *, recent: int, queued_limit: int | None = None,
                    include_picks: bool = False) -> Blocklist:
    """Artists that may not be offered: just played, queued, or on the last offer.

    ``include_picks`` adds the picker's own recent choices. The artist candidates set it
    because a quiet station's ledger is mostly fallback airings, so without it an artist
    chosen an hour ago could come straight back; the track candidates leave it off because their
    familiar block is meant to be reachable.
    """
    played = recent_artists(lib, station, recent)
    queued = queued_artists(lib, station, queued_limit)
    names = list(played) + list(queued) + list(queue.last_candidate_artists(station))
    if include_picks:
        names += queue.recent_pick_artists(station, recent + 3)
    keys = {navidrome.name_key(name) for name in names if name}
    return Blocklist(keys=keys, played=played, queued=queued)
