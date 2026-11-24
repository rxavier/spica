"""The query layer the tools sit on.

Six read-only questions over the index — search, filter, catalogue, similar,
history, station map — and the one place a tool call is dispatched. Nothing here
talks to the network; the tools only ever read what the rebuild wrote.
"""

from __future__ import annotations

import difflib
import hashlib
import html
import json
import random
import sqlite3
import time
from collections import Counter

from .. import config, loudness, mixer, navidrome, queue
from .. import genres as genres_module
from ..queries import tracks as track_queries
from .schemas import TOOLS


def clamp(value, default: int, high: int) -> int:
    """A limit the model chose, kept inside sensible bounds.

    limit=0 would slice to nothing (or, with recent()'s [-n:], to everything) and a
    huge one would flood a context the tool has to fit in 6000 characters anyway.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, high))


# How many tracks one artist may contribute to a page of `filter` results.
PER_ARTIST = 3

# How much of the 6,000-character tool result a page may spend on rows, leaving room for
# the envelope around them (`matching`, `offset`, `returned`, `next_offset`, a note).
# A page has to fit *before* the model sees it: the trimmer downstream cuts from the end,
# and a tool that says "returned 30" while the model reads 23 makes `offset` paging skip
# the seven tracks that were never shown.
ROW_BUDGET = 5200


class Library:
    # Set by the loop once per pick; a standalone call gets its own throwaway seed.
    draw_seed: int = 0

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        # The optional analysis store: None means the feature is off, and every
        # caller below treats that as "behave exactly as before".
        self._analysis_conn = None
        self._analysis_at: float | None = None
        self._analysis_features: dict = {}
        self._vectors: dict = {}
        self._vectors_at: float | None = None

    def analysis_store(self):
        """The read-only analysis store, reopened at most once a minute.

        A separate file from the index on purpose: the index is disposable and the
        analysis is an hour of CPU. Absent means analysis never ran here.
        """
        from .. import analysis

        if not analysis.enabled():
            return None
        now = time.monotonic()
        if self._analysis_at is None or now - self._analysis_at > 60:
            try:
                if self._analysis_conn is not None:
                    self._analysis_conn.close()
            except Exception:
                pass
            self._analysis_conn = analysis.connect(readonly=True)
            self._analysis_at = now
            self._analysis_features = analysis.feature_map(self._analysis_conn)
        return self._analysis_conn

    def _features(self) -> dict:
        self.analysis_store()
        return self._analysis_features

    def track_vectors(self) -> dict:
        """Every analysed track's unit embedding, cached until the store changes.

        ~5 KB per track, which is the price of being able to ask "what sounds like
        this" locally. Reloaded only when analysis.db is written to.
        """
        store = self.analysis_store()
        if store is None:
            return {}
        from .. import analysis

        try:
            stamp = analysis.store_path().stat().st_mtime
        except OSError:
            return {}
        if self._vectors_at != stamp:
            self._vectors = analysis.vector_map(store)
            self._vectors_at = stamp
        return self._vectors

    def sound_line(self, names: list[str]) -> str:
        """One compact line on how the shelf sounds, or "" when analysis is off."""
        if not names or self.analysis_store() is None:
            return ""
        from .. import analysis

        try:
            return analysis.shelf_sound(self._analysis_conn, names)
        except Exception:
            return ""

    def _audio_artists(self, name: str, k: int) -> list[str]:
        store = self.analysis_store()
        if store is None:
            return []
        from .. import analysis

        try:
            return analysis.nearest_artists(store, name, k=k)
        except Exception:
            return []

    def rows_on_station(self, rows: list, station: config.Station | None) -> list:
        """Drop whatever the station's rules would not let on air.

        Every tool result goes through this. The block was copy-pasted into five
        handlers, and the branch that missed it handed the DJ a track the station
        cannot play.
        """
        if station is None:
            return rows
        ok = navidrome.station_matcher(self.conn, station)
        return [r for r in rows if ok(r)]

    def search(self, query: str, limit: int = 20, station: config.Station | None = None, **_):
        """Tracks whose name matches the query, or what the library has that is close.

        Two things used to make this a dead end. The query had to appear as one
        substring in one column, so "Ride Vapour Trail" — an artist and a title, the
        way anybody would write it — matched nothing; and a name that simply is not in
        the library came back as an empty list, which reads the same as a typo, so the
        DJ would try the next name it remembered and lose the round.
        """
        text = html.unescape(str(query or "")).strip()
        limit = clamp(limit, 20, 200)
        tokens = [t for t in text.split() if t]
        if not tokens:
            return {"results": [], "note": "empty query"}

        # Every word has to land somewhere — one column, any column. "Ride Vapour
        # Trail" is two fields of the same record, which is how people name records.
        rows = track_queries.search(self.conn, text, tokens, limit * 40)

        on_station = self.rows_on_station(rows, station)
        if on_station:
            ctx = self._context(station)
            return self._visible(on_station[:limit], ctx)
        return self._search_miss(text, station)

    def _search_miss(self, text: str, station) -> dict:
        """A name that plays nowhere here, answered with names, never with a track.

        What the model wanted was a sound, not a spelling: "Parcels" is a way of asking
        for what Parcels sound like. So the miss asks the same similar-artist machinery
        `similar` uses and keeps only the artists this station plays — at most three,
        artist names only.

        Two things it deliberately does not do. It does not hand back a track id: that
        turned every miss into a pick, and because the acts that sound like the model's
        favourite French-touch names on this shelf are the three it owns, a night of
        asking for that scene fed the same three straight into the sets. And it does not
        offer acts that have just been on air: when everything similar is recent, the
        honest answer is that this corner of the shelf has had its turn.
        """
        offers = self.similar_here(text, station)
        recent = self._recent_act_keys(station)
        fresh = [o for o in offers if navidrome.name_key(o["artist"]) not in recent]
        payload: dict = {"results": [], "similar_here": []}
        if fresh:
            payload["similar_here"] = [
                {"artist": o["artist"], "tracks": o["tracks"]} for o in fresh[:3]
            ]
            payload["note"] = (
                f"nothing by '{text}' plays on this station. These do, and they are what "
                "it sounds like — call catalogue on one of them to see its tracks; no ids "
                "here, the record has to be opened."
            )
        elif offers:
            payload["note"] = (
                f"nothing by '{text}' plays on this station. Something here does sound like "
                "it, but it has been on air recently — take the set somewhere else and "
                "change the temperature."
            )
        else:
            payload["note"] = (
                f"nothing by '{text}' plays on this station, and nothing here sounds much "
                "like it. Find another vibe: filter draws from the whole station, flip "
                "deals three records, catalogue knows what you own by an artist you can name."
            )
            # Nothing similar at all: the only useful thing left is a spelling correction
            # for a name that does exist.
            payload["did_you_mean"] = self.nearest(text, station)
        return payload

    def similar_here(self, name: str, station: config.Station | None = None,
                     limit: int = 6) -> list[dict]:
        """Acts this station plays that sound like `name`, with a track to start from.

        The list comes from the same place `similar` gets it — cache, ListenBrainz,
        Last.fm, then the local genre as a last resort — but nothing here requires
        owning a note by the artist that was asked for.
        """
        from .. import enrich

        rows = self._station_rows(station)
        by_act: dict[str, list] = {}
        for row in rows:
            by_act.setdefault(navidrome.name_key(row["album_artist"] or row["artist"]), []).append(row)
        recent = self._recent_act_keys(station)

        def gather(names: list[str]) -> list[dict]:
            found = []
            for candidate in names:
                hits = by_act.get(navidrome.name_key(candidate))
                if not hits:
                    continue
                # The same track was handed out on every miss — "Là-haut" for
                # L'Impératrice, "Like Eating Glass" for Bloc Party — because the lead
                # was the first row in table order. Draw it from this pick's seed.
                lead = sorted(hits, key=self._shuffle_key)[0]
                found.append({
                    "artist": lead["album_artist"] or lead["artist"],
                    "tracks": len(hits),
                    "id": lead["id"],
                    "title": lead["title"],
                    "album": lead["album"],
                    "year": lead["year"],
                })
            found.sort(key=lambda d: (navidrome.name_key(str(d["artist"])) in recent,
                                      -d["tracks"], str(d["artist"]).casefold()))
            return found

        names, _source = enrich.similar_artists(self.conn, name, limit=25)
        out = gather(names)
        if len(out) < 3:
            # A cached or short list is twenty names, and on a library of sixty artists
            # the artist that plays here is often the twenty-first. Ask for a hundred.
            wider = enrich.lfm_similar(name, 100)
            fresh = [n for n in wider if n not in names]
            if fresh:
                more = gather(fresh)
                seen = {d["artist"] for d in out}
                out += [d for d in more if d["artist"] not in seen]
                out.sort(key=lambda d: (navidrome.name_key(str(d["artist"])) in recent,
                                        -d["tracks"], str(d["artist"]).casefold()))
        return out[:limit]

    def nearest(self, text: str, station: config.Station | None = None, limit: int = 5) -> list[dict]:
        """The closest names this station can actually play.

        Folded first, so an ASCII apostrophe or a missing accent still reaches the
        record, and a name contained in another counts as close — "Parcels" should
        find "The Parcels" whatever the ratio says. Anything the station cannot play
        is left out: a suggestion that leads to another dead end is worse than none.
        """
        key = navidrome.name_key(text)
        if not key:
            return []

        # Acts only. Album titles were tried too and produced noise: "Parcels" reached
        # "Parachutes", "Jungle" reached "Singles" — matches on shape, not on sense.
        scored = []
        for name in track_queries.album_artist_names(self.conn):
            folded = navidrome.name_key(name)
            if not folded:
                continue
            ratio = difflib.SequenceMatcher(None, key, folded).ratio()
            if key in folded or folded in key:
                ratio = max(ratio, 0.85)
            if ratio >= 0.6:
                scored.append((ratio, name))
        scored.sort(reverse=True)

        out: list[dict] = []
        for _, name in scored:
            rows = track_queries.tracks_for_album_artist(self.conn, name)
            playable = self.rows_on_station(rows, station) if station else rows
            if not playable:
                continue
            out.append({"name": name, "tracks": len(playable)})
            if len(out) >= limit:
                break
        return out

    def filter(self, station: config.Station | None = None, **kw) -> dict:
        """A draw from the station, optionally narrowed, with the size of the slice.

        Built on the station's own rows rather than querying the table again: the
        station rules are the expensive part and they have already been applied, so a
        narrower question only needs to narrow the list it is given.

        `genre` matches the library's own labels, not the operator's station rollup, and
        artists that aired in the last twenty tracks sort last — a genre word used to be
        answered with the whole shelf an alias filed it under, and the page always opened
        on whoever had just played.
        """
        rows = self._station_rows(station)
        if kw.get("year_from"):
            rows = [r for r in rows if (r["year"] or 0) >= int(kw["year_from"])]
        if kw.get("year_to"):
            rows = [r for r in rows if (r["year"] or 0) <= int(kw["year_to"])]

        # Genre goes through the same fold/refinement the stations use, so
        # "Punk" finds "Punk Rock" here exactly as it does on air.
        want = kw.get("genre")
        if want:
            # Fine labels, not the operator's station rollup: "Nu Disco" answers the
            # nu-disco shelf, not everything the station files under Electronic.
            target = genres_module.targets_fine([want])
            by_track = navidrome.labels_by_track(self.conn)
            rows = [r for r in rows if genres_module.matches(by_track.get(r["id"], ()), target)]

        # A shuffle keyed to this pick, not to this call. `offset` can only mean
        # "the next page" if the order holds still between the two calls, so the
        # seed is drawn once per pick — every call within one decision pages the
        # same list, and the next pick gets a fresh order. Artists that aired
        # recently sort behind everyone else, deterministically, so paging stays
        # stable within the pick.
        recent = self._recent_act_keys(station)
        rows = sorted(rows, key=lambda r: (self._act_key(r) in recent, self._shuffle_key(r)))
        total = len(rows)

        limit = clamp(kw.get("limit"), 30, 200)
        offset = max(0, int(kw.get("offset") or 0))
        # Spread the page across artists. A random draw over a label with one big name in
        # it hands back five tracks by the same band and looks like variety; three is
        # enough to show a record exists without letting it fill the page.
        seen: Counter = Counter()
        picked = []
        for row in rows[offset:]:
            who = row["album_artist"] or row["artist"]
            if seen[who] >= PER_ARTIST:
                continue
            seen[who] += 1
            picked.append(row)
            if len(picked) >= limit:
                break
        ctx = self._context(station)
        visible = self._visible(picked, ctx)
        return {
            "matching": total,
            "offset": offset,
            "returned": len(visible),
            "next_offset": offset + len(visible),
            "results": visible,
        }

    def roster(self, station: config.Station | None = None, limit: int = 60) -> dict:
        """Who and what this station holds, in the shape the system prompt wants.

        The numbers the DJ needs before it can choose anything: what this station is
        made of, in the only terms that matter to it — labels with weight, and every artist
        it can play. Two tools used to answer this a page at a time; a prompt can simply
        state it, and a stated fact costs no rounds.
        """
        rows = self._station_rows(station)

        artists: Counter = Counter()
        for row in rows:
            artists[(row["album_artist"] or row["artist"]) or "?"] += 1
        labels: Counter = Counter()
        by_track = navidrome.labels_by_track(self.conn)
        for row in rows:
            for label in by_track.get(row["id"], ()):
                labels[label] += 1

        return {
            "tracks": len(rows),
            "artists": artists.most_common(limit),
            "artists_total": len(artists),
            "labels": labels.most_common(limit),
            "labels_total": len(labels),
        }

    # Three records in the pile, five tracks visible per record. A DJ flipping crates
    # sees covers and a handful of titles, not a catalogue — and three is enough to
    # offer directions without turning the brief into a list.
    CRATE_ALBUMS = 3
    CRATE_TRACKS = 5

    def _shuffle_key(self, row: sqlite3.Row):
        seed = self.draw_seed or 0
        return hashlib.blake2b(f"{seed}|{row['id']}".encode(), digest_size=8).digest()

    def _act_key(self, row) -> str:
        """The folded identity of the record's artist, for recency comparisons."""
        return navidrome.name_key(row["album_artist"] or row["artist"])

    def _recent_act_keys(self, station: config.Station | None, window: int = 20) -> set[str]:
        """Artists aired in the last `window` plays, folded.

        Tools demote these; they never exclude them. Excluding would let a tool answer
        "nothing here" about music the station holds, and an empty page sends the model
        somewhere worse than a repeated artist does.
        """
        if station is None:
            return set()
        return {navidrome.name_key(a) for a in self.recent_labels(station, window)[1] if a}

    def popularity_ranks(self, rows: list) -> dict[str, float]:
        """Each track's standing among its own artist's tracks, 0 = the deep cut.

        Absolute play counts only say how big the artist is; the question in front of the
        DJ is which of their songs people actually know.
        """
        from .. import pool

        return pool._popularity_percentiles(self.conn, rows)

    def _remember_dealt(self, albums: list[str]) -> None:
        """Track which records are already in front of the DJ this pick."""
        if getattr(self, "_dealt_seed", None) != self.draw_seed:
            self._dealt_seed = self.draw_seed
            self._dealt_albums = set()
        self._dealt_albums |= set(albums)

    def crate(self, station: config.Station | None = None, *, size: int | None = None,
              tracks_shown: int | None = None, avoid_acts: set[str] | None = None) -> list[dict]:
        """A handful of records pulled out at random, the way you'd flip a crate.

        Nothing here is curated: it is the pile, and the DJ may take one or ignore all
        of them. Acts it has just played are left out, and each album comes from a
        different artist, so the five are five directions rather than five De Lux records.
        """
        rows = self._station_rows(station)
        if not rows:
            return []

        size = size or self.CRATE_ALBUMS
        tracks_shown = tracks_shown or self.CRATE_TRACKS
        skip = {navidrome.name_key(a) for a in (avoid_acts or set()) if a}
        self._crate_skip = set(avoid_acts or set())
        # Never deal the same record twice in one pick: a flip is a new pile, not the
        # same three covers with one swapped.
        if getattr(self, "_dealt_seed", None) != self.draw_seed:
            self._dealt_seed = self.draw_seed
            self._dealt_albums = set()
        dealt = set(self._dealt_albums)

        by_album: dict[str, list] = {}
        for row in rows:
            if row["album_id"] in dealt:
                continue
            if navidrome.name_key(row["album_artist"] or row["artist"]) in skip:
                continue
            by_album.setdefault(row["album_id"], []).append(row)
        if not by_album:
            return []

        # One album per artist, then take the artists at random: a crate of five, not a crate
        # of five records by whoever owns the most of the library.
        by_act: dict[str, list[str]] = {}
        for album_id, tracks in by_album.items():
            by_act.setdefault(navidrome.name_key(tracks[0]["album_artist"] or tracks[0]["artist"]), []).append(album_id)
        artists = list(by_act)
        random.shuffle(artists)

        picks: list[str] = []
        for artist in artists:
            picks.append(random.choice(by_act[artist]))
            if len(picks) >= size:
                break
        self._remember_dealt(picks)

        labels = navidrome.labels_by_track(self.conn)
        ranks = self.popularity_ranks([r for album in picks for r in by_album[album]])
        # How many tracks in the library carry each genre: the rarer the genre, the
        # more specific it is about this record. "Stoner Rock" says something about a
        # Sahara Surfers album that "Rock" does not.
        breadth = {
            row["label"]: row["n"]
            for row in self.conn.execute(
                "SELECT label, count(*) AS n FROM track_genres GROUP BY label"
            )
        }

        out: list[dict] = []
        for album_id in picks:
            tracks = sorted(by_album[album_id], key=lambda r: (r["track_no"] or 0, r["title"] or ""))
            lead = tracks[0]
            # Every genre the record carries, narrowest first — a record with four
            # genres is not one genre, and picking one of them was a coin toss.
            album_genres = set()
            for track in tracks:
                album_genres.update(labels.get(track["id"], ()))
            # The best-known song on the record, not its median: a median over one
            # album's own tracks is ~0.5 by construction and says nothing.
            known = max((ranks.get(t["id"], 0.0) for t in tracks), default=0.0)
            out.append({
                "album": lead["album"],
                "artist": lead["album_artist"] or lead["artist"],
                "year": lead["year"],
                "genre": " / ".join(
                    sorted(album_genres, key=lambda g: (breadth.get(g, 0), g))[:4]
                ),
                "known": known,
                "tracks": [
                    {
                        "id": t["id"],
                        "title": t["title"],
                        "no": t["track_no"],
                        "duration": self._clock(t["duration"]),
                        "popularity": round(ranks.get(t["id"], 0.5), 2),
                    }
                    for t in tracks[:tracks_shown]
                ],
                "total": len(tracks),
            })
        return out

    def recent_labels(self, station: config.Station, n: int = 5) -> tuple[list[str], list[str]]:
        """The labels and artists of the last few songs, newest first.

        This is the evidence a DJ would use to notice he has been in one corner all
        night: not what he meant to play, what actually went out.
        """
        plays = queue.recent(station, n)
        labels = navidrome.labels_by_track(self.conn)
        artists: list[str] = []
        counted: Counter = Counter()
        for entry in plays:
            track_id = entry.get("id") or ""
            row = track_queries.track_by_id(self.conn, track_id)
            if row is not None:
                artists.append(row["album_artist"] or row["artist"] or "")
                for label in labels.get(track_id, ()):
                    counted[label] += 1
            elif entry.get("artist"):
                artists.append(entry["artist"])
        return [label for label, _ in counted.most_common(3)], artists

    def by_id(self, track_id: str) -> navidrome.Track | None:
        row = track_queries.track_by_id(self.conn, track_id)
        return self._full(row) if row else None

    def count(self, station: config.Station) -> int:
        return len(navidrome.rows_for_station(self.conn, station))

    def on_station(self, station: config.Station, track_id: str) -> bool:
        """Whether this station's own rules let that track play.

        A station whose rules match nothing is on air with the whole library (see
        rows_for_station), so its picks are allowed the same way — otherwise a typo
        in a genre list would leave the DJ unable to answer at all.
        """
        row = track_queries.track_by_id(self.conn, track_id)
        if row is None:
            return False
        ok = navidrome.station_matcher(self.conn, station)
        if ok(row):
            return True
        return not any(ok(r) for r in track_queries.playable_tracks(self.conn))

    def similar(self, artist: str, limit: int = 12, station: config.Station | None = None) -> dict:
        """Tracks by artists that sound like this one.

        Four levels, cheapest first, so this answers even on a library with no
        MusicBrainz ids and no API key:
          1. cached similar-artist list
          2. ListenBrainz via an artist MBID (Navidrome or file tags)
          3. Last.fm by artist NAME (needs a key, needs no ids at all)
          4. local genre: other artists sharing this artist's dominant genre
        """
        limit = clamp(limit, 12, 50)
        from .. import enrich

        ctx = self._context(station)
        recent = self._recent_act_keys(station)

        # Audio first when it exists: it is local, free, and it can only name artists
        # this library owns — the provider chains below happily name ones it does not.
        audio_names = self._audio_artists(artist, k=12)
        if audio_names:
            keys = [navidrome.name_key(name) for name in audio_names]
            rows = track_queries.tracks_by_artist_keys(self.conn, keys)
            rows = self.rows_on_station(rows, station)
            if rows:
                rows.sort(key=lambda r: self._act_key(r) in recent)
                return {
                    "source": "audio",
                    "artists": audio_names[:10],
                    "tracks": [self._row(r, ctx) for r in rows[: int(limit)]],
                    "owned": True,
                }

        names, source = enrich.similar_artists(self.conn, artist, limit=20)
        if names:
            keys = [navidrome.name_key(name) for name in names]
            # Match the record's artist, not the track credit: an album artist's
            # tracks with guests are still that artist.
            rows = track_queries.tracks_by_artist_keys(self.conn, keys, random_order=True)
            rows = self.rows_on_station(rows, station)
            # Stable sort: the artist that played ten minutes ago goes to the back,
            # the random order survives within each group.
            rows.sort(key=lambda r: self._act_key(r) in recent)
            rows = rows[: int(limit)]
            # Names found but nothing owned is a real answer: report the similar
            # artists and an empty track list rather than pretending we found none.
            return {
                "source": source,
                "artists": names[:10],
                "tracks": [self._row(r, ctx) for r in rows],
                "owned": bool(rows),
            }

        # Grouped by the record's artist, so "L’Impératrice & Fabiana Martone" shares
        # the band's dominant genre instead of having none. Matched on the folded key,
        # because the caller types the name.
        key = navidrome.name_key(artist)
        genre = track_queries.dominant_genre(self.conn, key)
        if genre:
            # The one branch that had no station filter at all, so it could hand the
            # DJ a track the station is not allowed to play.
            rows = track_queries.tracks_in_genre_excluding_artist(self.conn, genre, key)
            rows = self.rows_on_station(rows, station)
            rows.sort(key=lambda r: self._act_key(r) in recent)
            rows = rows[: int(limit)]
            if rows:
                return {
                    "source": "genre",
                    "genre": genre,
                    "artists": sorted({
                        (r["album_artist"] or r["artist"]) for r in rows
                    })[:10],
                    "tracks": [self._row(r, ctx) for r in rows],
                }
        return {"source": "none", "artists": [], "tracks": []}

    def flip(self, station: config.Station | None = None, **_) -> dict:
        """Three more records from the racks, never ones already dealt this pick.

        The DJ keeps choosing from ids it was handed rather than going looking, so
        "nothing here fits" needed an answer that produces ids: another three covers,
        and another three after that.
        """
        _, recent_acts = self.recent_labels(station, 6)
        albums = self.crate(station, avoid_acts=set(recent_acts))
        if not albums:
            return {
                "albums": [],
                "note": "the racks ran out for this pick — every record has been dealt "
                        "or is by an artist already in play. Use filter or search instead.",
            }
        return {"albums": albums}

    def history(self, station: config.Station, limit: int = 20, **_) -> list[dict]:
        """The station's recent past, newest first, with how long ago each played."""
        limit = clamp(limit, 20, 100)
        plays = list(reversed(queue.recent(station, limit)))
        ctx = self._context(station)
        out: list[dict] = []
        for ago, entry in enumerate(plays):
            track_id = entry.get("id") or ""
            row = track_queries.track_by_id(self.conn, track_id)
            if row is not None:
                item = self._row(row, ctx)
            else:
                # Played, but no longer in the index (an album was removed since).
                item = {
                    "id": track_id,
                    "title": entry.get("title") or "",
                    "artist": entry.get("artist") or "",
                    "album": "", "year": None, "genre": "", "duration": "",
                    "popularity": 0.5, "played": True, "queued": False,
                }
            item["songs_ago"] = ago
            item["on_air"] = ago == 0
            out.append(item)
        return self._fit_rows(out)

    def catalogue(
        self,
        artist: str | None = None,
        album: str | None = None,
        limit: int = 25,
        station: config.Station | None = None,
        **_,
    ) -> dict:
        """One artist's holdings, or one album in running order.

        An album name alone is enough when only one artist owns it; if several do,
        the one with most tracks on this station wins and the reply says so.

        Albums come back in this pick's order, tracks in running order inside each —
        alphabetical order meant every browse of an artist opened on the same record.
        """
        limit = clamp(limit, 25, 100)
        rows = track_queries.catalogue(self.conn, artist, album)
        if rows is None:
            return {"error": "give an artist, an album, or both"}
        rows = self.rows_on_station(rows, station)
        if not rows:
            return {
                "artist": artist or "",
                "album": album or "",
                "tracks": [],
                "note": "this station holds nothing under that name",
            }

        # An album name shared by several artists: keep the one with the most tracks.
        if album and not artist:
            counts: dict[str, int] = {}
            for row in rows:
                key = (row["album_artist"] or row["artist"])
                counts[key] = counts.get(key, 0) + 1
            artist = max(counts, key=counts.get)
            rows = [r for r in rows if (r["album_artist"] or r["artist"]) == artist]

        ctx = self._context(station)
        if not album:
            # Albums in this pick's order, tracks still in running order within each
            # album: browsing an artist used to open on the same records every time —
            # alphabetical order is a favourite in disguise.
            grouped: dict[str, list] = {}
            for row in rows:
                grouped.setdefault(row["album_id"] or row["album"], []).append(row)
            ordered = sorted(grouped.values(), key=lambda rs: self._shuffle_key(rs[0]))
            rows = [r for rs in ordered for r in rs]

        albums: dict[str, dict] = {}
        kept = 0
        played_recently = False
        for row in rows[:limit]:
            item = self._lean_row(row, ctx)
            played_recently = played_recently or item["played"]
            key = row["album"] or ""
            block = albums.setdefault(
                key,
                {
                    "album": key,
                    "year": row["year"],
                    "genre": " / ".join((ctx["labels"].get(row["id"]) or [])[:3]),
                    "tracks": [],
                },
            )
            block["tracks"].append(item)
            kept += 1

        out = {
            "artist": rows[0]["album_artist"] or rows[0]["artist"],
            "album": album or "",
            "albums": list(albums.values()),
            "shown": kept,
            "total": len(rows),
            "played_recently": played_recently,
        }
        if kept < len(rows):
            out["note"] = (
                f"showing {kept} of {len(rows)} tracks; ask for the album you want to see "
                "the rest"
            )
        return out

    def run_tool(self, name: str, args: dict, station: config.Station):
        """Execute one tool call, turning bad arguments into something the model can read."""
        handler = HANDLERS.get(name)
        if handler is None:
            return {"error": f"no such tool: {name}"}
        try:
            return handler(self, station=station, **args)
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc}"}
        except Exception as exc:  # a tool must never take the pick down with it
            return {"error": f"{name} failed: {exc}"}

    def _station_rows(self, station: config.Station | None) -> list:
        """The station's playable rows, remembered for a few seconds.

        `rows_for_station` runs the station's rules over every track — 47 ms on this
        library — and a single pick asks for it in every tool call. The rules only
        change on a rebuild, so a short memo turns four calls into one without ever
        serving a stale index for long.
        """
        if station is None:
            return track_queries.playable_tracks(self.conn)
        cached = getattr(self, "_rows_cache", None)
        now = time.monotonic()
        if cached and cached[0] is station and now - cached[1] < 5.0:
            return cached[2]
        rows = navidrome.rows_for_station(self.conn, station)
        self._rows_cache = (station, now, rows)
        return rows

    def _context(self, station: config.Station | None) -> dict:
        """Everything a track row needs, gathered once per call rather than per row.

        Genres, how well known each track is for its artist, whether it should come
        round again yet, and whether it is already queued — the four things the DJ needs
        to judge a row, and the four things only some tools used to carry.
        """
        rows = self._station_rows(station)
        played = queue.recently_out(station) if station else set()
        try:
            queued = set(mixer.queued_ids(station)) if station else set()
        except Exception:
            queued = set()
        return {
            "labels": navidrome.labels_by_track(self.conn),
            "ranks": self.popularity_ranks(rows),
            "played": played,
            "queued": queued,
            "analysis": self._features(),
        }

    @staticmethod
    def _clock(seconds: int | None) -> str:
        total = int(seconds or 0)
        return f"{total // 60}:{total % 60:02d}" if total else ""

    def _row(self, row: sqlite3.Row, ctx: dict) -> dict:
        """The row every tool returns, so the model never has to guess what is missing.

        The record's artist, not the track credit: the DJ reasons about artists, and
        "L’Impératrice & Fabiana Martone" is L’Impératrice playing with a guest.
        """
        keys = row.keys()
        genres = ctx["labels"].get(row["id"]) or ([row["genre"]] if row["genre"] else [])
        item = {
            "id": row["id"],
            "title": row["title"],
            "artist": (row["album_artist"] if "album_artist" in keys else "") or row["artist"],
            "album": row["album"],
            "year": row["year"],
            "genre": " / ".join(genres),
            "duration": self._clock(row["duration"] if "duration" in keys else 0),
            "popularity": round(ctx["ranks"].get(row["id"], 0.5), 2),
            "played": row["id"] in ctx["played"],
            "queued": row["id"] in ctx["queued"],
        }
        # Only when audio analysis has this track: an installation without it gets
        # exactly the rows it always got.
        hit = ctx.get("analysis", {}).get(row["id"])
        if hit:
            if hit.get("bpm"):
                item["tempo"] = int(round(hit["bpm"]))
            if hit.get("key"):
                item["key"] = hit["key"]
            if hit.get("sound"):
                item["sound"] = hit["sound"]
            if hit.get("energy") is not None:
                item["energy"] = round(float(hit["energy"]), 2)
        return item

    def _visible(self, rows: list, ctx: dict) -> list[dict]:
        """The rows of a page that fit the budget, so the count beside them is true."""
        return self._fit_rows([self._row(r, ctx) for r in rows])

    @staticmethod
    def _fit_rows(items: list[dict], budget: int = ROW_BUDGET) -> list[dict]:
        """As many whole rows as fit — never a slice that leaves half a row behind."""
        out: list[dict] = []
        used = 0
        for item in items:
            size = len(json.dumps(item, ensure_ascii=False)) + 2
            if out and used + size > budget:
                break
            out.append(item)
            used += size
        return out

    def _lean_row(self, row: sqlite3.Row, ctx: dict) -> dict:
        """One track inside an album block: enough to choose it, nothing the album says.

        The album carries its own name, artist, year and genre, so repeating those on
        every track made the payload twice the size for half the information — and a
        `catalogue` answer was two views of the same tracks, which is what the trimmer
        then cut in the wrong place.
        """
        return {
            "id": row["id"],
            "title": row["title"],
            "track_no": row["track_no"],
            "duration": self._clock(row["duration"]),
            "popularity": round(ctx["ranks"].get(row["id"], 0.5), 2),
            "played": row["id"] in ctx["played"],
        }

    @staticmethod
    def _full(row: sqlite3.Row) -> navidrome.Track:
        return navidrome.Track(
            id=row["id"],
            uri=loudness.annotate(row["uri"], loudness.for_row(row, loudness.measured(row["id"]))),
            title=row["title"],
            artist=row["artist"],
            album_artist=(row["album_artist"] if "album_artist" in row.keys() else "") or row["artist"],
            album=row["album"],
            album_id=row["album_id"],
            track_no=row["track_no"],
            year=row["year"],
            genre=row["genre"],
            duration=row["duration"],
            mbz_album=row["mbz_album"] or "",
            mbz_recording=row["mbz_recording"] or "",
        )


# Tool name as the model sees it -> the method that answers it. Checked against the
# schemas at import, because a tool the model can call with nothing behind it used to
# be a runtime "no such tool" that cost a round trip and read like a library problem.
HANDLERS = {
    "history": Library.history,
    "catalogue": Library.catalogue,
    "similar": Library.similar,
    "flip": Library.flip,
    "search": Library.search,
    "filter": Library.filter,
}
_offered = {t["function"]["name"] for t in TOOLS}
if set(HANDLERS) != _offered:
    raise RuntimeError(
        f"tool schemas and handlers disagree: {sorted(_offered ^ set(HANDLERS))}"
    )
