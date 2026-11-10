"""The worker: runs one queued job at a time, in its own container.

Nothing heavy happens in the picker — it only writes a row into `jobs`. This
process polls that table, claims a job, and reports progress back, so the API
stays responsive and the worker can be capped or stopped on its own.

The job is resumable: it recomputes what is still missing when it starts, so a
restart continues rather than redoing the work.

    genre_tags  Last.fm artist tags for artists that have no label at all, written
                to `track_genres` with source='external'. Cheap (one call per
                artist, cached forever).
"""

from __future__ import annotations

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


def main() -> int:
    bootstrap.ensure(verbose=False)
    config.ensure_dirs()
    conn = _db()
    jobs.ensure_schema(conn)
    # A job marked running belongs to a process that no longer exists: this one has
    # just started. Leaving it there blocks every future job of that kind.
    with conn:
        stale = conn.execute(
            "UPDATE jobs SET status = 'failed', message = 'interrupted by a restart'"
            " WHERE status = 'running'"
        ).rowcount
    if stale:
        print(f"{stale} interrupted job(s) marked failed", flush=True)
    print(f"worker ready — polling for jobs every {POLL_SECONDS}s", flush=True)
    last_check = 0.0
    # The most gaps we have already asked for: a track with no data anywhere would
    # otherwise be retried forever, and only *new* gaps should wake the job up.
    asked_for = 0
    asked_mb = 0
    while True:
        job = jobs.claim(conn)
        if not job:
            # Nothing queued: the only thing left to do on its own is notice that
            # the library changed since the last rebuild.
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
                # New albums come in without a popularity figure; the job is batched
                # and cheap, so pick them up here instead of making it a chore.

            # MusicBrainz first: it decides the genre for anything without a file
            # tag, so it should be in place before the next index rebuild.
            try:
                mbm = missing_musicbrainz(conn)
            except Exception:
                mbm = 0
            if mbm > asked_mb:
                asked_mb = mbm
                print(f"{mbm} artist(s) without musicbrainz genres — queueing", flush=True)
                jobs.create(conn, "musicbrainz")
                continue

            # Independent of the library check: new albums arrive without a
            # popularity figure and the job is cheap, so fetch them as they appear.
            try:
                missing = missing_popularity(conn)
            except Exception:
                missing = 0
            if missing > asked_for:
                asked_for = missing
                print(f"{missing} track(s) without popularity — queueing", flush=True)
                jobs.create(conn, "popularity")
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


if __name__ == "__main__":
    sys.exit(main() or 0)
