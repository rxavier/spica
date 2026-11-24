"""Heuristic rotation: one deterministic, human-ordered m3u per station.

Ordering rules that matter and that an LLM should never be asked to do:
  * reordered on every rebuild, so a station is not the same sequence twice,
  * stable *between* rebuilds, because a rewrite restarts the playlist at its head
    and anything shuffling underneath a listener would be worse than variety,
  * no two tracks from one album adjacent,
  * no artist repeat inside ARTIST_WINDOW tracks,
  * how the set leans is the DJ's brief, not a filter: the order itself is neutral.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time

from . import config, loudness, navidrome, queue, subsonic
from .atomic import atomic_write
from .queries import enrichment as enrich_queries
from .queries import tracks as track_queries

ARTIST_WINDOW = 12


def _popularity_stats(conn: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    """Every stored play count, by track id. One read for the whole pool write."""
    return {
        r["track_id"]: (int(r["playcount"] or 0), r["source"] or "")
        for r in enrich_queries.popularity(conn)
    }


def _popularity_percentiles(conn: sqlite3.Connection, rows: list[sqlite3.Row],
                            stats: dict[str, tuple[int, str]] | None = None) -> dict[str, float]:
    """Each track's standing among that artist's own tracks, 0 = least known.

    Absolute counts only measure how big the artist is, which the genre filter already
    settled; the question here is which of their songs people actually know. Ranks are
    computed inside one provider, because Last.fm and ListenBrainz counts are not on
    the same scale.
    """
    stats = stats if stats is not None else _popularity_stats(conn)
    by_group: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for row in rows:
        hit = stats.get(row["id"])
        if not hit:
            continue
        by_group.setdefault((row["album_artist"] or row["artist"] or "", hit[1]), []).append((hit[0], row["id"]))
    out: dict[str, float] = {}
    for songs in by_group.values():
        if len(songs) < 2:
            continue
        songs.sort()
        span = len(songs) - 1
        for rank, (_, tid) in enumerate(songs):
            out[tid] = rank / span
    return out


def _score(station: config.Station, row: sqlite3.Row, salt: int, percentile: dict[str, float]) -> float:
    h = hashlib.blake2b(f"{station.id}|{salt}|{row['id']}".encode(), digest_size=8).hexdigest()
    base = int(h, 16) / 2**64
    if not station.deep_bias:
        return base
    p = percentile.get(row["id"], 0.5)  # no data means no opinion
    # The list plays from the lowest score up, so "deep" has to hand the popular
    # tracks the high scores and let them come round later.
    lean = p if station.deep_bias > 0 else (1.0 - p)
    return base * 0.45 + lean * 0.55


def order(station: config.Station, rows: list[sqlite3.Row], percentile: dict[str, float] | None = None,
          salt: int | None = None, seed_artists: list[str] | None = None,
          seed_album: str = "") -> list[sqlite3.Row]:
    """A fresh random order for every rebuild.

    The salt is fresh on every rebuild: reproducibility is not wanted here, because a
    rewrite restarts the playlist from its head and a stable order would put the same
    few tracks at the top again. The file on disk is the record of what was chosen.
    """
    salt = secrets.randbits(64) if salt is None else salt
    scored = sorted(rows, key=lambda r: _score(station, r, salt, percentile or {}))
    out: list[sqlite3.Row] = []
    # Seed the spacing rules with what actually aired, so a rebuilt rotation does
    # not open on the artist or album that just finished.
    recent_artists = list(seed_artists or [])[-ARTIST_WINDOW:]
    recent_albums = [seed_album] if seed_album else []
    # A dict keeps the scan order while allowing O(1) removal: popping from a list
    # copied the rest of the pool on every placement.
    pool = dict(enumerate(scored))
    while pool:
        for key, row in pool.items():
            if row["album_id"] in recent_albums[-1:]:
                continue
            who = row["album_artist"] or row["artist"]
            if who in recent_artists[-ARTIST_WINDOW:]:
                continue
            out.append(row)
            recent_artists.append(who)
            recent_albums.append(row["album_id"])
            del pool[key]
            break
        else:
            # Every remaining candidate is blocked: relax and take the first.
            first = next(iter(pool))
            row = pool.pop(first)
            out.append(row)
            recent_artists.append(row["album_artist"] or row["artist"])
            recent_albums.append(row["album_id"])
    return out


def _read_pool(st: config.Station) -> list[str]:
    try:
        return [line for line in st.pool_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    except FileNotFoundError:
        return []


def _index_of(lines: list[str], track_id: str) -> int | None:
    """Where a track sits in the pool. Compares whole ids, not prefixes: Navidrome
    ids vary in length, so `id=abc` was matching `id=abcd` and rotating the wrong way."""
    for i, line in enumerate(lines):
        if subsonic.song_id(line) == track_id:
            return i
    return None


def _rewrite(st: config.Station, lines: list[str]) -> None:
    atomic_write(st.pool_file, "\n".join(lines) + "\n")


def _rotate_after(st: config.Station, track_id: str) -> bool:
    """Move the head of the pool to just past a track. True if anything moved."""
    lines = _read_pool(st)
    if len(lines) < 2:
        return False
    at = _index_of(lines, track_id)
    if at is None:
        return False
    rotated = lines[at + 1:] + lines[:at + 1]
    if rotated == lines:
        return False
    _rewrite(st, rotated)
    return True


def _rotate_to(st: config.Station, track_id: str) -> bool:
    """Move a track to the head of the pool. True if anything moved."""
    lines = _read_pool(st)
    if len(lines) < 2:
        return False
    at = _index_of(lines, track_id)
    if not at:
        return False
    rotated = lines[at:] + lines[:at]
    _rewrite(st, rotated)
    return True


def skip_past(stations: list[config.Station], track_id: str, log=print) -> None:
    """Push a track the DJ just queued to the far end of the pool.

    A rewrite restarts a playlist at its first line, and this one rotates the list so
    that line is the track after the pick — the pool resumes from there and the pick
    itself is a full rotation away instead of turning up nine minutes later as a
    repeat. Nothing is dropped; the order is a loop.
    """
    if not track_id:
        return
    for st in stations:
        if _rotate_after(st, track_id):
            log(f"pool {st.id}: skipped past {track_id}")


def rewind_to(st: config.Station, track_id: str, log=print) -> bool:
    """Put a track back at the head of the pool, after its slot was taken from it.

    A request or a forced pick displaces whatever was waiting. That track never
    aired, so it should not be a rotation away — it should be next.
    """
    if not track_id:
        return False
    if _rotate_to(st, track_id):
        log(f"pool {st.id}: {track_id} is back at the head")
        return True
    return False


def resume_after_last_played(stations: list[config.Station], last_played, log=print) -> None:
    """Rotate each pool so it starts after the track that played last.

    Liquidsoap restarts a playlist from its first line, and the order is
    deterministic per day, so after a mixer restart you would hear the same
    opening sequence again. Rotating keeps the order intact but moves the head:
    the list is a loop, so nothing is lost, and nothing is shuffling.
    """
    for st in stations:
        last = last_played(st)
        if not last or not last.get("id"):
            continue
        if _rotate_after(st, last["id"]):
            log(f"pool {st.id}: resumed after {last.get('artist')} — {last.get('title')}")


def _recent_context(conn: sqlite3.Connection, station: config.Station) -> tuple[set[str], list[str], str]:
    """The recent plays a rebuilt rotation should leave out.

    Newest first. The pool is a static loop, so this is the best moment to apply the
    same avoid window the DJ uses: the tracks are left out of the list, and the first
    placed track may not be by an artist or on an album that just finished.
    """
    entries = list(reversed(queue.recent(station, max(40, ARTIST_WINDOW))))
    avoid = {str(e.get("id") or "") for e in entries[:40] if e.get("id")}
    rows = {
        row["id"]: row
        for row in track_queries.tracks_by_ids(
            conn, [str(e.get("id") or "") for e in entries],
            "id, album_artist, artist, album_id",
        )
    }
    artists: list[str] = []
    album = ""
    for entry in entries:
        row = rows.get(str(entry.get("id") or ""))
        name = (row["album_artist"] or row["artist"]) if row is not None else entry.get("artist") or ""
        if name and len(artists) < ARTIST_WINDOW:
            artists.append(name)
        if not album and row is not None and row["album_id"]:
            album = str(row["album_id"])
    return avoid, artists, album


def write_pools(conn: sqlite3.Connection, stations: list[config.Station], log=print) -> dict[str, int]:
    counts: dict[str, int] = {}
    # These are global tables, not per-station: read them once for the whole write.
    measured = loudness.all_measured()
    stats = _popularity_stats(conn)
    for st in stations:
        rows = navidrome.rows_for_station(conn, st)
        avoid, recent_artists, last_album = _recent_context(conn, st)
        if avoid:
            kept = [r for r in rows if r["id"] not in avoid]
            # A tiny station must still have a rotation; only drop the recent
            # tracks while at least half of it is left.
            if len(kept) >= max(1, len(rows) // 2):
                rows = kept
        ordered = order(
            st, rows, _popularity_percentiles(conn, rows, stats),
            seed_artists=recent_artists, seed_album=last_album,
        )
        st.pool_file.parent.mkdir(parents=True, exist_ok=True)
        body_lines: list[str] = []
        seen: set[str] = set()
        for r in ordered:
            # The gain rides on the line as Liquidsoap's liq_amplify annotation, so
            # the fallback rotation is levelled exactly like a DJ pick.
            uri = loudness.annotate(r["uri"], loudness.for_row(r, measured.get(r["id"])))
            if r["id"] in seen:  # the same track reached twice
                continue
            seen.add(r["id"])
            body_lines.append(uri)
        body = "\n".join(body_lines)
        atomic_write(st.pool_file, body + "\n")
        counts[st.id] = len(body_lines)
        log(f"pool {st.id}: {len(body_lines)} tracks")
    return counts
