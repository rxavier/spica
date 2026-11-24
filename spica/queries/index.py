"""The index.db schema, its migrations, and the rebuild's bulk writes.

`navidrome.connect` opens the file and calls `ensure_schema`; `navidrome.rebuild`
builds the rows and hands them to the `replace_*` functions here.
"""

from __future__ import annotations

import sqlite3

TRACK_COLUMNS = (
    "id,uri,title,artist,album,album_id,track_no,year,genre,duration,fetched_at,"
    "mbz_album,mbz_recording,artist_id,album_artist,album_artist_id,"
    "album_artist_key,replaygain_gain,replaygain_peak"
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    # Indexes built before the API rewrite stored a filesystem path. The track
    # table is a rebuildable projection, so an old shape is dropped rather than
    # migrated: POST /rebuild repopulates it from the API.
    have = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)")}
    if have and "uri" not in have:
        conn.execute("DROP TABLE tracks")
        conn.execute("DROP TABLE IF EXISTS track_genres")
        conn.execute("DROP TABLE IF EXISTS track_raw_genres")
        have = set()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tracks (
            id TEXT PRIMARY KEY,
            uri TEXT NOT NULL,
            title TEXT,
            artist TEXT,
            album TEXT,
            album_id TEXT,
            track_no INTEGER DEFAULT 0,
            year INTEGER DEFAULT 0,
            genre TEXT DEFAULT '',
            duration INTEGER DEFAULT 0,
            fetched_at INTEGER,
            mbz_album TEXT DEFAULT '',
            mbz_recording TEXT DEFAULT '',
            artist_id TEXT DEFAULT '',
            album_artist TEXT DEFAULT '',
            album_artist_id TEXT DEFAULT '',
            album_artist_key TEXT DEFAULT '',
            replaygain_gain REAL,
            replaygain_peak REAL
        )
        """
    )
    # `artist_id` is Navidrome's own handle for the artist, which is what turns a
    # name into a MusicBrainz id without guessing: getArtist answers with the
    # artist's `musicBrainzId`. Older indexes get the column rather than a rebuild,
    # so manual labels and cached provider answers survive the upgrade.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)")}
    if "artist_id" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN artist_id TEXT DEFAULT ''")
    # The record's artist, which is the identity everything groups by: a guest on a
    # track ("L'Impératrice & Fabiana Martone") is a credit, not a different artist.
    if "album_artist" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN album_artist TEXT DEFAULT ''")
    if "album_artist_id" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN album_artist_id TEXT DEFAULT ''")
    if "album_artist_key" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN album_artist_key TEXT DEFAULT ''")
    # ReplayGain arrives with the song on OpenSubsonic servers. Stored raw (the
    # tag's dB offset and linear peak) so a target change needs no re-index.
    if "replaygain_gain" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN replaygain_gain REAL")
    if "replaygain_peak" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN replaygain_peak REAL")
    conn.execute("CREATE INDEX IF NOT EXISTS tracks_genre ON tracks(genre)")
    conn.execute("CREATE INDEX IF NOT EXISTS tracks_year ON tracks(year)")
    conn.execute("CREATE INDEX IF NOT EXISTS tracks_artist ON tracks(artist)")
    conn.execute("CREATE INDEX IF NOT EXISTS tracks_album_artist ON tracks(album_artist)")
    # now_playing() looks a track's album up by album_id on every poll, and the
    # ledger once per row; without this it is a scan of the whole library each time.
    conn.execute("CREATE INDEX IF NOT EXISTS tracks_album_id ON tracks(album_id)")
    # Canonical genre labels, one row per (track, label). `tracks.genre` keeps
    # only the first label for display; every filter walks this table instead,
    # because matching is not SQL-expressible (see genres.py).
    #
    # `source` records where the label came from: "tag" when Navidrome reports it,
    # otherwise "album" or "artist" when inherited from that level, or "external"
    # when a provider supplied it.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS track_genres (
               track_id TEXT NOT NULL,
               label    TEXT NOT NULL,
               source   TEXT NOT NULL DEFAULT 'tag',
               PRIMARY KEY (track_id, label)
           )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS track_genres_label ON track_genres(label)")
    # Raw labels as the library spells them, kept for the merge audit and the
    # genre UI: `track_genres` holds only the canonical result.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS track_raw_genres (
               track_id TEXT NOT NULL,
               raw      TEXT NOT NULL,
               PRIMARY KEY (track_id, raw)
           )"""
    )
    # How well known a track is. Two providers, batched where possible; the source
    # is stored because ranks only compare inside one of them.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS track_popularity (
               track_id   TEXT PRIMARY KEY,
               playcount  INTEGER DEFAULT 0,
               users      INTEGER DEFAULT 0,
               source     TEXT NOT NULL,
               fetched_at INTEGER
           )"""
    )
    # A label the operator removed by hand, which a rebuild must not put back.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS track_genre_veto (
               track_id TEXT NOT NULL,
               label    TEXT NOT NULL,
               PRIMARY KEY (track_id, label)
           )"""
    )
    # MusicBrainz genres are per artist, so they are cached per artist rather than
    # copied onto every track.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS mb_artist_genres (
               artist_key TEXT PRIMARY KEY,
               mbid       TEXT,
               genres     TEXT,
               fetched_at INTEGER
           )"""
    )
    # Subsonic serves art under its own cover id (al-<album>_<hash>); asking for the
    # album id returns Navidrome's placeholder instead of the sleeve.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS album_art (
               album_id TEXT PRIMARY KEY,
               cover_id TEXT NOT NULL
           )"""
    )
    conn.commit()


def replace_tracks(conn: sqlite3.Connection, rows) -> None:
    conn.execute("DELETE FROM tracks")
    conn.executemany(
        f"INSERT INTO tracks ({TRACK_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


def replace_file_labels(conn: sqlite3.Connection, labels) -> None:
    """Recompute file-derived labels, keeping provider labels and manual vetoes.

    Labels fetched from a provider are knowledge about the artist, not a projection
    of the library, so a rebuild must not throw them away.
    """
    conn.execute("DELETE FROM track_genres WHERE source != 'external'")
    conn.executemany(
        "INSERT OR IGNORE INTO track_genres (track_id, label, source) VALUES (?,?,?)",
        labels,
    )


def replace_album_art(conn: sqlite3.Connection, rows) -> None:
    conn.execute("DELETE FROM album_art")
    conn.executemany(
        "INSERT OR REPLACE INTO album_art (album_id, cover_id) VALUES (?,?)", rows
    )


def replace_raw_genres(conn: sqlite3.Connection, rows) -> None:
    conn.execute("DELETE FROM track_raw_genres")
    conn.executemany(
        "INSERT OR IGNORE INTO track_raw_genres (track_id, raw) VALUES (?,?)", rows
    )
