"""The library index, built from Navidrome's Subsonic API.

Navidrome is treated as a service, not as a filesystem: the index is paged out of
`getAlbumList2` + `getAlbum`, and every track is played by handing Liquidsoap a
`rest/stream?id=...` URL. Nothing here reads Navidrome's database, its storage
layout, or a mount of the music tree.

The index itself is disposable: `POST /rebuild` drops it and re-pages the library,
so a schema or API change breaks a rebuild, not the data.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from . import config
from . import genres as genre_lib
from . import subsonic


@dataclass
class Track:
    id: str
    uri: str
    title: str
    artist: str
    album: str
    album_id: str
    track_no: int
    year: int
    genre: str
    duration: int
    mbz_album: str = ""
    mbz_recording: str = ""

    def as_line(self) -> str:
        return f"{self.title} — {self.artist} ({self.year or '?'})"


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # The API runs picks in worker threads (asyncio.to_thread), so the handle must
    # be usable across them. Reads dominate; writes happen only on /rebuild.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Indexes built before the API rewrite stored a filesystem path. The track
    # table is a rebuildable projection, so an old shape is dropped rather than
    # migrated: POST /rebuild repopulates it from the API.
    have = {r["name"] for r in conn.execute("PRAGMA table_info(tracks)")}
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
            artist_id TEXT DEFAULT ''
        )
        """
    )
    # `artist_id` is Navidrome's own handle for the artist, which is what turns a
    # name into a MusicBrainz id without guessing: getArtist answers with the
    # artist's `musicBrainzId`. Older indexes get the column rather than a rebuild,
    # so manual labels and cached provider answers survive the upgrade.
    if "artist_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(tracks)")}:
        conn.execute("ALTER TABLE tracks ADD COLUMN artist_id TEXT DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS tracks_genre ON tracks(genre)")
    conn.execute("CREATE INDEX IF NOT EXISTS tracks_year ON tracks(year)")
    conn.execute("CREATE INDEX IF NOT EXISTS tracks_artist ON tracks(artist)")
    # now_playing() looks a track's album up by album_id on every poll, and the
    # ledger once per row; without this it is a scan of the whole library each time.
    conn.execute("CREATE INDEX IF NOT EXISTS tracks_album_id ON tracks(album_id)")
    # Canonical genre labels, one row per (track, label). `tracks.genre` keeps
    # only the first label for display; every filter walks this table instead,
    # because matching is not SQL-expressible (see radio/genres.py).
    #
    # `source` records where the label came from: "tag" when Navidrome reports it,
    # otherwise "album" or "artist" when it was inherited from that level, or
    # "external" when a provider supplied it.
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
    # future genre UI: `track_genres` holds only the canonical result.
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
    from . import enrich, jobs

    enrich.ensure_schema(conn)
    jobs.ensure_schema(conn)
    return conn


# Fill a track's missing genre from the level above, but only when that level
# agrees strongly: the label must cover at least half the tagged tracks of the
# album (preferred) or the artist, and at most three labels are taken. These live
# in `track_genres` with source='album' or 'artist', so a real tag always wins and
# a later cleanup simply stops using the inherited ones.
# "Future Islands feat. Debbie Harry" is a credit, not an artist: genre votes and
# fallback lookups have to group under the primary name or those tracks are
# treated as an unknown artist and stay unlabelled.
_FEAT = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+", re.IGNORECASE)


def primary_artist(name: str) -> str:
    return _FEAT.split(name.strip(), maxsplit=1)[0].strip()


GENRE_FILL_MIN_SHARE = 0.5
GENRE_FILL_MAX = 3
# ...and only when the level itself is meaningfully tagged: at least this share
# of the album's or artist's tracks must carry a real tag before anything is
# inherited from it. Without a floor a single tag on a 97-track artist
# characterises the other 87.
GENRE_FILL_MIN_COVERAGE = 0.25


def _inherited_labels(
    meta: list[tuple[str, str, str]], native: dict[str, list[str]]
) -> list[tuple[str, str, str]]:
    album_freq: dict[str, Counter] = defaultdict(Counter)
    artist_freq: dict[str, Counter] = defaultdict(Counter)
    album_tagged: Counter = Counter()
    artist_tagged: Counter = Counter()
    album_total: Counter = Counter()
    artist_total: Counter = Counter()
    for tid, album, artist in meta:
        album_total[album] += 1
        artist_total[artist] += 1
        labels = native.get(tid) or ()
        if not labels:
            continue
        album_tagged[album] += 1
        artist_tagged[artist] += 1
        for label in labels:
            album_freq[album][label] += 1
            artist_freq[artist][label] += 1

    out: list[tuple[str, str, str]] = []
    for tid, album, artist in meta:
        if native.get(tid):
            continue  # never override a real tag
        for source, freq, tagged, totals, key in (
            ("album", album_freq, album_tagged, album_total, album),
            ("artist", artist_freq, artist_tagged, artist_total, artist),
        ):
            total = tagged.get(key, 0)
            if not total:
                continue
            if total / totals[key] < GENRE_FILL_MIN_COVERAGE:
                continue  # too thinly tagged at this level to characterise it
            picks = [
                label
                for label, hits in freq[key].most_common(GENRE_FILL_MAX)
                if hits / total >= GENRE_FILL_MIN_SHARE
            ]
            if picks:
                out.extend((tid, label, source) for label in picks)
                break
    return out


def rebuild_index(conn: sqlite3.Connection, log=print, page_error: bool = True) -> int:
    """Page the whole library out of the API and rebuild the projection."""
    now = int(time.time())
    rows: list[tuple] = []
    labels: list[tuple[str, str, str]] = []
    raws: list[tuple[str, str]] = []
    meta: list[tuple[str, str, str]] = []
    native: dict[str, list[str]] = {}
    albums = 0
    failed = 0

    album_art: list[tuple[str, str]] = []
    for album in subsonic.iter_albums():
        albums += 1
        # Subsonic serves covers under a derived id (al-<id>_<hash>), not the album
        # id: asking for the album id returns Navidrome's placeholder.
        cover = str(album.get("coverArt") or "")
        if album.get("id") and cover:
            album_art.append((str(album["id"]), cover))
        try:
            songs = subsonic.album_songs(str(album.get("id", "")))
        except Exception as exc:  # one bad album must not lose the library
            failed += 1
            if page_error:
                log(f"album {album.get('name')!r}: {exc}")
            continue
        for song in songs:
            track_id = str(song.get("id") or "")
            if not track_id:
                continue
            canon: list[str] = []
            seen_raw: list[str] = []
            for raw in subsonic.song_genres(song):
                name = str(raw).strip()
                if name and name.casefold() not in {x.casefold() for x in seen_raw}:
                    seen_raw.append(name)
                    raws.append((track_id, name))
                for label in genre_lib.labels_of(raw):
                    if label.casefold() not in {x.casefold() for x in canon}:
                        canon.append(label)
            rows.append(
                (
                    track_id,
                    subsonic.stream_url(track_id),
                    song.get("title") or "",
                    song.get("artist") or album.get("artist") or "",
                    song.get("album") or album.get("name") or "",
                    str(song.get("albumId") or album.get("id") or ""),
                    int(song.get("track") or 0),
                    int(song.get("year") or album.get("year") or 0),
                    canon[0] if canon else "",
                    int(song.get("duration") or 0),
                    now,
                    str(album.get("musicBrainzId") or ""),
                    str(song.get("musicBrainzId") or ""),
                    str(song.get("artistId") or album.get("artistId") or ""),
                )
            )
            native[track_id] = canon
            meta.append(
                (
                    track_id,
                    str(song.get("albumId") or album.get("id") or ""),
                    primary_artist(song.get("artist") or album.get("artist") or ""),
                )
            )
            labels.extend((track_id, label, "tag") for label in canon)

    with conn:
        conn.execute("DELETE FROM tracks")
        conn.executemany(
            """INSERT INTO tracks
                 (id,uri,title,artist,album,album_id,track_no,year,genre,duration,fetched_at,
                  mbz_album,mbz_recording,artist_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        labels.extend(_inherited_labels(meta, native))
        # Labels fetched from a provider are knowledge about the artist, not a
        # projection of the library, so a rebuild must not throw them away —
        # only the labels derived from the files are recomputed here.
        conn.execute("DELETE FROM track_genres WHERE source != 'external'")
        conn.executemany(
            "INSERT OR IGNORE INTO track_genres (track_id, label, source) VALUES (?,?,?)", labels
        )
        conn.execute("DELETE FROM album_art")
        conn.executemany(
            "INSERT OR REPLACE INTO album_art (album_id, cover_id) VALUES (?,?)", album_art
        )
        apply_genre_precedence(conn, log)
        merged = genre_lib.unify_spellings(conn)
        if merged:
            log(f"genres: {merged} spelling variant(s) merged")
        conn.execute("DELETE FROM track_raw_genres")
        conn.executemany("INSERT OR IGNORE INTO track_raw_genres (track_id, raw) VALUES (?,?)", raws)

    by_source = Counter(row[2] for row in labels)
    log(
        f"indexed {len(rows)} tracks from {albums} albums via the API"
        + (f" ({failed} album(s) failed)" if failed else "")
        + f" — {len(labels)} genre labels "
        f"({', '.join(f'{k} {v}' for k, v in sorted(by_source.items()))})"
    )
    return len(rows)


def labels_by_track(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Every track's canonical genre labels, in one pass."""
    out: dict[str, list[str]] = {}
    for r in conn.execute("SELECT track_id, label FROM track_genres"):
        out.setdefault(r["track_id"], []).append(r["label"])
    return out


def mb_genres_by_artist(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    import json as _json

    out: dict[str, list[dict]] = {}
    for row in conn.execute("SELECT artist_key, genres FROM mb_artist_genres"):
        try:
            entries = _json.loads(row["genres"] or "[]")
        except ValueError:
            continue
        genres = [
            {"name": e["name"], "votes": int(e.get("votes") or 0)}
            if isinstance(e, dict)
            else {"name": str(e), "votes": 0}
            for e in entries
        ]
        if genres:
            out[row["artist_key"]] = genres
    return out


def select_mb(genres: list[dict]) -> list[str]:
    """The artist's genre, singular: whatever MusicBrainz votes for most.

    Ties keep every tied name, because a tie is the data saying it cannot choose
    between them. Everything below the top is opinion that happens to have been
    entered once — an artist with 23 votes for progressive rock and 7 for art rock is
    a progressive rock artist, and carrying both made the vocabulary noise.
    """
    ranked = sorted(genres, key=lambda g: (-g["votes"], g["name"]))
    if not ranked:
        return []
    top = ranked[0]["votes"]
    return [g["name"] for g in ranked if g["votes"] == top]


def apply_genre_precedence(conn: sqlite3.Connection, log=print) -> dict:
    """One source per track, in the order the operator asked for.

    A file tag is the operator's own assertion and wins outright. Where there is no
    tag, MusicBrainz speaks next, and Last.fm only fills what is left. Keeping every
    layer would rebuild the exact noise this is meant to remove.

    Runs inside the caller's transaction and never commits: it is one step of a
    rebuild, and stepping out of that transaction halfway left a partial index.
    """
    # Provider names go through the same canonicalisation as file tags, or an
    # operator merge is undone by the next rebuild that re-reads the cache.
    def _canonical_entries(entries: list[dict]) -> list[dict]:
        merged: dict[str, int] = {}
        for entry in entries:
            if not genre_lib.is_genre_like(entry["name"]):
                continue  # instrumental, atmospheric, cover — true, not a genre
            name = genre_lib.canonical(entry["name"])
            if not name:
                continue
            merged[name] = max(merged.get(name, 0), int(entry.get("votes") or 0))
        return [{"name": name, "votes": votes} for name, votes in merged.items()]

    mb = {
        key: select_mb(_canonical_entries(vals))
        for key, vals in mb_genres_by_artist(conn).items()
    }
    mb = {key: names for key, names in mb.items() if names}

    rows = conn.execute("SELECT id, artist FROM tracks").fetchall()
    tagged = 0
    informed = 0
    # No transaction of its own: the rebuild already has one open, and opening a
    # second here committed the caller's half-written index every time it exited.
    for row in rows:
        names = mb.get(genre_lib.fold(row["artist"]))
        if not names:
            # No MusicBrainz answer: whatever Last.fm said stands.
            continue
        # MusicBrainz is consulted for every artist it knows, file tag or not.
        conn.execute(
            "DELETE FROM track_genres WHERE track_id = ? AND source NOT IN ('tag', 'manual')",
            (row["id"],),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO track_genres (track_id, label, source) VALUES (?,?, 'musicbrainz')",
            [(row["id"], name) for name in names],
        )
        informed += 1
        tagged += int(
            conn.execute(
                "SELECT COUNT(*) c FROM track_genres WHERE track_id = ? AND source = 'tag'",
                (row["id"],),
            ).fetchone()["c"]
            > 0
        )
    removed = conn.execute(
        """DELETE FROM track_genres
           WHERE EXISTS (SELECT 1 FROM track_genre_veto v
                         WHERE v.track_id = track_genres.track_id
                           AND lower(v.label) = lower(track_genres.label))"""
    ).rowcount
    if removed:
        log(f"genres: {removed} label(s) kept out by hand")
    log(f"genres: musicbrainz informed {informed} track(s), {tagged} of them also carry a file tag")
    return {"musicbrainz": informed, "with_file_tag": tagged}


def station_matcher(conn: sqlite3.Connection, st: config.Station):
    """One predicate that decides whether a track may play on this station.

    Used by the pool builder AND by the DJ's tools, so a station's rules are a
    constraint on what the model can return, not a suggestion in its prompt.
    """
    by_track = labels_by_track(conn)
    # A station picks labels from the vocabulary, so this is set membership: no
    # substring guessing about what a genre name might have meant.
    include = {genre_lib.fold(g) for g in st.genres if genre_lib.fold(g)}
    exclude = {genre_lib.fold(g) for g in st.exclude_genres if genre_lib.fold(g)}
    wanted = {a.strip().casefold() for a in st.artists if a.strip()}
    unwanted = {a.strip().casefold() for a in st.exclude_artists if a.strip()}

    def ok(row: sqlite3.Row) -> bool:
        if not row["uri"]:
            return False
        year = row["year"] or 0
        if st.year_from and year and year < st.year_from:
            return False
        if st.year_to and year and year > st.year_to:
            return False
        artist = (row["artist"] or "").casefold()
        if unwanted and artist in unwanted:
            return False
        # Genres and artists are both ways *in*: a track plays if it matches a
        # ticked genre OR belongs to a named artist. An artist list therefore
        # reaches outside the genres rather than narrowing them.
        if include or wanted:
            labels = {genre_lib.fold(label) for label in by_track.get(row["id"], ())}
            by_genre = bool(include) and bool(labels & include)
            by_artist = bool(wanted) and artist in wanted
            if not (by_genre or by_artist):
                return False
        if exclude:
            labels = {genre_lib.fold(label) for label in by_track.get(row["id"], ())}
            if labels & exclude:
                return False
        return True

    return ok


def rows_for_station(conn: sqlite3.Connection, st: config.Station) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT * FROM tracks WHERE uri != ''").fetchall()
    ok = station_matcher(conn, st)
    picked = [r for r in rows if ok(r)]
    if picked:
        return picked

    # A station whose rules match nothing must never go on air empty: say so
    # loudly and fall back to the whole library. This is what a typo, or a
    # library with no genre tags at all, looks like.
    print(
        f"WARNING station {st.id}: rules (genres {list(st.genres)}) match no tracks "
        f"({len(rows)} candidates) — falling back to the whole library",
        flush=True,
    )
    return rows


def genres(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Canonical labels with track counts."""
    q = conn.execute(
        "SELECT label, COUNT(*) c FROM track_genres GROUP BY label ORDER BY c DESC"
    )
    return [(r["label"], r["c"]) for r in q.fetchall()]


def genre_report(conn: sqlite3.Connection) -> dict:
    """Canonical vocabulary plus the raw labels behind it — the future UI's data."""
    raw = conn.execute(
        "SELECT raw AS genre, COUNT(*) c FROM track_raw_genres GROUP BY raw ORDER BY c DESC"
    ).fetchall()
    canonical = genres(conn)
    unmapped = [
        {"raw": r["genre"], "tracks": r["c"], "canonical": genre_lib.canonical(r["genre"])}
        for r in raw
        if genre_lib.explain(r["genre"])["source"] == "library"
    ]
    source_rows = conn.execute(
        "SELECT source, COUNT(DISTINCT track_id) tracks, COUNT(*) labels "
        "FROM track_genres GROUP BY source ORDER BY tracks DESC"
    ).fetchall()
    untagged = conn.execute(
        "SELECT COUNT(*) c FROM tracks t WHERE NOT EXISTS "
        "(SELECT 1 FROM track_genres g WHERE g.track_id = t.id)"
    ).fetchone()["c"]
    return {
        "labels": [{"label": label, "tracks": count} for label, count in canonical],
        "raw": [{"raw": r["genre"], "tracks": r["c"], "canonical": genre_lib.canonical(r["genre"])} for r in raw],
        "unmapped": unmapped,
        "sources": {r["source"]: {"tracks": r["tracks"], "labels": r["labels"]} for r in source_rows},
        "untagged_tracks": untagged,
    }


def genre_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """One row per canonical label: how many tracks, how many albums."""
    return conn.execute(
        """SELECT g.label AS label, COUNT(*) AS tracks, COUNT(DISTINCT t.album_id) AS albums,
                  GROUP_CONCAT(DISTINCT g.source) AS sources
           FROM track_genres g JOIN tracks t ON t.id = g.track_id
           GROUP BY g.label ORDER BY tracks DESC, g.label"""
    ).fetchall()


def label_albums(conn: sqlite3.Connection, label: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT t.artist AS artist, t.album AS album, COUNT(*) AS tracks,
                  MIN(t.year) AS year
           FROM track_genres g JOIN tracks t ON t.id = g.track_id
           WHERE g.label = ?
           GROUP BY t.artist, t.album ORDER BY t.artist, t.album""",
        (label,),
    ).fetchall()


def label_tracks(conn: sqlite3.Connection, label: str, limit: int = 300) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT t.artist AS artist, t.album AS album, t.title AS title, t.year AS year
           FROM track_genres g JOIN tracks t ON t.id = g.track_id
           WHERE g.label = ? ORDER BY t.artist, t.album, t.track_no LIMIT ?""",
        (label, limit),
    ).fetchall()


_SOURCE_WORDS = {
    "tag": "file",
    "manual": "by hand",
    "musicbrainz": "musicbrainz",
    "external": "last.fm",
    "artist": "last.fm",
    "album": "last.fm",
}


def label_explorer(conn: sqlite3.Connection, labels: list[str], limit: int = 500) -> dict:
    """Every track carrying any of these labels, each with all of its own genres
    and where each of those came from."""
    labels = [l for l in labels if l]
    if not labels:
        return {"tracks": [], "total": 0, "albums": 0}
    marks = ",".join("?" * len(labels))
    total = conn.execute(
        f"SELECT COUNT(DISTINCT track_id) c FROM track_genres WHERE label IN ({marks})", labels
    ).fetchone()["c"]
    albums = conn.execute(
        f"""SELECT COUNT(DISTINCT t.album_id) c FROM track_genres g
            JOIN tracks t ON t.id = g.track_id WHERE g.label IN ({marks})""",
        labels,
    ).fetchone()["c"]
    rows = conn.execute(
        f"""SELECT t.id AS id, t.artist AS artist, t.album AS album, t.title AS title,
                   t.year AS year
            FROM tracks t
            WHERE t.id IN (SELECT track_id FROM track_genres WHERE label IN ({marks}))
            ORDER BY t.artist, t.album, t.track_no LIMIT ?""",
        labels + [limit],
    ).fetchall()

    ids = [r["id"] for r in rows]
    by_track: dict[str, list[str]] = {}
    for chunk_start in range(0, len(ids), 400):
        chunk = ids[chunk_start : chunk_start + 400]
        marks2 = ",".join("?" * len(chunk))
        for g in conn.execute(
            f"""SELECT track_id, label, GROUP_CONCAT(DISTINCT source) AS sources
                FROM track_genres WHERE track_id IN ({marks2})
                GROUP BY track_id, label""",
            chunk,
        ):
            words = [_SOURCE_WORDS.get(s, s) for s in (g["sources"] or "").split(",") if s]
            by_track.setdefault(g["track_id"], []).append(
                f"{g['label']} ({', '.join(words)})" if words else g["label"]
            )

    tracks = []
    for r in rows:
        item = dict(r)
        item["genres"] = sorted(by_track.get(r["id"], []), key=str.casefold)
        tracks.append(item)
    return {"tracks": tracks, "total": total, "albums": albums}
