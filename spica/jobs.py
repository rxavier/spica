"""Background jobs: queue, progress, and the "how much does the DJ know" numbers.

Deliberately small. The picker stays responsive because it only ever *writes a
row*; the heavy work runs in the `spica-worker` container, which polls this table.
No broker, no socket, no docker API — one SQLite table in `index.db`, which both
processes already share.

A job is resumable by construction: each kind recomputes what is still missing
when it starts, so a crash or a restart continues instead of redoing the work.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time

from .queries import jobs as job_queries

KINDS = ("genre_tags", "refresh", "popularity", "musicbrainz", "similarity", "analysis", "loudness")

# What the operator calls each job. The raw kind stays visible beside it, because
# that is the string in the logs and in the URLs.
KIND_LABELS = {
    "genre_tags": "Genres from Last.fm",
    "refresh": "Library refresh",
    "popularity": "Popularity",
    "musicbrainz": "MusicBrainz genres",
    "similarity": "Similar artists (scored)",
    "analysis": "Audio analysis",
    "loudness": "Loudness",
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    job_queries.ensure_schema(conn)


def create(conn: sqlite3.Connection, kind: str) -> int:
    """Queue a job. Re-queues instead of stacking duplicates of the same kind."""
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind!r}; expected one of {KINDS}")
    row = job_queries.pending(conn, kind)
    if row:
        return row["id"]
    with conn:
        return job_queries.insert(conn, kind, int(time.time()))


def claim(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Atomically take the oldest queued job, so two workers never share one."""
    with conn:
        return job_queries.claim(conn, int(time.time()))


def progress(conn: sqlite3.Connection, job_id: int, done: int, total: int) -> None:
    with conn:
        job_queries.progress(conn, job_id, done, total)


def finish(conn: sqlite3.Connection, job_id: int, result: dict | None = None) -> None:
    with conn:
        job_queries.finish(conn, job_id, int(time.time()), json.dumps(result or {}))


def fail(conn: sqlite3.Connection, job_id: int, message: str) -> None:
    with conn:
        job_queries.fail(conn, job_id, int(time.time()), str(message)[:500])


def last(conn: sqlite3.Connection, kind: str) -> sqlite3.Row | None:
    """The most recent job of this kind, whatever became of it."""
    return job_queries.last(conn, kind)


def get(conn: sqlite3.Connection, job_id: int) -> dict | None:
    row = job_queries.get(conn, job_id)
    return dict(row) if row else None


def _iso(ts) -> str:
    """An ISO stamp the page can localise, or ""."""
    if not ts:
        return ""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _took(row: dict) -> str:
    """How long the job ran, in the plainest form."""
    start, end = row.get("started_at"), row.get("finished_at")
    if not start:
        return ""
    seconds = max(0, int((end or time.time()) - start))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s" if rest else f"{minutes}m"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h {rest:02d}m"


_WORDS = {"found": "with genres", "artists": "artists", "tracks": "tracks", "albums": "albums"}


def _note(row: dict) -> str:
    """The result line, as a sentence rather than as JSON."""
    raw = (row.get("result") or "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and data:
            parts = []
            for key, value in data.items():
                if key == "pools" and isinstance(value, dict):
                    parts.append(", ".join(f"{k} {v:,}" for k, v in value.items()) + " in pools")
                elif isinstance(value, int):
                    parts.append(f"{value:,} {_WORDS.get(key, key.replace('_', ' '))}")
                else:
                    parts.append(f"{key.replace('_', ' ')} {value}")
            return " · ".join(parts)
        if isinstance(raw, str) and not raw.startswith("{"):
            return raw
    message = (row.get("message") or "").strip()
    if message:
        return message
    if row.get("total"):
        return f"{row['done']:,} of {row['total']:,}"
    return ""


def recent(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = job_queries.recent(conn, limit)
    out = []
    for row in rows:
        item = dict(row)
        item["started_iso"] = _iso(item.get("started_at"))
        item["finished_iso"] = _iso(item.get("finished_at")) or _iso(item.get("created_at"))
        item["took"] = _took(item)
        item["note"] = _note(item)
        item["label"] = KIND_LABELS.get(item.get("kind", ""), item.get("kind", ""))
        out.append(item)
    return out


def coverage(conn: sqlite3.Connection) -> dict:
    """What the DJ actually knows — the numbers the library page acts on."""
    counts = job_queries.coverage_counts(conn)
    total = counts["total"]
    genre_known = counts["genre_known"]
    popularity_known = counts["popularity_known"]
    artists_total = counts["artists_total"]
    similarity_known = counts["similarity_known"]

    def pack(known: int, of: int = 0) -> dict:
        of = of or total
        return {
            "known": known,
            "total": of,
            "remaining": max(0, of - known),
            "percent": math.floor(known * 1000.0 / of) / 10 if of else 0.0,
        }

    return {
        "genres": pack(genre_known),
        "popularity": pack(popularity_known),
        "similarity": pack(similarity_known, artists_total),
    }
