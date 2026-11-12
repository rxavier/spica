"""The worker: runs one queued job at a time, in its own container.

Nothing heavy happens in the picker — it only writes a row into `jobs`. This
process polls that table, claims a job, and reports progress back, so the API
stays responsive and the worker can be capped or stopped on its own.

The job is resumable: it recomputes what is still missing when it starts, so a
restart continues rather than redoing the work.

Kinds: refresh (re-index, rewrite the pools), genre_tags (Last.fm tags for artists
with no label), musicbrainz (genres per artist), popularity (ListenBrainz, then
Last.fm, play counts). While idle, one tick every REFRESH_CHECK_S notices a
changed library and queues the follow-ups the new gaps call for.
"""

from __future__ import annotations

import sqlite3
import sys
import time

from . import bootstrap
from . import genres as genre_lib
from . import config, enrich, genres, jobs, navidrome, pool, queue, subsonic

POLL_SECONDS = 5


def _db():
    return navidrome.connect(config.STATE_DIR / "index.db")


def _artist_labels(conn, artist: str) -> list[str]:
    """Last.fm tags for an artist, cached in `artist_tags` so it is fetched once."""
    cached = [r["tag"] for r in conn.execute("SELECT tag FROM artist_tags WHERE name = ?", (artist,))]
    if cached:
        return cached
    tags = enrich.lfm_tags(artist)
    if not tags:
        return []
    now = int(time.time())
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO artist_tags (name, tag, source, fetched_at) VALUES (?,?,?,?)",
            [(artist, t, "lastfm", now) for t in tags],
        )
    return tags


def run_genre_tags(conn, job_id: int, log=print) -> dict:
    # Group by the primary artist: a "feat." credit is the same act for tagging
    # purposes, and Last.fm has no entry for the combination.
    by_artist: dict[str, list[str]] = {}
    for row in conn.execute(
        """SELECT id, artist FROM tracks
           WHERE artist != '' AND NOT EXISTS
                 (SELECT 1 FROM track_genres g
                   WHERE g.track_id = tracks.id AND g.source = 'external')"""
    ):
        by_artist.setdefault(navidrome.primary_artist(row["artist"]), []).append(row["id"])
    # Only names MusicBrainz itself calls genres survive; a provider tag that is not
    # in that vocabulary is somebody's mood, nationality or file-manager debris.
    vocabulary = enrich.mb_genre_vocabulary()
    if vocabulary:
        log(f"genre_tags: validating against {len(vocabulary)} known genres")
    pending = sorted(by_artist.items(), key=lambda kv: -len(kv[1]))
    total = len(pending)
    jobs.progress(conn, job_id, 0, total)
    if not total:
        return {"artists": 0, "tagged": 0, "note": "every track already has a genre"}

    tagged = 0
    for i, (artist, track_ids) in enumerate(pending, 1):
        labels: list[str] = []
        for tag in _artist_labels(conn, artist):
            if not genres.is_genre_like(tag):
                continue
            if vocabulary and genres.fold(tag) not in vocabulary:
                continue
            label = genres.canonical(tag)
            if label and label.casefold() not in {x.casefold() for x in labels}:
                labels.append(label)
        if labels:
            ids = track_ids
            with conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO track_genres (track_id, label, source) VALUES (?,?, 'external')",
                    [(tid, label) for tid in ids for label in labels[:3]],
                )
            tagged += 1
        if i % 5 == 0 or i == total:
            jobs.progress(conn, job_id, i, total)
            log(f"genre_tags {i}/{total} artists ({tagged} tagged)")
    return {"artists": total, "tagged": tagged}


def run_refresh(conn, job_id: int, log=print) -> dict:
    """Rebuild the projection from the API and rewrite the pools."""
    jobs.progress(conn, job_id, 0, 2)
    log("refresh: indexing the library")
    tracks = navidrome.rebuild_index(conn, log=log)
    jobs.progress(conn, job_id, 1, 2)
    log(f"refresh: {tracks} tracks indexed, rewriting pools")
    stations = config.load_stations()
    counts = pool.write_pools(conn, stations, log=log)
    # Keep whatever is on air where it is: start each pool after the last play.
    pool.resume_after_last_played(stations, lambda st: (queue.recent(st, 1) or [{}])[-1], log=log)
    jobs.progress(conn, job_id, 2, 2)
    return {"tracks": tracks, "pools": counts}


def run_popularity(conn, job_id: int, log=print) -> dict:
    """Fill the popularity table: ListenBrainz in batches, Last.fm for the rest."""
    todo = conn.execute(
        """SELECT t.id, t.artist, t.title, t.mbz_recording FROM tracks t
           WHERE NOT EXISTS (SELECT 1 FROM track_popularity p WHERE p.track_id = t.id)"""
    ).fetchall()
    total = len(todo)
    jobs.progress(conn, job_id, 0, total)
    if not total:
        return {"tracks": 0, "note": "every track already has a popularity figure"}

    done = 0
    stored = 0

    # Tracks with a MusicBrainz id go through ListenBrainz, a hundred at a time.
    by_mbid = [r for r in todo if r["mbz_recording"]]
    for i in range(0, len(by_mbid), 100):
        batch = by_mbid[i : i + 100]
        found = enrich.lb_popularity([r["mbz_recording"] for r in batch])
        rows = [
            (r["id"], found[r["mbz_recording"]]["playcount"], found[r["mbz_recording"]]["users"],
             "listenbrainz", int(time.time()))
            for r in batch
            if r["mbz_recording"] in found
        ]
        if rows:
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO track_popularity"
                    " (track_id, playcount, users, source, fetched_at) VALUES (?,?,?,?,?)",
                    rows,
                )
            stored += len(rows)
        done += len(batch)
        jobs.progress(conn, job_id, done, total)
        log(f"popularity: listenbrainz {done}/{total} ({stored} stored)")

    # Whatever is left has no MBID: ask Last.fm, gently.
    rest = [r for r in todo if not r["mbz_recording"]]
    for i, row in enumerate(rest, 1):
        info = enrich.lfm_track(row["artist"], row["title"])
        if info:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO track_popularity"
                    " (track_id, playcount, users, source, fetched_at) VALUES (?,?,?,?,?)",
                    (row["id"], info["playcount"], info["users"], "lastfm", int(time.time())),
                )
            stored += 1
        if i % 20 == 0 or i == len(rest):
            jobs.progress(conn, job_id, done + i, total)
            log(f"popularity: lastfm {i}/{len(rest)} ({stored} stored)")
        time.sleep(0.35)
    return {"tracks": total, "stored": stored}


def missing_musicbrainz(conn) -> int:
    """Artists with no MusicBrainz genre cached yet."""
    cached = {row["artist_key"] for row in conn.execute("SELECT artist_key FROM mb_artist_genres")}
    artists = {
        row["artist"]
        for row in conn.execute("SELECT DISTINCT artist FROM tracks WHERE artist != ''")
    }
    return sum(1 for artist in artists if genre_lib.fold(artist) not in cached)


def missing_popularity(conn) -> int:
    """Tracks the stations could lean on but have no figure for yet."""
    return conn.execute(
        """SELECT count(*) FROM tracks t
           WHERE NOT EXISTS (SELECT 1 FROM track_popularity p WHERE p.track_id = t.id)"""
    ).fetchone()[0]


def run_musicbrainz(conn, job_id: int, log=print) -> dict:
    """Cache MusicBrainz genres for every artist that does not have them yet."""
    cached = {row["artist_key"] for row in conn.execute("SELECT artist_key FROM mb_artist_genres")}
    rows = conn.execute(
        "SELECT artist, COUNT(*) c FROM tracks WHERE artist != '' GROUP BY artist ORDER BY c DESC"
    ).fetchall()
    todo = [row for row in rows if genre_lib.fold(row["artist"]) not in cached]
    # Album titles are how a shared name gets settled: the right artist owns them.
    albums: dict[str, list[str]] = {}
    for row in conn.execute("SELECT DISTINCT artist, album FROM tracks WHERE album != ''"):
        albums.setdefault(row["artist"], []).append(row["album"])
    total = len(todo)
    jobs.progress(conn, job_id, 0, total)
    found = 0
    for i, row in enumerate(todo, 1):
        artist = row["artist"]
        info = enrich.mb_artist_genres(artist, albums.get(artist, []))
        if info and info["genres"]:
            import json as _json

            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO mb_artist_genres (artist_key, mbid, genres, fetched_at)"
                    " VALUES (?,?,?,?)",
                    (
                        genre_lib.fold(artist),
                        info["mbid"],
                        _json.dumps(info["genres"]),
                        int(time.time()),
                    ),
                )
            found += 1
        jobs.progress(conn, job_id, i, total)
        if i % 20 == 0 or i == total:
            log(f"musicbrainz {i}/{total} ({found} with genres)")
    return {"artists": total, "found": found}


def stale_reason(conn) -> str:
    """Why the index should be rebuilt, or "" when it is current."""
    status = subsonic.scan_status()
    if not status:
        return ""
    if status.get("scanning"):
        return ""  # never index a library that is mid-scan
    have = conn.execute("SELECT count(*) FROM tracks").fetchone()[0]
    theirs = int(status.get("count") or 0)
    if theirs and theirs != have:
        return f"library has {theirs} tracks, index has {have}"
    return ""


HANDLERS = {"genre_tags": run_genre_tags, "refresh": run_refresh, "popularity": run_popularity, "musicbrainz": run_musicbrainz}

# The follow-ups worth queueing when the index changes, in the order they matter:
# MusicBrainz decides the genre for anything without a file tag, so it should be
# in place before the pools are judged again.
FOLLOWUPS = (
    ("musicbrainz", missing_musicbrainz, "artist(s) without musicbrainz genres"),
    ("popularity", missing_popularity, "track(s) without popularity"),
)


def _retry_due(conn: sqlite3.Connection, kind: str, after: float = 3600.0) -> bool:
    """True when the last attempt at this job failed long enough ago to try again."""
    last = jobs.last(conn, kind)
    if last is None or last["status"] != "failed":
        return False
    return time.time() - float(last["finished_at"] or 0) > after


def queue_followups(conn: sqlite3.Connection, seen: dict[str, int]) -> bool:
    """Queue the cheap jobs the index's gaps call for. True if one was queued.

    `seen` is the backlog this process last looked at. A track with no figure on any
    provider never gets one, so the rule is "the backlog grew since last time", not
    "there is a backlog" — otherwise the same unfillable gaps are retried forever.
    """
    for kind, counter, label in FOLLOWUPS:
        try:
            missing = counter(conn)
        except Exception as exc:
            print(f"{kind}: could not count the backlog: {exc}", flush=True)
            continue
        if (missing and missing > seen.get(kind, -1)) or _retry_due(conn, kind):
            seen[kind] = missing
            print(f"{missing} {label} — queueing", flush=True)
            jobs.create(conn, kind)
            return True
        seen[kind] = missing
    return False


def main() -> int:
    bootstrap.ensure(verbose=False)
    config.ensure_dirs()
    conn = _db()
    jobs.ensure_schema(conn)
    # A job marked running belongs to a process that no longer exists: this one has
    # just started. Leaving it there blocks every future job of that kind, and these
    # jobs are resumable by construction — each one recomputes what is still missing
    # when it starts — so it goes back in the queue rather than being called failed.
    with conn:
        stale = conn.execute(
            "UPDATE jobs SET status = 'queued', started_at = NULL WHERE status = 'running'"
        ).rowcount
    if stale:
        print(f"{stale} interrupted job(s) queued again", flush=True)
    print(f"worker ready — polling for jobs every {POLL_SECONDS}s", flush=True)
    last_check = 0.0
    # The backlog this process last saw, per job kind. Seeded from the current
    # index so a restart does not re-ask for gaps no provider can fill; a real
    # change still shows up as growth on the next tick.
    seen: dict[str, int] = {}
    for kind, counter, _ in FOLLOWUPS:
        try:
            seen[kind] = counter(conn)
        except Exception:
            seen[kind] = 0
    while True:
        try:
            job = jobs.claim(conn)
        except sqlite3.Error as exc:
            # A locked database must not take the worker down with it: the job it
            # was about to claim is still queued, and the next pass will get it.
            print(f"could not claim a job: {exc}", flush=True)
            time.sleep(POLL_SECONDS)
            continue
        if not job:
            # Nothing queued. Every REFRESH_CHECK_S, notice whether the library
            # changed and whether the gaps that follow changed with it. Doing this
            # on a timer instead of every poll keeps two full-table scans off the
            # idle path.
            if time.time() - last_check > config.REFRESH_CHECK_S:
                last_check = time.time()
                try:
                    why = stale_reason(conn)
                except Exception as exc:
                    print(f"staleness check failed: {exc}", flush=True)
                    why = ""
                if why:
                    print(f"index stale ({why}) — queueing refresh", flush=True)
                    jobs.create(conn, "refresh")
                    continue
                if queue_followups(conn, seen):
                    continue
            time.sleep(POLL_SECONDS)
            continue
        kind, job_id = job["kind"], job["id"]
        print(f"job {job_id} {kind}: start", flush=True)
        try:
            result = HANDLERS[kind](conn, job_id)
            jobs.finish(conn, job_id, result)
            print(f"job {job_id} {kind}: done {result}", flush=True)
        except Exception as exc:  # a bad job must not kill the worker
            jobs.fail(conn, job_id, str(exc))
            print(f"job {job_id} {kind}: FAILED {exc}", flush=True)
        # The backlog just moved; let the next timer tick judge it.
        last_check = 0.0


if __name__ == "__main__":
    sys.exit(main() or 0)
