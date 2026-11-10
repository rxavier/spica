"""Heuristic rotation: one deterministic, human-ordered m3u per station.

Ordering rules that matter and that an LLM should never be asked to do:
  * stable per station+day, so a station has a consistent shape but is not the
    same list forever,
  * no two tracks from one album adjacent,
  * no artist repeat inside ARTIST_WINDOW tracks,
  * how the set leans is the DJ's brief, not a filter: the order itself is neutral.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

from . import config, navidrome

ARTIST_WINDOW = 12


def _popularity_percentiles(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[str, float]:
    """Each track's standing among that artist's own tracks, 0 = least known.

    Absolute counts only measure how big the artist is, which the genre filter already
    settled; the question here is which of their songs people actually know. Ranks are
    computed inside one provider, because Last.fm and ListenBrainz counts are not on
    the same scale.
    """
    stats = {
        r["track_id"]: (int(r["playcount"] or 0), r["source"] or "")
        for r in conn.execute("SELECT track_id, playcount, source FROM track_popularity")
    }
    by_group: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for row in rows:
        hit = stats.get(row["id"])
        if not hit:
            continue
        by_group.setdefault((row["artist"] or "", hit[1]), []).append((hit[0], row["id"]))
    out: dict[str, float] = {}
    for songs in by_group.values():
        if len(songs) < 2:
            continue
        songs.sort()
        span = len(songs) - 1
        for rank, (_, tid) in enumerate(songs):
            out[tid] = rank / span
    return out


def _score(station: config.Station, row: sqlite3.Row, day: int, percentile: dict[str, float]) -> float:
    h = hashlib.blake2b(f"{station.id}|{day}|{row['id']}".encode(), digest_size=8).hexdigest()
    base = int(h, 16) / 2**64
    if not station.deep_bias:
        return base
    p = percentile.get(row["id"], 0.5)  # no data means no opinion
    # The list plays from the lowest score up, so "deep" has to hand the popular
    # tracks the high scores and let them come round later.
    lean = p if station.deep_bias > 0 else (1.0 - p)
    return base * 0.45 + lean * 0.55


def order(station: config.Station, rows: list[sqlite3.Row], percentile: dict[str, float] | None = None) -> list[sqlite3.Row]:
    day = int(time.time() // 86400)
    scored = sorted(rows, key=lambda r: _score(station, r, day, percentile or {}))
    out: list[sqlite3.Row] = []
    recent_artists: list[str] = []
    recent_albums: list[str] = []
    pool = scored[:]
    while pool:
        for i, row in enumerate(pool):
            if row["album_id"] in recent_albums[-1:]:
                continue
            if recent_artists.count(row["artist"]) and row["artist"] in recent_artists[-ARTIST_WINDOW:]:
                continue
            out.append(row)
            recent_artists.append(row["artist"])
            recent_albums.append(row["album_id"])
            pool.pop(i)
            break
        else:
            # Every remaining candidate is blocked: relax and take the first.
            row = pool.pop(0)
            out.append(row)
            recent_artists.append(row["artist"])
            recent_albums.append(row["album_id"])
    return out


def resume_after_last_played(stations: list[config.Station], last_played, log=print) -> None:
    """Rotate each pool so it starts after the track that played last.

    Liquidsoap restarts a playlist from its first line, and the order is
    deterministic per day, so after a mixer restart you would hear the same
    opening sequence again. Rotating keeps the order intact but moves the head:
    the list is a loop, so nothing is lost, and nothing is shuffling.
    """
    for st in stations:
        try:
            lines = [line for line in st.pool_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        except FileNotFoundError:
            continue
        if len(lines) < 2:
            continue
        last = last_played(st)
        if not last or not last.get("id"):
            continue
        marker = f"id={last['id']}"
        at = next((i for i, line in enumerate(lines) if marker in line), None)
        if at is None:
            continue
        rotated = lines[at + 1 :] + lines[: at + 1]
        if rotated == lines:
            continue
        tmp = st.pool_file.with_suffix(".tmp")
        tmp.write_text("\n".join(rotated) + "\n", encoding="utf-8")
        tmp.replace(st.pool_file)
        log(f"pool {st.id}: resumed after {last.get('artist')} — {last.get('title')}")


def write_pools(conn: sqlite3.Connection, stations: list[config.Station], log=print) -> dict[str, int]:
    counts: dict[str, int] = {}
    for st in stations:
        rows = navidrome.rows_for_station(conn, st)
        ordered = order(st, rows, _popularity_percentiles(conn, rows))
        st.pool_file.parent.mkdir(parents=True, exist_ok=True)
        body_lines: list[str] = []
        seen: set[str] = set()
        for r in ordered:
            uri = r["uri"]
            if uri in seen:  # the same track reached twice
                continue
            seen.add(uri)
            body_lines.append(uri)
        body = "\n".join(body_lines)
        tmp = st.pool_file.with_suffix(".tmp")
        tmp.write_text(body + "\n", encoding="utf-8")
        tmp.replace(st.pool_file)
        counts[st.id] = len(body_lines)
        log(f"pool {st.id}: {len(body_lines)} tracks")
    return counts
