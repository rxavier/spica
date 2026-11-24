"""The library index, built from Navidrome's Subsonic API.

Navidrome is treated as a service, not as a filesystem: the index is paged out of
`getAlbumList2` + `getAlbum`, and every track is played by handing Liquidsoap a
`rest/stream?id=...` URL. Nothing here reads Navidrome's database, its storage
layout, or a mount of the music tree.

The index itself is disposable: `POST /rebuild` drops it and re-pages the library,
so a schema or API change breaks a rebuild, not the data.
"""

from __future__ import annotations

import sqlite3
import unicodedata
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
    artist: str          # the credit on this track
    album: str
    album_id: str
    track_no: int
    year: int
    genre: str
    duration: int
    mbz_album: str = ""
    mbz_recording: str = ""
    album_artist: str = ""   # the record's artist: the identity everything groups by

    def as_line(self) -> str:
        return f"{self.title} — {self.artist} ({self.year or '?'})"


def name_key(name: str) -> str:
    """A name reduced to what it says, not how it was typed.

    "L’Impératrice", "L'Impératrice" and "l’imperatrice" are one artist; a model typing
    an ASCII apostrophe used to get nothing back from a library that stores the
    typographic one.
    """
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    for quote in "’‘`´":
        text = text.replace(quote, "'")
    # "blink‐182" uses a typographic hyphen; nobody types one of those.
    for dash in "‐‑‒–—−":
        text = text.replace(dash, "-")
    return " ".join(text.casefold().split())


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
    if "artist_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(tracks)")}:
        conn.execute("ALTER TABLE tracks ADD COLUMN artist_id TEXT DEFAULT ''")
    # The record's artist, which is the identity everything groups by: a guest on a
    # track ("L’Impératrice & Fabiana Martone") is a credit, not a different artist, and
    # counting by the track artist split one band across five names.
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(tracks)")}
    if "album_artist" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN album_artist TEXT DEFAULT ''")
    if "album_artist_id" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN album_artist_id TEXT DEFAULT ''")
    if "album_artist_key" not in columns:
        conn.execute("ALTER TABLE tracks ADD COLUMN album_artist_key TEXT DEFAULT ''")
    # ReplayGain arrives with the song on OpenSubsonic servers. Stored raw (the
    # tag's dB offset and linear peak) so a target change needs no re-index; the
    # gain itself is computed when a request is written.
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


def _tag_number(value) -> float | None:
    """A numeric ReplayGain field, or None for anything a tag parser would reject."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


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
    replaygain = 0

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
        album_artists = album.get("albumArtists") or []
        for song in songs:
            track_id = str(song.get("id") or "")
            if not track_id:
                continue
            # The record's artist, always, and the track credit only when the record
            # has none: that is what makes "L’Impératrice & Fabiana Martone" one artist
            # with the rest of the album instead of a sixth spelling of the band.
            own = (song.get("albumArtists") or album_artists or [{}])[0] or {}
            album_artist = str(
                own.get("name")
                or song.get("displayAlbumArtist")
                or album.get("displayAlbumArtist")
                or song.get("artist")
                or album.get("artist")
                or ""
            )
            album_artist_id = str(own.get("id") or song.get("artistId") or "")
            canon: list[str] = []
            seen_raw: list[str] = []
            for raw in subsonic.song_genres(song):
                name = str(raw).strip()
                if name and name.casefold() not in {x.casefold() for x in seen_raw}:
                    seen_raw.append(name)
                    raws.append((track_id, name))
                for label in genre_lib.labels_of(raw, operator=False):
                    if label.casefold() not in {x.casefold() for x in canon}:
                        canon.append(label)
            rg = song.get("replayGain")
            rg = rg if isinstance(rg, dict) else {}
            tag_gain = _tag_number(rg.get("trackGain"))
            if tag_gain is not None:
                replaygain += 1
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
                    album_artist,
                    album_artist_id,
                    name_key(album_artist),
                    tag_gain,
                    _tag_number(rg.get("trackPeak")),
                )
            )
            native[track_id] = canon
            meta.append(
                (
                    track_id,
                    str(song.get("albumId") or album.get("id") or ""),
                    album_artist,
                )
            )
            labels.extend((track_id, label, "tag") for label in canon)

    with conn:
        conn.execute("DELETE FROM tracks")
        conn.executemany(
            """INSERT INTO tracks
                 (id,uri,title,artist,album,album_id,track_no,year,genre,duration,fetched_at,
                  mbz_album,mbz_recording,artist_id,album_artist,album_artist_id,
                  album_artist_key,replaygain_gain,replaygain_peak)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
        f" · {replaygain} with ReplayGain"
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
            name = genre_lib.canonical(entry["name"], operator=False)
            if not name:
                continue
            merged[name] = max(merged.get(name, 0), int(entry.get("votes") or 0))
        return [{"name": name, "votes": votes} for name, votes in merged.items()]

    mb = {
        key: select_mb(_canonical_entries(vals))
        for key, vals in mb_genres_by_artist(conn).items()
    }
    mb = {key: names for key, names in mb.items() if names}

    from .queries import tracks as track_queries

    rows = track_queries.record_artists(conn)
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
    # Vetoes are placed from the console, which offers the collapsed vocabulary, so a
    # refusal has to be compared the same way station matching compares labels —
    # through canonical(). A literal string match stopped biting the moment the stored
    # label was the fine one ("Electronic" vetoed, row stored as "Nu Disco").
    vetoes: dict[str, set[str]] = {}
    for row in conn.execute("SELECT track_id, label FROM track_genre_veto"):
        vetoes.setdefault(row["track_id"], set()).add(genre_lib.fold(genre_lib.canonical(row["label"])))
    removed = 0
    if vetoes:
        doomed = []
        for row in conn.execute("SELECT track_id, label FROM track_genres"):
            keys = vetoes.get(row["track_id"])
            if not keys:
                continue
            if genre_lib.fold(genre_lib.canonical(row["label"])) in keys:
                doomed.append((row["track_id"], row["label"]))
        if doomed:
            removed = conn.executemany(
                "DELETE FROM track_genres WHERE track_id = ? AND label = ?", doomed
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
    #
    # The operator's aliases are a station-selection rollup, and they act HERE, at
    # match time: a track stored under its fine label ("Nu Disco") also answers the
    # shelf the operator filed it under ("Electronic"). Only the *track* side is rolled
    # up — the station's own words keep their spelling (builtin spelling fixes aside),
    # so a station that ticks "Nu Disco" is asking for nu disco and not for the whole
    # Electronic shelf.
    canon: dict[str, str] = {}
    for track_labels in by_track.values():
        for label in track_labels:
            if label not in canon:
                canon[label] = genre_lib.fold(genre_lib.canonical(label))

    def forms(labels) -> set[str]:
        out = set()
        for label in labels:
            out.add(genre_lib.fold(label))
            out.add(canon.get(label) or genre_lib.fold(genre_lib.canonical(label)))
        return out

    include = {genre_lib.fold(genre_lib.canonical(g, operator=False)) for g in st.genres if genre_lib.fold(g)}
    exclude = {genre_lib.fold(genre_lib.canonical(g, operator=False)) for g in st.exclude_genres if genre_lib.fold(g)}
    wanted = {a.strip().casefold() for a in st.artists if a.strip()}
    unwanted = {a.strip().casefold() for a in st.exclude_artists if a.strip()}

    def ok(row: sqlite3.Row) -> bool:
        if not row["uri"]:
            return False
        keys = row.keys()
        # A four-second gap between songs is a track to the index, not a track to a
        # station: it airs as dead air, and the standby rotation played one fifty-two
        # times. Unknown duration is left alone — no data is not a veto.
        if "duration" in keys:
            duration = int(row["duration"] or 0)
            if config.MIN_TRACK_S and duration and duration < config.MIN_TRACK_S:
                return False
        year = row["year"] or 0
        if st.year_from and year and year < st.year_from:
            return False
        if st.year_to and year and year > st.year_to:
            return False
        # Both names count as "this artist": a station that includes the act should
        # catch its tracks with guests, and one that excludes it should not let them
        # in through the credit.
        record_artist = (row["album_artist"] if "album_artist" in keys else "") or ""
        names = {(row["artist"] or "").casefold(), record_artist.casefold()} - {""}
        # An excluded artist is the most specific rule there is, so it wins outright.
        if unwanted and names & unwanted:
            return False
        # Genres and artists are both ways *in*: a track plays if it matches a
        # ticked genre OR belongs to a named artist. An artist list therefore
        # reaches outside the genres rather than narrowing them.
        by_artist = bool(wanted) and bool(names & wanted)
        labels = forms(by_track.get(row["id"], ())) if (include or wanted or exclude) else set()
        if include or wanted:
            by_genre = bool(include) and bool(labels & include)
            if not (by_genre or by_artist):
                return False
        # And a named artist is an exception the other way too: naming it is the
        # deliberate act, so it plays even when its genre is vetoed. A station that
        # excludes Progressive Rock and lists Giraffe wants Giraffe; the old order
        # vetoed it, which made `include_artists` look decorative.
        if exclude and not by_artist:
            if labels & exclude:
                return False
        return True

    return ok


def rows_for_station(conn: sqlite3.Connection, st: config.Station) -> list[sqlite3.Row]:
    from .queries import tracks as track_queries

    rows = track_queries.playable_tracks(conn)
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


def _collapsed(conn: sqlite3.Connection) -> dict[str, dict]:
    """Stored labels grouped the way the operator builds stations: one name per shelf.

    The index keeps the library's fine vocabulary ("Nu Disco", "Post-Disco", …) so the
    DJ's tools can match it; the admin console and station matching work on the
    operator's rollup instead, because ticking one shelf beats ticking twelve. This is
    the console's view: `{collapsed name: {tracks, members}}`.
    """
    out: dict[str, dict] = {}
    for row in conn.execute("SELECT label, COUNT(*) c FROM track_genres GROUP BY label"):
        name = genre_lib.pretty(genre_lib.canonical(row["label"]))
        entry = out.setdefault(name, {"tracks": 0, "members": {}})
        entry["tracks"] += row["c"]
        entry["members"][row["label"]] = row["c"]
    return out


def _member_labels(conn: sqlite3.Connection, label: str) -> list[str]:
    """Every stored label that the console shows under `label`."""
    want = genre_lib.fold(genre_lib.canonical(label))
    return [
        row["label"]
        for row in conn.execute("SELECT label FROM track_genres GROUP BY label")
        if genre_lib.fold(genre_lib.canonical(row["label"])) == want
    ]


def genres(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Collapsed labels with track counts — what the station editor picks from."""
    collapsed = _collapsed(conn)
    return sorted(((name, e["tracks"]) for name, e in collapsed.items()), key=lambda kv: (-kv[1], kv[0]))



def genre_report(conn: sqlite3.Connection) -> dict:
    """The operator's vocabulary plus the raw labels behind it — the console's data.

    `labels` is collapsed (one entry per shelf, with the fine labels nested under it)
    because the console exists to build stations; `raw` and `unmapped` still describe
    the library's own words, which is what a merge rule is written against.
    """
    raw = conn.execute(
        "SELECT raw AS genre, COUNT(*) c FROM track_raw_genres GROUP BY raw ORDER BY c DESC"
    ).fetchall()
    collapsed = _collapsed(conn)
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
        "labels": [
            {"label": name, "tracks": entry["tracks"],
             "members": [{"label": m, "tracks": c}
                         for m, c in sorted(entry["members"].items(), key=lambda kv: (-kv[1], kv[0]))
                         if m != name]}
            for name, entry in sorted(collapsed.items(), key=lambda kv: (-kv[1]["tracks"], kv[0]))
        ],
        "raw": [{"raw": r["genre"], "tracks": r["c"], "canonical": genre_lib.canonical(r["genre"])} for r in raw],
        "unmapped": unmapped,
        "sources": {r["source"]: {"tracks": r["tracks"], "labels": r["labels"]} for r in source_rows},
        "untagged_tracks": untagged,
    }


def genre_counts(conn: sqlite3.Connection) -> list[dict]:
    """One row per collapsed label: tracks, albums, sources, and its fine labels.

    Collapsed because this table is how the operator builds stations — one row per
    shelf, with the library's own labels nested underneath. The index still stores the
    fine ones; grouping is a view, not a rewrite.
    """
    collapsed = _collapsed(conn)
    albums: dict[str, set] = {}
    sources: dict[str, set] = {}
    for row in conn.execute(
        "SELECT g.label AS label, t.album_id AS album_id, g.source AS source"
        " FROM track_genres g JOIN tracks t ON t.id = g.track_id"
    ):
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
        for name, entry in sorted(collapsed.items(), key=lambda kv: (-kv[1]["tracks"], kv[0]))
    ]


def label_albums(conn: sqlite3.Connection, label: str) -> list[sqlite3.Row]:
    marks = ", ".join("?" * max(1, len(_member_labels(conn, label))))
    return conn.execute(
        f"""SELECT COALESCE(NULLIF(t.album_artist, ''), t.artist) AS artist,
                  t.album AS album, COUNT(*) AS tracks, MIN(t.year) AS year
           FROM track_genres g JOIN tracks t ON t.id = g.track_id
           WHERE g.label IN ({marks})
           GROUP BY COALESCE(NULLIF(t.album_artist, ''), t.artist), t.album
           ORDER BY COALESCE(NULLIF(t.album_artist, ''), t.artist), t.album""",
        _member_labels(conn, label) or [label],
    ).fetchall()


def label_tracks(conn: sqlite3.Connection, label: str, limit: int = 300) -> list[sqlite3.Row]:
    marks = ", ".join("?" * max(1, len(_member_labels(conn, label))))
    return conn.execute(
        f"""SELECT COALESCE(NULLIF(t.album_artist, ''), t.artist) AS artist,
                  t.album AS album, t.title AS title, t.year AS year
           FROM track_genres g JOIN tracks t ON t.id = g.track_id
           WHERE g.label IN ({marks})
           ORDER BY COALESCE(NULLIF(t.album_artist, ''), t.artist), t.album, t.track_no
           LIMIT ?""",
        (_member_labels(conn, label) or [label]) + [limit],
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
    # The console offers collapsed names ("Electronic"), the index stores fine ones
    # ("Nu Disco"): expand what was asked for into the labels actually on the rows.
    members: list[str] = []
    for name in labels:
        for found in _member_labels(conn, name) or [name]:
            if found not in members:
                members.append(found)
    labels = members
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
        f"""SELECT t.id AS id, COALESCE(NULLIF(t.album_artist, ''), t.artist) AS artist,
                   t.album AS album, t.title AS title, t.year AS year
            FROM tracks t
            WHERE t.id IN (SELECT track_id FROM track_genres WHERE label IN ({marks}))
            ORDER BY COALESCE(NULLIF(t.album_artist, ''), t.artist), t.album, t.track_no
            LIMIT ?""",
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
