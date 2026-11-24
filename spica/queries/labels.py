"""Reads and writes over track_genres, track_raw_genres and track_genre_veto.

The collapsing/folding that turns stored labels into the operator's shelves stays
in `navidrome` (and `genres`); this module only moves rows in and out of SQLite.
"""

from __future__ import annotations

import sqlite3

from .. import db


def by_track(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Every track's stored labels, in one pass."""
    out: dict[str, list[str]] = {}
    for row in conn.execute("SELECT track_id, label FROM track_genres"):
        out.setdefault(row["track_id"], []).append(row["label"])
    return out


def counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """One row per stored label with its track count."""
    return conn.execute(
        "SELECT label, COUNT(*) c FROM track_genres GROUP BY label"
    ).fetchall()


def counts_by_tracks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Label counts, most-carrying first, for the console's pickers."""
    return conn.execute(
        "SELECT label, COUNT(*) c FROM track_genres"
        " GROUP BY label ORDER BY c DESC, label"
    ).fetchall()




def collapsed(conn: sqlite3.Connection) -> dict[str, dict]:
    """Stored labels grouped by the operator's rollup name: tracks and member labels."""
    from .. import genres as genre_lib

    out: dict[str, dict] = {}
    for row in counts(conn):
        name = genre_lib.pretty(genre_lib.canonical(row["label"]))
        entry = out.setdefault(name, {"tracks": 0, "members": {}})
        entry["tracks"] += row["c"]
        entry["members"][row["label"]] = row["c"]
    return out


def members_by_rollup(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Folded rollup name -> every stored label under it, built in one scan."""
    from .. import genres as genre_lib

    out: dict[str, list[str]] = {}
    for label in distinct(conn):
        out.setdefault(genre_lib.fold(genre_lib.canonical(label)), []).append(label)
    return out


def member_labels(conn: sqlite3.Connection, label: str,
                  members: dict[str, list[str]] | None = None) -> list[str]:
    """Every stored label the console shows under `label`."""
    from .. import genres as genre_lib

    members = members if members is not None else members_by_rollup(conn)
    return members.get(genre_lib.fold(genre_lib.canonical(label)), [])


def genre_counts(conn: sqlite3.Connection) -> list[dict]:
    """One row per collapsed label: tracks, albums, sources and nested fine labels."""
    from .. import genres as genre_lib

    rolled = collapsed(conn)
    albums: dict[str, set] = {}
    sources: dict[str, set] = {}
    for row in album_genre_sources(conn):
        name = genre_lib.pretty(genre_lib.canonical(row["label"]))
        albums.setdefault(name, set()).add(row["album_id"])
        sources.setdefault(name, set()).add(row["source"])
    return [
        {
            "label": name,
            "tracks": entry["tracks"],
            "albums": len(albums.get(name, ())),
            "sources": sorted(sources.get(name, ())),
            "members": [
                {"label": m, "tracks": c}
                for m, c in sorted(entry["members"].items(), key=lambda kv: (-kv[1], kv[0]))
                if m != name
            ],
        }
        for name, entry in sorted(rolled.items(), key=lambda kv: (-kv[1]["tracks"], kv[0]))
    ]


def distinct(conn: sqlite3.Connection) -> list[str]:
    return [
        row["label"]
        for row in conn.execute(
            "SELECT label FROM track_genres GROUP BY label ORDER BY label"
        )
    ]


def known(conn: sqlite3.Connection) -> list[str]:
    return [
        row["label"]
        for row in conn.execute("SELECT DISTINCT label FROM track_genres")
    ]


def all_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT track_id, label FROM track_genres").fetchall()


def raw_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT raw AS genre, COUNT(*) c FROM track_raw_genres"
        " GROUP BY raw ORDER BY c DESC"
    ).fetchall()


def source_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT source, COUNT(DISTINCT track_id) tracks, COUNT(*) labels"
        " FROM track_genres GROUP BY source ORDER BY tracks DESC"
    ).fetchall()


def vetoes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT track_id, label FROM track_genre_veto").fetchall()


def album_genre_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """label, album id and source for every stored label, for the console view."""
    return conn.execute(
        "SELECT g.label AS label, t.album_id AS album_id, g.source AS source"
        " FROM track_genres g JOIN tracks t ON t.id = g.track_id"
    ).fetchall()


def explorer_total(conn: sqlite3.Connection, labels: list[str]) -> int:
    where, args = db.where_in("label", labels)
    return int(
        conn.execute(
            f"SELECT COUNT(DISTINCT track_id) c FROM track_genres WHERE {where}",
            args,
        ).fetchone()["c"]
    )


def explorer_albums(conn: sqlite3.Connection, labels: list[str]) -> int:
    where, args = db.where_in("g.label", labels)
    return int(
        conn.execute(
            f"""SELECT COUNT(DISTINCT t.album_id) c FROM track_genres g
                JOIN tracks t ON t.id = g.track_id WHERE {where}""",
            args,
        ).fetchone()["c"]
    )


def explorer_rows(
    conn: sqlite3.Connection, labels: list[str], limit: int
) -> list[sqlite3.Row]:
    where, args = db.where_in("label", labels)
    artist = db.artist_expr("t")
    return conn.execute(
        f"""SELECT t.id AS id, {artist} AS artist,
                   t.album AS album, t.title AS title, t.year AS year
            FROM tracks t
            WHERE t.id IN (SELECT track_id FROM track_genres WHERE {where})
            ORDER BY {artist}, t.album, t.track_no
            LIMIT ?""",
        args + [limit],
    ).fetchall()


def genres_for_tracks(
    conn: sqlite3.Connection, ids: list[str]
) -> list[sqlite3.Row]:
    """Each track's labels with their sources, batched to keep `IN` bounded."""
    out: list[sqlite3.Row] = []
    for start in range(0, len(ids), 400):
        where, args = db.where_in("track_id", ids[start : start + 400])
        out += conn.execute(
            f"""SELECT track_id, label, GROUP_CONCAT(DISTINCT source) AS sources
                FROM track_genres WHERE {where}
                GROUP BY track_id, label""",
            args,
        ).fetchall()
    return out


def insert(conn: sqlite3.Connection, pairs, source: str) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO track_genres (track_id, label, source) VALUES (?,?,?)",
        [(track_id, label, source) for track_id, label in pairs],
    )


def delete(conn: sqlite3.Connection, pairs) -> int:
    return conn.executemany(
        "DELETE FROM track_genres WHERE track_id = ? AND label = ?", pairs
    ).rowcount


def delete_for_tracks(conn: sqlite3.Connection, track_ids, label: str) -> None:
    conn.executemany(
        "DELETE FROM track_genres WHERE track_id = ? AND lower(label) = lower(?)",
        [(track_id, label) for track_id in track_ids],
    )


def veto(conn: sqlite3.Connection, pairs) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO track_genre_veto (track_id, label) VALUES (?,?)",
        pairs,
    )


def delete_non_tag(conn: sqlite3.Connection, track_ids: list[str]) -> None:
    """Drop provider labels for many tracks at once, keeping tags and manual rows."""
    for start in range(0, len(track_ids), 400):
        where, args = db.where_in("track_id", track_ids[start : start + 400])
        conn.execute(
            f"DELETE FROM track_genres WHERE {where}"
            " AND source NOT IN ('tag', 'manual')",
            args,
        )


def count_with_source(
    conn: sqlite3.Connection, source: str, track_ids: list[str]
) -> int:
    """How many of these tracks carry a label from `source`, batched."""
    total = 0
    for start in range(0, len(track_ids), 400):
        where, args = db.where_in("track_id", track_ids[start : start + 400])
        total += int(
            conn.execute(
                f"SELECT COUNT(DISTINCT track_id) c FROM track_genres"
                f" WHERE source = ? AND {where}",
                [source] + args,
            ).fetchone()["c"]
        )
    return total


def relabel(conn: sqlite3.Connection, target: str, source: str) -> None:
    """Move every row spelled `source` onto `target`, then drop the previous spelling."""
    conn.execute(
        "INSERT OR IGNORE INTO track_genres (track_id, label, source)"
        " SELECT track_id, ?, source FROM track_genres WHERE label = ?",
        (target, source),
    )
    conn.execute("DELETE FROM track_genres WHERE label = ?", (source,))
