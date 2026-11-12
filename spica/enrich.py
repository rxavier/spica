"""Optional enrichment: MusicBrainz ids and "similar artist" data.

Names are the baseline — every library has them. IDs are a cache: each provider
fills what it can, and anything it cannot answer stays unresolved without
breaking a pick. Providers, cheapest first:

  1. Navidrome's own artist records — one call per artist, no search, no guessing
  2. MusicBrainz web service  — album MBID -> release -> artist MBID (1 req/s)
  3. ListenBrainz             — artist MBID -> similar artists (keyless)
  4. Last.fm                  — artist NAME -> similar artists / tags (needs a key)

Nothing here is required: with no network and no key, the DJ falls back to
search/filter over names.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
import urllib.request

from . import config, subsonic

MB = "https://musicbrainz.org/ws/2"
LB = "https://labs.api.listenbrainz.org"
# The labs endpoint requires an explicit algorithm profile; this is the default
# session-based one (7,500 days of listens, 300 per artist).
LB_ALGORITHM = (
    "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"
)
LF = "https://ws.audioscrobbler.com/2.0"

_last_call: dict[str, float] = {}


def _throttle(bucket: str, min_interval: float) -> None:
    now = time.time()
    wait = min_interval - (now - _last_call.get(bucket, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_call[bucket] = time.time()


def _get_json(url: str, headers: dict | None = None, timeout: float = 20.0):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------- providers


def mb_release_artist(mbz_album_id: str) -> tuple[str | None, str | None]:
    """album MBID -> (artist MBID, artist name). One call per album, not per track."""
    if not mbz_album_id:
        return None, None
    _throttle("mb", 1.1)  # MusicBrainz asks for <= 1 req/s
    url = f"{MB}/release/{mbz_album_id}?inc=artist-credits&fmt=json"
    try:
        d = _get_json(url, {"User-Agent": config.MB_USER_AGENT})
        credits = d.get("artist-credit") or []
        for c in credits:
            artist = c.get("artist") or {}
            if artist.get("id"):
                return artist["id"], artist.get("name")
    except Exception:
        return None, None
    return None, None


def lb_similar_artists(artist_mbid: str, limit: int = 25) -> list[str]:
    """ListenBrainz artist similarity — keyless, keyed on the MBID."""
    if not artist_mbid:
        return []
    try:
        rows = _get_json(
            f"{LB}/similar-artists/json?artist_mbids={artist_mbid}&algorithm={LB_ALGORITHM}",
            timeout=30.0,
        )
    except Exception:
        return []
    out: list[str] = []
    if isinstance(rows, list):
        for row in rows[: limit * 2]:
            name = row.get("name") or row.get("artist_name")
            if name and name not in out:
                out.append(name)
    return out[:limit]


def lfm_similar(name: str, limit: int = 25) -> list[str]:
    """Last.fm similarity by NAME — the ID-free path, needs an API key."""
    if not config.LASTFM_KEY or not name:
        return []
    _throttle("lfm", 0.2)
    q = urllib.parse.urlencode(
        {"method": "artist.getsimilar", "artist": name, "limit": limit,
         "api_key": config.LASTFM_KEY, "format": "json"}
    )
    try:
        d = _get_json(f"{LF}/?{q}")
        return [a["name"] for a in d.get("similarartists", {}).get("artist", [])][:limit]
    except Exception:
        return []


def lfm_tags(name: str) -> list[str]:
    if not config.LASTFM_KEY or not name:
        return []
    _throttle("lfm", 0.2)
    q = urllib.parse.urlencode(
        {"method": "artist.gettoptags", "artist": name, "autocorrect": 1,
         "api_key": config.LASTFM_KEY, "format": "json"}
    )
    try:
        d = _get_json(f"{LF}/?{q}")
        tags = d.get("toptags", {}).get("tag", [])
        return [t["name"] for t in tags if int(t.get("count", 0)) > 0][:15]
    except Exception:
        return []


# ------------------------------------------------------------------- store


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
        CREATE INDEX IF NOT EXISTS similar_by_name ON similar_artists(name);
        """
    )
    conn.commit()


def cached_mbid(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute("SELECT mbid FROM artist_mbids WHERE name = ?", (name,)).fetchone()
    return row["mbid"] if row else None


def remember_mbid(conn: sqlite3.Connection, name: str, mbid: str, source: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO artist_mbids (name, mbid, source, fetched_at) VALUES (?,?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET mbid=excluded.mbid, source=excluded.source,"
            " fetched_at=excluded.fetched_at",
            (name, mbid, source, int(time.time())),
        )


def cached_similar(conn: sqlite3.Connection, name: str) -> list[str]:
    rows = conn.execute("SELECT similar FROM similar_artists WHERE name = ?", (name,)).fetchall()
    return [r["similar"] for r in rows]


def remember_similar(conn: sqlite3.Connection, name: str, names: list[str], source: str) -> None:
    now = int(time.time())
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO similar_artists (name, similar, source, fetched_at) VALUES (?,?,?,?)",
            [(name, s, source, now) for s in names],
        )


def similar_artists(conn: sqlite3.Connection, name: str, limit: int = 25) -> tuple[list[str], str]:
    """Cache -> ListenBrainz (by MBID) -> Last.fm (by name). Returns (names, source).

    The MBID comes from the cache, which `/enrich` fills from Navidrome's own
    artist records and, failing those, from a MusicBrainz release lookup.
    """
    hit = cached_similar(conn, name)
    if hit:
        return hit[:limit], "cache"

    mbid = cached_mbid(conn, name)
    if mbid:
        names = lb_similar_artists(mbid, limit)
        if names:
            remember_similar(conn, name, names, "listenbrainz")
            return names, "listenbrainz"

    names = lfm_similar(name, limit)
    if names:
        remember_similar(conn, name, names, "lastfm")
        return names, "lastfm"

    return [], "none"


def navidrome_artist_mbid(artist_id: str) -> str:
    """The artist's MusicBrainz id straight out of Navidrome, or "".

    Most libraries already carry this: `getArtist` answers with a `musicBrainzId`,
    which is a name turned into an id by the thing that knows the answer rather
    than by a search.
    """
    if not artist_id:
        return ""
    try:
        return str(subsonic.get_artist(artist_id).get("musicBrainzId") or "")
    except Exception:
        return ""



def lfm_track(artist: str, title: str) -> dict | None:
    """Last.fm play counts for one track."""
    from . import config

    if not config.LASTFM_KEY or not artist or not title:
        return None
    params = {
        "method": "track.getInfo",
        "api_key": config.LASTFM_KEY,
        "artist": artist,
        "track": title,
        "format": "json",
        "autocorrect": 1,
    }
    try:
        with urllib.request.urlopen(
            f"https://ws.audioscrobbler.com/2.0/?{urllib.parse.urlencode(params)}", timeout=15
        ) as response:
            data = json.loads(response.read())
    except Exception:
        return None
    track = (data or {}).get("track") or {}
    if not track.get("playcount"):
        return None
    return {"playcount": int(track["playcount"]), "users": int(track.get("listeners") or 0), "source": "lastfm"}


def lb_popularity(mbids: list[str]) -> dict[str, dict]:
    """ListenBrainz listen counts for a batch of recordings, keyed by MBID.

    Batched, which is the whole point: 4,600 tracks cost about forty requests
    instead of 4,600, and the ids come from the library so nothing is fuzzy-matched.
    """
    if not mbids:
        return {}
    request = urllib.request.Request(
        "https://api.listenbrainz.org/1/popularity/recording",
        data=json.dumps({"recording_mbids": mbids}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            rows = json.loads(response.read())
    except Exception:
        return {}
    return {
        row["recording_mbid"]: {
            "playcount": int(row.get("total_listen_count") or 0),
            "users": int(row.get("total_user_count") or 0),
            "source": "listenbrainz",
        }
        for row in rows
        if row.get("recording_mbid")
    }


def _mb_get(url: str, attempts: int = 3) -> dict:
    """One MusicBrainz GET, with patience: the limiter answers 503 under load and the
    call is otherwise fine a few seconds later."""
    from . import config

    last: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers={"User-Agent": config.MB_USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except Exception as exc:  # timeout, DNS, reset
            last = exc
        time.sleep(2 + attempt * 4)
    raise last if last else RuntimeError("musicbrainz: no response")


_ARTIST_IDS = "artist-mbids.json"


def _artist_overrides() -> dict[str, str]:
    """Operator pins: folded artist name -> MusicBrainz id.

    Name search is the weak link — 'Solaris' is a Hungarian prog band and also an
    ambient producer, 'Frost*' loses to Celtic Frost — so an operator can settle the
    question once, in $SPICA_CONFIG/artist-mbids.json.
    """
    from . import config

    try:
        raw = json.loads((config.CONFIG_DIR / _ARTIST_IDS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k).strip().lower(): str(v).strip() for k, v in raw.items() if v}


def _release_titles(mbid: str, attempts: int = 3) -> set[str]:
    """Album titles MusicBrainz credits to this artist. Empty on failure."""
    from . import genres

    for attempt in range(attempts):
        try:
            data = _mb_get(
                f"https://musicbrainz.org/ws/2/artist/{mbid}?inc=release-groups&limit=100&fmt=json"
            )
            return {genres.fold(g.get("title")) for g in data.get("release-groups", []) if g.get("title")}
        except Exception:
            time.sleep(1.5 * (attempt + 1))  # 429/503 from the rate limiter
    return set()


def mb_artist_genres(artist: str, albums: list[str] | None = None, pause=1.1) -> dict | None:
    """MusicBrainz genres for an artist, most voted first.

    Two calls for an unambiguous name: the search turns a name into an id, the lookup
    carries the genres with their vote counts. When several artists share the name, the
    candidates are checked against the album titles in the library and the one that
    actually owns those records wins; if none does, the answer is "unknown" rather than
    a confident wrong genre.
    """
    from . import config, genres

    if not artist:
        return None

    override = _artist_overrides().get(genres.fold(artist))
    if override:
        mbid, name = override, artist
        time.sleep(pause)
    else:
        query = urllib.parse.urlencode({"query": f'artist:"{artist}"', "fmt": "json", "limit": 5})
        try:
            hits = _mb_get(f"https://musicbrainz.org/ws/2/artist?{query}").get("artists") or []
        except Exception:
            return None
        hits = [h for h in hits if int(h.get("score") or 0) >= 85][:5]
        if not hits:
            return None
        chosen = hits[0]
        if len(hits) > 1 and albums:
            wanted = {genres.fold(a) for a in albums if a}
            best, best_overlap = None, 0
            for hit in hits:
                time.sleep(pause)
                overlap = len(wanted & _release_titles(hit["id"]))
                if overlap > best_overlap:
                    best, best_overlap = hit, overlap
            if best is None:
                return None  # ambiguous and nothing to prove it with: no opinion
            chosen = best
        elif len(hits) > 1:
            return None  # shared name and no album titles to settle it
        mbid, name = chosen.get("id"), chosen.get("name") or artist
        time.sleep(pause)

    try:
        entity = _mb_get(f"https://musicbrainz.org/ws/2/artist/{mbid}?inc=genres+tags&fmt=json")
    except Exception:
        return None
    ranked = sorted(
        ({"name": g["name"], "votes": int(g.get("count") or 0)} for g in entity.get("genres", [])),
        key=lambda g: (-g["votes"], g["name"]),
    )
    return {"mbid": mbid, "name": name, "genres": ranked}


_VOCAB_FILE = "mb-genres.json"


def mb_genre_vocabulary(force: bool = False, pause: float = 1.0) -> set[str]:
    """MusicBrainz's own genre list — 2,200 names it considers genres.

    This is the difference between a tag and a genre: `psychedelic rock` and `stoner
    rock` are in it, `france`, `genius` and `cuatro puertorriqueno` are not. Cached for
    a month because it changes slowly and costs twenty-three throttled calls.
    """
    from . import config, genres

    path = config.STATE_DIR / _VOCAB_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not force and time.time() - float(data.get("fetched_at") or 0) < 30 * 86400:
            return set(data.get("genres") or [])
    except (OSError, ValueError):
        pass

    names: list[str] = []
    offset = 0
    try:
        while True:
            page = _mb_get(
                f"https://musicbrainz.org/ws/2/genre/all?fmt=json&limit=100&offset={offset}"
            )
            batch = page.get("genres") or []
            if not batch:
                break
            names.extend(g["name"] for g in batch)
            offset += len(batch)
            if offset >= int(page.get("genre-count") or 0):
                break
            time.sleep(pause)
    except Exception:
        return set()

    folded = sorted({genres.fold(n) for n in names if n})
    try:
        path.write_text(
            json.dumps({"fetched_at": int(time.time()), "genres": folded}), encoding="utf-8"
        )
    except OSError:
        pass
    return set(folded)
