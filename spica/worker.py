"""The worker: runs one queued job at a time, in its own container.

Nothing heavy happens in the picker — it only writes a row into `jobs`. This
process polls that table, claims a job, and reports progress back, so the API
stays responsive and the worker can be capped or stopped on its own.

The job is resumable: it recomputes what is still missing when it starts, so a
restart continues rather than redoing the work.

Kinds: refresh (re-index, rewrite the pools), genre_tags (Last.fm tags for artists
with no label), musicbrainz (genres per artist), popularity (ListenBrainz, then
Last.fm, play counts), loudness (measure tracks whose files carry no ReplayGain
tag), and analysis (the optional audio backfill). While idle, one tick every
REFRESH_CHECK_S notices a changed library and queues the follow-ups the new gaps
call for.
"""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path

from . import bootstrap
from . import genres as genre_lib
from . import config, enrich, genres, jobs, loudness, navidrome, pool, queue, subsonic

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
        """SELECT id, COALESCE(NULLIF(album_artist, ''), artist) AS artist
           FROM tracks
           WHERE COALESCE(NULLIF(album_artist, ''), artist) != '' AND NOT EXISTS
                 (SELECT 1 FROM track_genres g
                   WHERE g.track_id = tracks.id AND g.source = 'external')"""
    ):
        by_artist.setdefault(row["artist"], []).append(row["id"])
    # Only names MusicBrainz itself calls genres survive; a provider tag that is not
    # in that vocabulary is somebody's mood, nationality or file-manager debris.
    vocabulary = enrich.mb_genre_vocabulary()
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
            if genres.fold(tag) not in vocabulary:
                continue
            label = genres.canonical(tag, operator=False)
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
        """SELECT t.id, COALESCE(NULLIF(t.album_artist, ''), t.artist) AS artist,
                  t.title, t.mbz_recording
           FROM tracks t
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


def _artists_without_mb_genres(conn) -> list[sqlite3.Row]:
    """Artists to ask MusicBrainz about, most records first.

    A row in `mb_artist_genres` is a *check*, not an answer: `genres = '[]'` says
    MusicBrainz had nothing for that artist. Counting those as done is what stops a name
    the database cannot answer for from being asked again on every refresh — the job used
    to run for eight minutes on every import, forty names at a time, finding one or two.
    The TTL brings a miss back in case the artist was edited upstream.
    """
    cutoff = int(time.time()) - config.MB_GENRES_TTL_S
    fresh = {
        row["artist_key"]
        for row in conn.execute(
            "SELECT artist_key FROM mb_artist_genres WHERE fetched_at >= ?", (cutoff,)
        )
    }
    rows = conn.execute(
        "SELECT COALESCE(NULLIF(album_artist, ''), artist) AS artist, COUNT(*) c"
        " FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) != ''"
        " GROUP BY artist ORDER BY c DESC"
    ).fetchall()
    return [row for row in rows if genre_lib.fold(row["artist"]) not in fresh]


def missing_musicbrainz(conn) -> int:
    """Artists with no MusicBrainz genres cached yet, or a cache entry gone stale."""
    return len(_artists_without_mb_genres(conn))


def missing_popularity(conn) -> int:
    """Tracks the stations could lean on but have no figure for yet."""
    return conn.execute(
        """SELECT count(*) FROM tracks t
           WHERE NOT EXISTS (SELECT 1 FROM track_popularity p WHERE p.track_id = t.id)"""
    ).fetchone()[0]


def run_musicbrainz(conn, job_id: int, log=print) -> dict:
    """Cache MusicBrainz genres for every artist that does not have them yet."""
    todo = _artists_without_mb_genres(conn)
    # Album titles are how a shared name gets settled: the right artist owns them.
    albums: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT DISTINCT COALESCE(NULLIF(album_artist, ''), artist) AS artist, album"
        " FROM tracks WHERE album != ''"
    ):
        albums.setdefault(row["artist"], []).append(row["album"])
    total = len(todo)
    jobs.progress(conn, job_id, 0, total)
    found = 0
    for i, row in enumerate(todo, 1):
        artist = row["artist"]
        # Navidrome already knows which artist this is; the name search is what failed on
        # the hyphenated spellings and the shared names. Ask by id when we have it.
        info = enrich.mb_artist_genres(artist, albums.get(artist, []),
                                       mbid=enrich.cached_mbid(conn, artist))
        if info is not None:
            # Written whether or not there were genres: an empty answer is a result, and
            # leaving no trace is what made this job re-ask the same names for ever.
            import json as _json

            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO mb_artist_genres (artist_key, mbid, genres, fetched_at)"
                    " VALUES (?,?,?,?)",
                    (
                        genre_lib.fold(artist),
                        info.get("mbid") or "",
                        _json.dumps(info.get("genres") or []),
                        int(time.time()),
                    ),
                )
            if info.get("genres"):
                found += 1
        jobs.progress(conn, job_id, i, total)
        if i % 20 == 0 or i == total:
            log(f"musicbrainz {i}/{total} ({found} with genres)")
    return {"artists": total, "found": found}


def run_similarity(conn, job_id: int, log=print) -> dict:
    """Cache the scored neighbourhood of every artist on the index.

    Last.fm is the only provider that says *how* similar, and the shortlist needs the
    number: "the three nearest, none below 0.15" is a cut a bare name list cannot make.
    Re-running costs nothing for artists already cached.
    """
    rows = conn.execute(
        "SELECT DISTINCT album_artist AS name FROM tracks WHERE album_artist != '' ORDER BY 1"
    ).fetchall()
    total = len(rows)
    jobs.progress(conn, job_id, 0, total)
    if not total:
        return {"artists": 0, "edges": 0}
    edges = 0
    for i, row in enumerate(rows, 1):
        pairs = enrich.similarity_for(conn, row["name"])
        edges += len(pairs)
        jobs.progress(conn, job_id, i, total)
        if i % 20 == 0 or i == total:
            log(f"similarity {i}/{total} artists ({edges} edges known)")
    return {"artists": total, "edges": edges}


# ------------------------------------------------------- audio analysis (optional)
#
# The models live in the child processes, one set each, created after the fork: an
# ONNX session is not shared safely across processes, and loading it once per worker
# rather than once per track is the difference between minutes and hours.

_ANALYZER = None


def _analysis_init(models_dir: str, threads: int) -> None:
    global _ANALYZER
    from .analysis import Analyzer

    _ANALYZER = Analyzer(Path(models_dir), threads=threads)


def _fetch_audio(track_id: str, target: Path) -> Path:
    """The bytes to analyse: the original by default, a small transcode if asked."""
    if config.ANALYSIS_TRANSCODE:
        return subsonic.download_transcoded(track_id, target, bitrate=config.ANALYSIS_BITRATE)
    return subsonic.download(track_id, target)


def _analysis_task(track_id: str) -> tuple[str, dict]:
    """One track in a worker process: fetch the audio, analyse it, hand it back."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "track"
        _fetch_audio(track_id, path)
        return track_id, _ANALYZER.analyze(path)


def run_analysis(conn, job_id: int, log=print) -> dict:
    """Analyse a slice of the library: embeddings, styles, moods, tempo, key, loudness.

    A slice, not the whole backlog. This job can run for hours and the other
    follow-ups should not wait behind it: `main` requeues the leftover slice after
    this one finishes, and because a claim takes the oldest queued job, a backfill
    interleaves with popularity and MusicBrainz instead of starving them.
    """
    from . import analysis

    if not config.ANALYSIS:
        return {"tracks": 0, "note": "audio analysis is off (set SPICA_ANALYSIS=1)"}
    if not analysis.installed():
        return {"tracks": 0,
                "note": "this image was built without the analysis extra; use the -analysis image"}
    store = analysis.connect(create=True)
    analysis.ensure_models(config.ANALYSIS_MODELS_DIR, log=log)

    rows = analysis.todo(conn, store, config.ANALYSIS_SLICE)
    total = len(rows)
    jobs.progress(conn, job_id, 0, total)
    if not total:
        written = analysis.refresh_artists(conn, store)
        return {"tracks": 0, "artists": written, "note": "every track is analyzed"}

    tasks = [(row["id"], row["album_artist"] or row["artist"] or "") for row in rows]
    workers = max(1, config.ANALYSIS_WORKERS)
    done = failed = 0
    errors: list[str] = []
    started = time.time()

    def take(track_id: str, artist: str, features: dict) -> None:
        nonlocal done
        analysis.save(store, track_id, navidrome.name_key(artist), features)
        done += 1

    def fetch_and_analyze(analyzer, track_id: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "track"
            _fetch_audio(track_id, path)
            return analyzer.analyze(path)

    if workers > 1 and "fork" in multiprocessing.get_all_start_methods():
        context = multiprocessing.get_context("fork")
        with ProcessPoolExecutor(
            max_workers=workers,
            mp_context=context,
            initializer=_analysis_init,
            initargs=(str(config.ANALYSIS_MODELS_DIR), max(1, config.ANALYSIS_THREADS)),
        ) as pool:
            futures = {pool.submit(_analysis_task, track_id): (track_id, artist)
                       for track_id, artist in tasks}
            for i, future in enumerate(as_completed(futures), 1):
                track_id, artist = futures[future]
                try:
                    _id, features = future.result()
                    take(track_id, artist, features)
                except Exception as exc:
                    failed += 1
                    errors.append(str(exc))
                    log(f"analysis: {artist or track_id}: {exc}")
                jobs.progress(conn, job_id, i, total)
    else:
        from .analysis import Analyzer

        analyzer = Analyzer(config.ANALYSIS_MODELS_DIR, threads=min(4, os.cpu_count() or 1))
        for i, (track_id, artist) in enumerate(tasks, 1):
            try:
                take(track_id, artist, fetch_and_analyze(analyzer, track_id))
            except Exception as exc:
                failed += 1
                errors.append(str(exc))
                log(f"analysis: {artist or track_id}: {exc}")
            jobs.progress(conn, job_id, i, total)

    if done:
        analysis.refresh_artists(conn, store)
    if total and not done:
        # The per-track errors are in the log; the first one in the summary is what
        # makes "the whole slice failed" diagnosable from the admin page.
        detail = errors[0] if errors else "no track could be analyzed"
        raise RuntimeError(f"no track could be analyzed ({failed} failed): {detail}")
    return {"tracks": done, "failed": failed, "remaining": analysis.backlog(conn, store),
            "seconds": int(time.time() - started)}


def run_loudness(conn, job_id: int, log=print) -> dict:
    """Measure a slice of the tracks whose files carry no ReplayGain tag.

    A slice, for the same reason as analysis: the first backfill is hours of CPU
    and the other follow-ups should get a turn instead of waiting behind it. A
    measurement is kept in its own store so a rebuild of the index does not wipe
    it, and an attempted track is not tried again — a file that cannot be decoded
    should not make every run start over.
    """
    if not config.LOUDNESS:
        return {"tracks": 0, "note": "loudness is off (set SPICA_LOUDNESS=1)"}
    if not loudness.measure_available():
        return {"tracks": 0,
                "note": "this worker image has no ffmpeg; run the -worker image"}

    store = loudness.connect(create=True)
    rows = loudness.pending(conn, config.LOUDNESS_SLICE)
    total = len(rows)
    jobs.progress(conn, job_id, 0, total)
    if not total:
        store.close()
        return {"tracks": 0, "note": "every playable track already has a level"}

    # Mint the URLs here, on one thread, so the salted token is not built by
    # several ffmpeg workers at once.
    tasks = [(row["id"], subsonic.stream_url(row["id"])) for row in rows]
    workers = max(1, config.LOUDNESS_WORKERS)
    done = failed = 0
    errors: list[str] = []
    started = time.time()

    def measure_one(track_id: str, url: str) -> tuple[str, tuple]:
        return track_id, loudness.measure(url)

    def take(track_id: str, result: tuple) -> None:
        nonlocal done
        lufs, peak = result
        loudness.save(store, track_id, lufs, peak)
        done += 1

    def fail(track_id: str, exc: Exception) -> None:
        nonlocal failed
        failed += 1
        errors.append(str(exc))
        loudness.save(store, track_id, None, None, str(exc))
        log(f"loudness: {track_id}: {exc}")

    if workers > 1:
        # Not named `pool`: that is the module this function calls at the end.
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(measure_one, track_id, url): track_id
                       for track_id, url in tasks}
            for i, future in enumerate(as_completed(futures), 1):
                track_id = futures[future]
                try:
                    _id, result = future.result()
                    take(track_id, result)
                except Exception as exc:
                    fail(track_id, exc)
                jobs.progress(conn, job_id, i, total)
    else:
        for i, (track_id, url) in enumerate(tasks, 1):
            try:
                _id, result = measure_one(track_id, url)
                take(track_id, result)
            except Exception as exc:
                fail(track_id, exc)
            jobs.progress(conn, job_id, i, total)

    store.close()
    if total and not done:
        # The per-track errors are in the log; the first one in the summary is what
        # makes "the whole slice failed" diagnosable from the admin page.
        detail = errors[0] if errors else "no track could be measured"
        raise RuntimeError(f"no track could be measured ({failed} failed): {detail}")
    remaining = loudness.backlog(conn)
    if done and not remaining:
        # The pool is written once, at rebuild time. Now that the last untagged
        # tracks have a level, rewrite it so the standby rotation is levelled too.
        try:
            stations = config.load_stations()
            counts = pool.write_pools(conn, stations, log=log)
            pool.resume_after_last_played(
                stations, lambda st: (queue.recent(st, 1) or [{}])[-1], log=log
            )
            log(f"loudness: pools rewritten {counts}")
        except Exception as exc:  # a pool rewrite must not fail the measurement
            log(f"loudness: pools not rewritten: {exc}")
    return {"tracks": done, "failed": failed, "remaining": remaining,
            "seconds": int(time.time() - started)}


def _analysis_due(conn) -> bool:
    """The optional backfill has work and nothing is pending for it.

    Deliberately not one of FOLLOWUPS: that rule fires on a backlog *growing*, and
    an analysis backlog shrinking is progress, so it would never start. A recent
    failure waits an hour, like any other job.
    """
    from . import analysis

    if not config.ANALYSIS or not analysis.installed():
        return False
    if conn.execute(
        "SELECT 1 FROM jobs WHERE kind = 'analysis' AND status IN ('queued','running')"
    ).fetchone():
        return False
    last = jobs.last(conn, "analysis")
    if last is not None and last["status"] == "failed":
        if time.time() - float(last["finished_at"] or 0) < 3600:
            return False
    try:
        return analysis.backlog(conn, analysis.connect(readonly=True)) > 0
    except Exception as exc:
        print(f"analysis: could not count the backlog: {exc}", flush=True)
        return False


def _loudness_due(conn) -> bool:
    """There are untagged tracks to measure and a worker that can measure them.

    Same shape as the analysis rule: a shrinking backlog must not need to grow
    before it starts, and a recent failure waits an hour. Off when the operator
    has asked for tags only, or when the image has no ffmpeg.
    """
    if not (config.LOUDNESS and config.LOUDNESS_MEASURE and loudness.measure_available()):
        return False
    if conn.execute(
        "SELECT 1 FROM jobs WHERE kind = 'loudness' AND status IN ('queued','running')"
    ).fetchone():
        return False
    last = jobs.last(conn, "loudness")
    if last is not None and last["status"] == "failed":
        if time.time() - float(last["finished_at"] or 0) < 3600:
            return False
    try:
        return loudness.backlog(conn) > 0
    except Exception as exc:
        print(f"loudness: could not count the backlog: {exc}", flush=True)
        return False


def missing_similarity(conn) -> int:
    """Artists with nothing scored yet. Zero without a Last.fm key — there is nothing
    to ask, and a job queued for it would retry forever."""
    if not config.provider_settings()["lastfm_key"]:
        return 0
    row = conn.execute(
        """SELECT count(*) FROM (
             SELECT DISTINCT album_artist AS name FROM tracks WHERE album_artist != ''
           ) a
           WHERE NOT EXISTS (SELECT 1 FROM artist_similarity s WHERE s.name = a.name)"""
    ).fetchone()
    return int(row[0] or 0)


def stale_reason(conn) -> str:
    """Why the index should be rebuilt, or "" when it is current."""
    # Raises when Navidrome is unreachable; the loop logs it and tries again on the
    # next tick, where returning "" here used to mean "the index is current".
    status = subsonic.scan_status()
    if status.get("scanning"):
        return ""  # never index a library that is mid-scan
    have = conn.execute("SELECT count(*) FROM tracks").fetchone()[0]
    theirs = int(status.get("count") or 0)
    if theirs and theirs != have:
        return f"library has {theirs} tracks, index has {have}"
    return ""


HANDLERS = {"genre_tags": run_genre_tags, "refresh": run_refresh, "popularity": run_popularity, "musicbrainz": run_musicbrainz, "similarity": run_similarity, "analysis": run_analysis, "loudness": run_loudness}

# The follow-ups worth queueing when the index changes, in the order they matter:
# MusicBrainz decides the genre for anything without a file tag, so it should be
# in place before the pools are judged again.
FOLLOWUPS = (
    ("musicbrainz", missing_musicbrainz, "artist(s) without musicbrainz genres"),
    ("similarity", missing_similarity, "artist(s) without scored neighbours"),
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
    if _analysis_due(conn):
        print("audio analysis backlog — queueing", flush=True)
        jobs.create(conn, "analysis")
        return True
    if _loudness_due(conn):
        print("loudness backlog — queueing", flush=True)
        jobs.create(conn, "loudness")
        return True
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
            # The rest of the slice goes to the back of the queue, so other job kinds
            # queued in between get a turn instead of waiting behind the whole backfill.
            if kind in ("analysis", "loudness") and (result or {}).get("remaining"):
                jobs.create(conn, kind)
            print(f"job {job_id} {kind}: done {result}", flush=True)
        except Exception as exc:  # a bad job must not kill the worker
            jobs.fail(conn, job_id, str(exc))
            print(f"job {job_id} {kind}: FAILED {exc}", flush=True)
        # The backlog just moved; let the next timer tick judge it.
        last_check = 0.0


if __name__ == "__main__":
    sys.exit(main() or 0)
