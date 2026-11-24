"""Reads and writes over loudness.db, plus the index reads the job needs.

Gain maths and the ffmpeg measurement stay in `spica.loudness`.
"""

from __future__ import annotations

import sqlite3

from .. import db
from . import tracks as track_queries

SCHEMA = """
CREATE TABLE IF NOT EXISTS track_loudness (
    track_id    TEXT PRIMARY KEY,
    lufs        REAL,
    peak        REAL,
    measured_at INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT ''
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def save(conn: sqlite3.Connection, values: tuple) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO track_loudness"
        " (track_id, lufs, peak, measured_at, error) VALUES (?,?,?,?,?)",
        values,
    )


def measured(conn: sqlite3.Connection, track_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT lufs, peak FROM track_loudness WHERE track_id = ?", (track_id,)
    ).fetchone()


def all_measured(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT track_id, lufs, peak FROM track_loudness WHERE lufs IS NOT NULL"
    ).fetchall()


def done_ids(conn: sqlite3.Connection) -> set[str]:
    return {
        row["track_id"]
        for row in conn.execute("SELECT track_id FROM track_loudness")
    }


def measured_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT track_id, lufs FROM track_loudness").fetchall()


def pending(index_conn: sqlite3.Connection, done_ids, limit: int) -> list[sqlite3.Row]:
    """Untagged playable tracks with no attempt in `done_ids`, in album order."""
    with index_conn:
        index_conn.execute("CREATE TEMP TABLE IF NOT EXISTS _done_ids (id TEXT PRIMARY KEY)")
        index_conn.execute("DELETE FROM _done_ids")
        index_conn.executemany(
            "INSERT OR IGNORE INTO _done_ids (id) VALUES (?)",
            ((track_id,) for track_id in done_ids),
        )
    return index_conn.execute(
        f"SELECT id, uri, replaygain_gain FROM tracks"
        f" WHERE {db.PLAYABLE} AND replaygain_gain IS NULL"
        " AND id NOT IN (SELECT id FROM _done_ids)"
        " ORDER BY album_artist, album, track_no, id"
        " LIMIT ?",
        (limit,),
    ).fetchall()


def gain_rows(index_conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return index_conn.execute(
        f"SELECT id, replaygain_gain FROM tracks WHERE {db.PLAYABLE}"
    ).fetchall()


def playable_ids(index_conn: sqlite3.Connection) -> set[str]:
    return track_queries.playable_ids(index_conn)


def tagged_ids(index_conn: sqlite3.Connection) -> set[str]:
    return {
        row["id"]
        for row in index_conn.execute(
            f"SELECT id FROM tracks WHERE {db.PLAYABLE} AND replaygain_gain IS NOT NULL"
        )
    }
