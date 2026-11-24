"""Reads and writes over the enrichment and popularity side tables.

These live in index.db but outlive a rebuild: artist MBIDs, similar artists, tags,
scored similarity, per-track popularity and the MusicBrainz genre cache. The
provider clients and their policies stay in `spica.enrich`/`spica.worker`.
"""

from __future__ import annotations

import sqlite3

from .. import db


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS artist_mbids (
            name TEXT PRIMARY KEY,
            mbid TEXT NOT NULL,
            source TEXT NOT NULL,
            fetched_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS similar_artists (
            name TEXT NOT NULL,
            similar TEXT NOT NULL,
            source TEXT NOT NULL,
            fetched_at INTEGER,
            PRIMARY KEY (name, similar)
        );
        CREATE TABLE IF NOT EXISTS artist_tags (
            name TEXT NOT NULL,
            tag TEXT NOT NULL,
            source TEXT NOT NULL,
            fetched_at INTEGER,
            PRIMARY KEY (name, tag)
        );
        -- Scored similarity. `similar_artists` answers "who sounds like this" for the
        -- tools and keeps whatever a provider returned; this one keeps the match, so a
        -- caller can say "the three nearest" and mean it.
        CREATE TABLE IF NOT EXISTS artist_similarity (
            name TEXT NOT NULL,
            similar TEXT NOT NULL,
            match REAL NOT NULL DEFAULT 0,
            source TEXT NOT NULL,
            fetched_at INTEGER,
            PRIMARY KEY (name, similar)
        );
        CREATE INDEX IF NOT EXISTS similar_by_name ON similar_artists(name);
        CREATE INDEX IF NOT EXISTS similarity_by_name ON artist_similarity(name);
        """
    )
    conn.commit()


# ------------------------------------------------------------------ artist MBIDs


def cached_mbid(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT mbid FROM artist_mbids WHERE name = ?", (name,)
    ).fetchone()
    return row["mbid"] if row else None


def remember_mbid(
    conn: sqlite3.Connection, name: str, mbid: str, source: str, fetched_at: int
) -> None:
    conn.execute(
        "INSERT INTO artist_mbids (name, mbid, source, fetched_at) VALUES (?,?,?,?)"
        " ON CONFLICT(name) DO UPDATE SET mbid=excluded.mbid, source=excluded.source,"
        " fetched_at=excluded.fetched_at",
        (name, mbid, source, fetched_at),
    )


# --------------------------------------------------------------- similar artists


def cached_similar(conn: sqlite3.Connection, name: str) -> list[str]:
    return [
        row["similar"]
        for row in conn.execute(
            "SELECT similar FROM similar_artists WHERE name = ?", (name,)
        )
    ]


def remember_similar(
    conn: sqlite3.Connection, name: str, names: list[str], source: str,
    fetched_at: int,
) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO similar_artists (name, similar, source, fetched_at)"
        " VALUES (?,?,?,?)",
        [(name, similar, source, fetched_at) for similar in names],
    )


def cached_scores(conn: sqlite3.Connection, name: str) -> list[tuple[str, float]]:
    return [
        (row["similar"], float(row["match"] or 0.0))
        for row in conn.execute(
            "SELECT similar, match FROM artist_similarity"
            " WHERE name = ? ORDER BY match DESC",
            (name,),
        )
    ]


def remember_scores(
    conn: sqlite3.Connection, name: str, pairs, source: str, fetched_at: int
) -> None:
    conn.executemany(
        "INSERT INTO artist_similarity (name, similar, match, source, fetched_at)"
        " VALUES (?,?,?,?,?) ON CONFLICT(name, similar) DO UPDATE SET"
        " match=excluded.match, source=excluded.source, fetched_at=excluded.fetched_at",
        [(name, similar, float(match), source, fetched_at) for similar, match in pairs],
    )


def similarity_edges(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT name, similar, match FROM artist_similarity"
    ).fetchall()


# --------------------------------------------------------------------- tags


def tags_for(conn: sqlite3.Connection, name: str) -> list[str]:
    return [
        row["tag"]
        for row in conn.execute("SELECT tag FROM artist_tags WHERE name = ?", (name,))
    ]


def remember_tags(
    conn: sqlite3.Connection, name: str, tags: list[str], source: str, fetched_at: int
) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO artist_tags (name, tag, source, fetched_at)"
        " VALUES (?,?,?,?)",
        [(name, tag, source, fetched_at) for tag in tags],
    )


# ------------------------------------------------------- MusicBrainz genre cache


def mb_genre_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT artist_key, genres FROM mb_artist_genres"
    ).fetchall()


def remember_mb_genres(
    conn: sqlite3.Connection, artist_key: str, mbid: str, genres: str, fetched_at: int
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO mb_artist_genres (artist_key, mbid, genres, fetched_at)"
        " VALUES (?,?,?,?)",
        (artist_key, mbid, genres, fetched_at),
    )


def fresh_mb_genre_keys(conn: sqlite3.Connection, cutoff: int) -> set[str]:
    return {
        row["artist_key"]
        for row in conn.execute(
            "SELECT artist_key FROM mb_artist_genres WHERE fetched_at >= ?", (cutoff,)
        )
    }


# ------------------------------------------------------------------ popularity


def popularity(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT track_id, playcount, source FROM track_popularity"
    ).fetchall()


def replace_popularity(conn: sqlite3.Connection, rows) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO track_popularity"
        " (track_id, playcount, users, source, fetched_at) VALUES (?,?,?,?,?)",
        rows,
    )


def missing_popularity(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT count(*) FROM tracks t WHERE NOT EXISTS"
            " (SELECT 1 FROM track_popularity p WHERE p.track_id = t.id)"
        ).fetchone()[0]
    )


def missing_similarity(conn: sqlite3.Connection) -> int:
    artist = db.artist_expr()
    row = conn.execute(
        f"SELECT count(*) FROM ("
        f"  SELECT DISTINCT {artist} AS name FROM tracks WHERE {artist} != ''"
        f") a WHERE NOT EXISTS (SELECT 1 FROM artist_similarity s WHERE s.name = a.name)"
    ).fetchone()
    return int(row[0] or 0)
