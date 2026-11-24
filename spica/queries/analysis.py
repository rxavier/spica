"""Reads and writes over analysis.db (track_analysis + artist_analysis).

Embedding maths and the model stay in `spica.analysis`; this module owns the
statements and the schema.
"""

from __future__ import annotations

import sqlite3

from .. import db
from . import tracks as track_queries

SCHEMA = """
CREATE TABLE IF NOT EXISTS track_analysis (
    track_id       TEXT PRIMARY KEY,
    artist_key     TEXT NOT NULL DEFAULT '',
    duration       REAL,
    bpm            REAL,
    bpm_confidence REAL,
    key            TEXT,
    key_scale      TEXT,
    key_strength   REAL,
    lufs           REAL,
    lra            REAL,
    dyn_complexity REAL,
    danceability   REAL,
    energy         REAL,
    styles         TEXT NOT NULL DEFAULT '[]',
    heads          TEXT NOT NULL DEFAULT '{}',
    vocal          INTEGER,
    vec            BLOB,
    model_version  TEXT NOT NULL DEFAULT '',
    analyzed_at    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS artist_analysis (
    artist_key TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    vec        BLOB,
    tracks     INTEGER NOT NULL DEFAULT 0,
    bpm        REAL,
    energy     REAL,
    styles     TEXT NOT NULL DEFAULT '[]',
    updated_at INTEGER NOT NULL DEFAULT 0
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def insert(conn: sqlite3.Connection, values: tuple) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO track_analysis
           (track_id, artist_key, duration, bpm, bpm_confidence, key, key_scale,
            key_strength, lufs, lra, dyn_complexity, danceability, energy, styles,
            heads, vocal, vec, model_version, analyzed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        values,
    )


def track_ids(conn: sqlite3.Connection, model_version: str) -> set[str]:
    return {
        row["track_id"]
        for row in conn.execute(
            "SELECT track_id FROM track_analysis WHERE model_version = ?",
            (model_version,),
        )
    }


def feature_rows(conn: sqlite3.Connection, model_version: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT track_id, bpm, bpm_confidence, key, key_scale, energy, styles,
                  vocal, lufs, danceability
           FROM track_analysis WHERE model_version = ?""",
        (model_version,),
    ).fetchall()


def vector_rows(conn: sqlite3.Connection, model_version: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT track_id, vec FROM track_analysis"
        " WHERE model_version = ? AND length(vec) > 0",
        (model_version,),
    ).fetchall()


def artist_track_rows(conn: sqlite3.Connection, model_version: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT track_id, vec, bpm, energy, styles FROM track_analysis"
        " WHERE model_version = ?",
        (model_version,),
    ).fetchall()


def clear_artists(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM artist_analysis")


def insert_artist(conn: sqlite3.Connection, values: tuple) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO artist_analysis
           (artist_key, name, vec, tracks, bpm, energy, styles, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        values,
    )


def artist_profile_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT artist_key, name, bpm, energy, styles FROM artist_analysis"
    ).fetchall()


def artist_vector_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT artist_key, vec FROM artist_analysis"
    ).fetchall()


def artist_pool_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT artist_key, name, vec FROM artist_analysis"
    ).fetchall()


def candidate_rows(index_conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return index_conn.execute(
        f"SELECT id, artist, album_artist, uri, fetched_at FROM tracks WHERE {db.PLAYABLE}"
    ).fetchall()


def playable_count(index_conn: sqlite3.Connection) -> int:
    return track_queries.library_size(index_conn)


def playable_ids(index_conn: sqlite3.Connection) -> set[str]:
    return track_queries.playable_ids(index_conn)
