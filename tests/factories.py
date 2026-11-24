"""Shared helpers for seeding the index in tests.

Tests still describe their own rows, but the column list and the INSERT live here,
so adding a column to the schema does not break every fixture.
"""

from __future__ import annotations

TRACK_COLUMNS = (
    "id", "uri", "title", "artist", "album", "album_id",
    "track_no", "year", "genre", "duration", "album_artist", "album_artist_key",
)


def insert_tracks(conn, rows, columns=TRACK_COLUMNS, *, replace: bool = True) -> None:
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    holds = ", ".join("?" for _ in columns)
    conn.executemany(
        f"{verb} INTO tracks ({', '.join(columns)}) VALUES ({holds})",
        rows,
    )


def insert_track(conn, **fields) -> None:
    names = ", ".join(fields)
    holds = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT OR REPLACE INTO tracks ({names}) VALUES ({holds})",
        tuple(fields.values()),
    )
