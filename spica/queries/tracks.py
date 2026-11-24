"""Reads over the tracks table.

Every statement that touches `tracks` for a read belongs here. Callers ask a
question ("this id", "this artist", "the whole index") and get rows back; they do
not compose SQL.
"""

from __future__ import annotations

import sqlite3

from .. import db, navidrome

# Every word must land in one of these columns; a name spread over artist and
# title ("Beach House Space Song") still matches.
SEARCH_COLUMNS = (
    "title LIKE ? OR artist LIKE ? OR album LIKE ? OR album_artist LIKE ? "
    "OR album_artist_key LIKE ?"
)


def track_by_id(
    conn: sqlite3.Connection, track_id: str, columns: str = "*"
) -> sqlite3.Row | None:
    if not track_id:
        return None
    return conn.execute(
        f"SELECT {columns} FROM tracks WHERE id = ?", (track_id,)
    ).fetchone()


def tracks_by_ids(
    conn: sqlite3.Connection, ids, columns: str = "*"
) -> list[sqlite3.Row]:
    ids = [i for i in ids if i]
    if not ids:
        return []
    where, args = db.where_in("id", ids)
    return conn.execute(f"SELECT {columns} FROM tracks WHERE {where}", args).fetchall()


def playable_ids(conn: sqlite3.Connection) -> set[str]:
    """Ids of tracks a station can actually fetch."""
    return {row["id"] for row in conn.execute(f"SELECT id FROM tracks WHERE {db.PLAYABLE}")}


def library_size(conn: sqlite3.Connection, *, playable: bool = True) -> int:
    where = f" WHERE {db.PLAYABLE}" if playable else ""
    return int(conn.execute(f"SELECT count(*) FROM tracks{where}").fetchone()[0])


def untagged_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) c FROM tracks t WHERE NOT EXISTS"
            " (SELECT 1 FROM track_genres g WHERE g.track_id = t.id)"
        ).fetchone()["c"]
    )


def album_name(conn: sqlite3.Connection, album_id: str) -> str:
    if not album_id:
        return ""
    row = conn.execute(
        "SELECT album FROM tracks WHERE album_id = ? LIMIT 1", (album_id,)
    ).fetchone()
    return (row["album"] if row else "") or ""


def album_covers(conn: sqlite3.Connection, album_ids) -> list[sqlite3.Row]:
    """Cover ids for many albums, batched so an `IN` stays bounded."""
    ids = [album_id for album_id in album_ids if album_id]
    out: list[sqlite3.Row] = []
    for start in range(0, len(ids), 400):
        where, args = db.where_in("album_id", ids[start : start + 400])
        out += conn.execute(
            f"SELECT album_id, cover_id FROM album_art WHERE {where}", args
        ).fetchall()
    return out


def album_cover(conn: sqlite3.Connection, album_id: str) -> str | None:
    if not album_id:
        return None
    row = conn.execute(
        "SELECT cover_id FROM album_art WHERE album_id = ?", (album_id,)
    ).fetchone()
    return row["cover_id"] if row else None


def all_for_index(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The columns `store.tracks()` needs to build its in-memory title index."""
    return conn.execute("SELECT id, title, artist, album_id FROM tracks").fetchall()


# The columns a picker row needs. Hot queries select these instead of `*`; the
# full-row accessors keep `*`.
ROW_COLUMNS = (
    "id, uri, title, artist, album, album_id, track_no, year, genre, duration,"
    " mbz_album, mbz_recording, album_artist"
)


def playable_tracks(
    conn: sqlite3.Connection, columns: str = "*"
) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT {columns} FROM tracks WHERE {db.PLAYABLE}"
    ).fetchall()


def record_artists(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every track's id and record artist, playable or not."""
    return conn.execute(
        f"SELECT id, {db.artist_expr()} AS artist FROM tracks"
    ).fetchall()


def record_artist_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Artists with at least one track, most tracks first."""
    artist = db.artist_expr()
    return conn.execute(
        f"SELECT {artist} AS artist, COUNT(*) c FROM tracks"
        f" WHERE {artist} != '' GROUP BY artist ORDER BY c DESC, artist"
    ).fetchall()


def record_artist_albums(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    artist = db.artist_expr()
    return conn.execute(
        f"SELECT DISTINCT {artist} AS artist, album FROM tracks WHERE album != ''"
    ).fetchall()


def album_artists(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Distinct record artists, for the similarity job."""
    artist = db.artist_expr()
    return conn.execute(
        f"SELECT DISTINCT {artist} AS name FROM tracks"
        f" WHERE {artist} != '' ORDER BY 1"
    ).fetchall()


def album_artist_names(conn: sqlite3.Connection) -> list[str]:
    """Distinct non-empty record artists, for fuzzy artist matching."""
    artist = db.artist_expr()
    return [
        row["name"]
        for row in conn.execute(
            f"SELECT {artist} AS name FROM tracks"
            f" WHERE {artist} != '' GROUP BY 1"
        )
    ]


def tracks_for_album_artist(
    conn: sqlite3.Connection, name: str, columns: str = "*"
) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT {columns} FROM tracks WHERE album_artist = ?", (name,)
    ).fetchall()


def track_ids_for_artist(conn: sqlite3.Connection, artist_key: str) -> list[str]:
    return [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM tracks WHERE album_artist_key = ?", (artist_key,)
        )
    ]


def tracks_with_album_artist(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, album_artist FROM tracks WHERE album_artist != ''"
    ).fetchall()


def tracks_missing_external_genres(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    artist = db.artist_expr()
    return conn.execute(
        f"""SELECT id, {artist} AS artist
            FROM tracks
            WHERE {artist} != '' AND NOT EXISTS
                  (SELECT 1 FROM track_genres g
                    WHERE g.track_id = tracks.id AND g.source = 'external')"""
    ).fetchall()


def tracks_missing_popularity(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    artist = db.artist_expr("t")
    return conn.execute(
        f"""SELECT t.id, {artist} AS artist, t.title, t.mbz_recording
            FROM tracks t
            WHERE NOT EXISTS (SELECT 1 FROM track_popularity p WHERE p.track_id = t.id)"""
    ).fetchall()


def artists_missing_mbid(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    artist = db.artist_expr("t")
    return conn.execute(
        f"""SELECT {artist} AS artist,
                   MIN(COALESCE(NULLIF(t.album_artist_id, ''), t.artist_id)) AS artist_id
            FROM tracks t
            LEFT JOIN artist_mbids a ON a.name = {artist}
            WHERE a.name IS NULL
              AND {artist} != ''
              AND COALESCE(NULLIF(t.album_artist_id, ''), t.artist_id) != ''
            GROUP BY artist"""
    ).fetchall()


def artists_missing_mbid_with_album(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    artist = db.artist_expr("t")
    return conn.execute(
        f"""SELECT {artist} AS artist, MAX(t.mbz_album) AS album
            FROM tracks t
            LEFT JOIN artist_mbids a ON a.name = {artist}
            WHERE a.name IS NULL
              AND {artist} != ''
              AND t.mbz_album != ''
            GROUP BY artist"""
    ).fetchall()


def tracks_by_artist_keys(
    conn: sqlite3.Connection, keys, *, random_order: bool = False
) -> list[sqlite3.Row]:
    where, args = db.where_in("album_artist_key", keys)
    order = " ORDER BY RANDOM()" if random_order else ""
    return conn.execute(f"SELECT {ROW_COLUMNS} FROM tracks WHERE {where}{order}", args).fetchall()


def dominant_genre(conn: sqlite3.Connection, artist_key: str) -> str:
    row = conn.execute(
        """SELECT genre FROM tracks WHERE album_artist_key = ? AND genre != ''
           GROUP BY genre ORDER BY COUNT(*) DESC LIMIT 1""",
        (artist_key,),
    ).fetchone()
    return row["genre"] if row else ""


def tracks_in_genre_excluding_artist(
    conn: sqlite3.Connection, genre: str, artist_key: str
) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT {ROW_COLUMNS} FROM tracks WHERE genre = ? AND album_artist_key <> ?"
        " ORDER BY RANDOM()",
        (genre, artist_key),
    ).fetchall()


def _search_args(token: str) -> list:
    like = f"%{token}%"
    return [like, like, like, like, f"%{navidrome.name_key(token)}%"]


def search(
    conn: sqlite3.Connection, text: str, tokens: list[str], fetch_limit: int
) -> list[sqlite3.Row]:
    """Tracks matching every word, falling back to the whole string."""
    where, args = [db.PLAYABLE], []
    for token in tokens:
        where.append("(" + SEARCH_COLUMNS + ")")
        args += _search_args(token)
    rows = conn.execute(
        f"SELECT {ROW_COLUMNS} FROM tracks WHERE " + " AND ".join(where)
        + " ORDER BY album_artist, album, track_no LIMIT ?",
        (*args, fetch_limit),
    ).fetchall()
    if not rows and len(tokens) > 1:
        rows = conn.execute(
            f"SELECT {ROW_COLUMNS} FROM tracks WHERE (" + SEARCH_COLUMNS + f") AND {db.PLAYABLE}"
            + " ORDER BY album_artist, album, track_no LIMIT ?",
            (*_search_args(text), fetch_limit),
        ).fetchall()
    return rows


def catalogue(
    conn: sqlite3.Connection, artist: str | None, album: str | None
) -> list[sqlite3.Row] | None:
    """Playable tracks under an artist and/or album; None when neither is given."""
    if not artist and not album:
        return None
    where, args = [db.PLAYABLE], []
    if artist:
        where.append("(album_artist_key = ? OR artist = ? COLLATE NOCASE)")
        args.extend([navidrome.name_key(artist), artist.strip()])
    if album:
        where.append("album LIKE ?")
        args.append(f"%{album.strip()}%")
    return conn.execute(
        f"SELECT {ROW_COLUMNS} FROM tracks WHERE " + " AND ".join(where)
        + f" ORDER BY {db.artist_expr()}, album, track_no",
        args,
    ).fetchall()
