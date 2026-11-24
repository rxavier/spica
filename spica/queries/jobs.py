"""Reads and writes over the jobs table and the coverage counts it feeds.

The job lifecycle, formatting and follow-up rules stay in `spica.jobs`; this
module owns the statements.
"""

from __future__ import annotations

import sqlite3


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
               id         INTEGER PRIMARY KEY AUTOINCREMENT,
               kind       TEXT NOT NULL,
               status     TEXT NOT NULL DEFAULT 'queued',
               total      INTEGER NOT NULL DEFAULT 0,
               done       INTEGER NOT NULL DEFAULT 0,
               message    TEXT NOT NULL DEFAULT '',
               result     TEXT NOT NULL DEFAULT '',
               created_at INTEGER NOT NULL,
               started_at INTEGER,
               finished_at INTEGER
           )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status)")
    conn.commit()


def pending(conn: sqlite3.Connection, kind: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id FROM jobs WHERE kind = ? AND status IN ('queued','running') LIMIT 1",
        (kind,),
    ).fetchone()


def insert(conn: sqlite3.Connection, kind: str, created_at: int) -> int:
    cur = conn.execute(
        "INSERT INTO jobs (kind, status, created_at) VALUES (?, 'queued', ?)",
        (kind, created_at),
    )
    return int(cur.lastrowid)


def claim(conn: sqlite3.Connection, started_at: int) -> sqlite3.Row | None:
    return conn.execute(
        """UPDATE jobs SET status = 'running', started_at = ?
           WHERE id = (SELECT id FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1)
             AND status = 'queued'
           RETURNING *""",
        (started_at,),
    ).fetchone()


def progress(conn: sqlite3.Connection, job_id: int, done: int, total: int) -> None:
    conn.execute(
        "UPDATE jobs SET done = ?, total = ? WHERE id = ?", (done, total, job_id)
    )


def finish(
    conn: sqlite3.Connection, job_id: int, finished_at: int, result: str
) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'done', finished_at = ?, message = '', result = ?"
        " WHERE id = ?",
        (finished_at, result, job_id),
    )


def fail(
    conn: sqlite3.Connection, job_id: int, finished_at: int, message: str
) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'failed', finished_at = ?, message = ? WHERE id = ?",
        (finished_at, message, job_id),
    )


def last(conn: sqlite3.Connection, kind: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM jobs WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)
    ).fetchone()


def get(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def recent(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def requeue_running(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "UPDATE jobs SET status = 'queued', started_at = NULL WHERE status = 'running'"
    ).rowcount


def coverage_counts(conn: sqlite3.Connection) -> dict:
    """The raw counts behind the library page's coverage bars."""
    total = conn.execute("SELECT COUNT(*) c FROM tracks").fetchone()["c"]
    genre_known = conn.execute(
        "SELECT COUNT(DISTINCT track_id) c FROM track_genres"
    ).fetchone()["c"]
    # Join back to the index: the enrichment tables outlive a rebuild, and a stale
    # row is not a track this library can play.
    popularity_known = conn.execute(
        """SELECT COUNT(DISTINCT p.track_id) c FROM track_popularity p
           JOIN tracks t ON t.id = p.track_id"""
    ).fetchone()["c"]
    artists_total = conn.execute(
        "SELECT COUNT(DISTINCT album_artist) c FROM tracks WHERE album_artist != ''"
    ).fetchone()["c"]
    similarity_known = conn.execute(
        """SELECT COUNT(*) c FROM (
             SELECT DISTINCT s.name FROM artist_similarity s
             WHERE EXISTS (SELECT 1 FROM tracks t WHERE t.album_artist = s.name)
           )"""
    ).fetchone()["c"]
    return {
        "total": total,
        "genre_known": genre_known,
        "popularity_known": popularity_known,
        "artists_total": artists_total,
        "similarity_known": similarity_known,
    }
