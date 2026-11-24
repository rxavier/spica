"""Reads and writes over loudness.db, plus the index reads the job needs.

Gain maths and the ffmpeg measurement stay in `spica.loudness`.
"""

from __future__ import annotations

import sqlite3

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


def candidates(index_conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    """Untagged playable tracks in album order, optionally only the first `limit`."""
    sql = (
        "SELECT id, uri, replaygain_gain FROM tracks"
        " WHERE uri != '' AND replaygain_gain IS NULL"
        " ORDER BY album_artist, album, track_no, id"
    )
    if limit is not None:
        return index_conn.execute(sql + " LIMIT ?", (limit,)).fetchall()
    return index_conn.execute(sql).fetchall()


def gain_rows(index_conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return index_conn.execute(
        "SELECT id, replaygain_gain FROM tracks WHERE uri != ''"
    ).fetchall()


def playable_ids(index_conn: sqlite3.Connection) -> set[str]:
    return {
        row["id"]
        for row in index_conn.execute("SELECT id FROM tracks WHERE uri != ''")
    }


def tagged_ids(index_conn: sqlite3.Connection) -> set[str]:
    return {
        row["id"]
        for row in index_conn.execute(
            "SELECT id FROM tracks WHERE uri != '' AND replaygain_gain IS NOT NULL"
        )
    }
