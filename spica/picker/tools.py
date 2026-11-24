"""The picker's tool implementations.

Each tool reads the index through the Library, shapes rows through `picker.rows`, and
returns plain dicts the model can read. Dispatch lives here so the Library stays a
lookup facade.
"""

from __future__ import annotations

import difflib
import html
import random
import sqlite3
from collections import Counter

from .. import config, ledger, mixer, navidrome, queue
from .. import genres as genres_module
from ..names import seed_key
from ..queries import labels as label_queries
from ..queries import tracks as track_queries
from .memory import recent_artists
from .rows import clock, fit_rows, full, lean_row, visible
from .rows import row as render_row
from .schemas import TOOLS

PER_ARTIST = 3
SAMPLE_ALBUMS = 3
SAMPLE_TRACKS = 5


def clamp(value, default: int, high: int) -> int:
    """A limit the model chose, kept inside sensible bounds."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, high))


class Tools:
    def __init__(self, lib) -> None:
        self.lib = lib
        self._sampled_seed = None
        self._sampled_albums: set[str] = set()

    def search(self, query: str, limit: int = 20, station: config.Station | None = None, **_):
        """Tracks whose name matches the query, or what the library has that is close.

        Every word has to land somewhere, in any column, so "Beach House Space Song" — an
        artist and a title, the way anybody would write it — finds the record. A name
        that simply is not in the library answers with what does sound like it: an
        empty list reads the same as a typo and sends the picker to the next remembered name.
        """
        text = html.unescape(str(query or "")).strip()
        limit = clamp(limit, 20, 200)
        tokens = [t for t in text.split() if t]
        if not tokens:
            return {"results": [], "note": "empty query"}

        # Every word has to land somewhere — one column, any column. "Ride Vapour
        # Song" is two fields of the same record, which is how people name records.
        rows = track_queries.search(self.lib.conn, text, tokens, limit * 40)

        on_station = self.lib.rows_on_station(rows, station)
        if on_station:
            ctx = self._context(station)
            return visible(on_station[:limit], ctx)
        return self._search_miss(text, station)

    def _search_miss(self, text: str, station) -> dict:
        """A name that plays nowhere here, answered with names, never with a track.

        What the model wanted was a sound, not a spelling: "Parcels" is a way of asking
        for what Parcels sound like. So the miss asks the same similar-artist machinery
        `similar` uses and keeps only the artists this station plays — at most three,
        artist names only.

        It never hands back a track id: a miss is a route, not a pick. And it does not
        offer acts that have just been on air; when everything similar is recent, the
        honest answer is that this corner of the station has had its turn.
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
                "like it. Find another vibe: filter draws from the whole station, redraw "
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

        rows = self.lib._station_rows(station)
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
                # was the first row in candidate order. Draw it from this pick's seed.
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

        names, _source = enrich.similar_artists(self.lib.conn, name, limit=25)
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

        # Acts only: matching album titles admits shape, not sense — "Parcels" reaches
        # "Parachutes", "Jungle" reaches "Singles".
        scored = []
        for name in track_queries.album_artist_names(self.lib.conn):
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
            rows = track_queries.tracks_for_album_artist(self.lib.conn, name)
            playable = self.lib.rows_on_station(rows, station) if station else rows
            if not playable:
                continue
            out.append({"name": name, "tracks": len(playable)})
            if len(out) >= limit:
                break
        return out

    def filter(self, station: config.Station | None = None, **kw) -> dict:
        """A draw from the station, optionally narrowed, with the size of the slice.

        Built on the station's own rows rather than querying the index again: the
        station rules are the expensive part and they have already been applied, so a
        narrower question only needs to narrow the list it is given.

        `genre` matches the library's own labels, not the operator's station rollup, and
        artists that aired in the last twenty tracks sort last, so a page does not open on
        whoever just played.
        """
        rows = self.lib._station_rows(station)
        if kw.get("year_from"):
            rows = [r for r in rows if (r["year"] or 0) >= int(kw["year_from"])]
        if kw.get("year_to"):
            rows = [r for r in rows if (r["year"] or 0) <= int(kw["year_to"])]

        # Genre goes through the same fold/refinement the stations use, so
        # "Punk" finds "Punk Rock" here exactly as it does on air.
        want = kw.get("genre")
        if want:
            # Fine labels, not the operator's station rollup: "Nu Disco" answers the
            # nu-disco labels, not everything the station files under Electronic.
            target = genres_module.targets_fine([want])
            by_track = self.lib.labels_by_track()
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
        shown = visible(picked, ctx)
        return {
            "matching": total,
            "offset": offset,
            "returned": len(shown),
            "next_offset": offset + len(shown),
            "results": shown,
        }

    def catalog_summary(self, station: config.Station | None = None, limit: int = 60) -> dict:
        """What this station holds, in the shape the system prompt wants.

        The numbers the picker needs before it can choose: labels with weight and every
        artist it can play. A prompt can state this, so it costs no tool rounds.
        """
        rows = self.lib._station_rows(station)

        artists: Counter = Counter()
        for row in rows:
            artists[(row["album_artist"] or row["artist"]) or "?"] += 1
        labels: Counter = Counter()
        by_track = self.lib.labels_by_track()
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

    # Three records in the pile, five tracks visible per record: enough to show
    # directions without turning the prompt into a catalogue.
    SAMPLE_ALBUMS = 3
    SAMPLE_TRACKS = 5

    def _shuffle_key(self, row: sqlite3.Row):
        return seed_key(self.lib.draw_seed or 0, row["id"])

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
        """Each track's standing among its own artist's tracks, 0 = the least known.

        Absolute play counts only say how big the artist is; the question in front of the
        picker is which of their songs people actually know.
        """
        from .. import pool

        return pool._popularity_percentiles(self.lib.conn, rows)

    def _remember_sampled(self, albums: list[str]) -> None:
        """Remember which records have already been sampled this pick."""
        if getattr(self, "_sampled_seed", None) != self.lib.draw_seed:
            self._sampled_seed = self.lib.draw_seed
            self._sampled_albums = set()
        self._sampled_albums |= set(albums)

    def sample_albums(self, station: config.Station | None = None, *, size: int | None = None,
              tracks_shown: int | None = None, avoid_acts: set[str] | None = None) -> list[dict]:
        """A handful of records sampled at random.

        Nothing here is curated: it is the pile, and the picker may take one or ignore all
        of them. Acts it has just played are left out, and each album comes from a
        different artist, so the five are five directions rather than five De Lux records.
        """
        rows = self.lib._station_rows(station)
        if not rows:
            return []

        size = size or SAMPLE_ALBUMS
        tracks_shown = tracks_shown or SAMPLE_TRACKS
        skip = {navidrome.name_key(a) for a in (avoid_acts or set()) if a}
        # Never sample the same record twice in one pick: a redraw is a new pile, not
        # the same three covers with one swapped.
        self._remember_sampled(())
        dealt = set(self._sampled_albums)

        by_album: dict[str, list] = {}
        for row in rows:
            if row["album_id"] in dealt:
                continue
            if navidrome.name_key(row["album_artist"] or row["artist"]) in skip:
                continue
            by_album.setdefault(row["album_id"], []).append(row)
        if not by_album:
            return []

        # One album per artist, then take the artists at random: five directions, not
        # five records by whoever owns the most of the library.
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
        self._remember_sampled(picks)

        labels = self.lib.labels_by_track()
        ranks = self.popularity_ranks([r for album in picks for r in by_album[album]])
        # How many tracks in the library carry each genre: the rarer the genre, the
        # more specific it is about this record. "Stoner Rock" says something about a
        # Sahara Surfers album that "Rock" does not.
        breadth = {row["label"]: row["c"] for row in label_queries.counts(self.lib.conn)}

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
                        "duration": clock(t["duration"]),
                        "popularity": round(ranks.get(t["id"], 0.5), 2),
                    }
                    for t in tracks[:tracks_shown]
                ],
                "total": len(tracks),
            })
        return out

    def recent_labels(self, station: config.Station, n: int = 5) -> tuple[list[str], list[str]]:
        """The labels and artists of the last few songs, oldest first.

        The evidence a picker uses to notice it has been in one corner all night: not
        what it meant to play, what actually went out.
        """
        plays = queue.recent(station, n)
        labels = self.lib.labels_by_track()
        counted: Counter = Counter()
        for entry in plays:
            for label in labels.get(entry.get("id") or "", ()):
                counted[label] += 1
        return [label for label, _ in counted.most_common(3)], recent_artists(self.lib, station, n)

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
        audio_names = self.lib._audio_artists(artist, k=12)
        if audio_names:
            keys = [navidrome.name_key(name) for name in audio_names]
            rows = track_queries.tracks_by_artist_keys(self.lib.conn, keys)
            rows = self.lib.rows_on_station(rows, station)
            if rows:
                rows.sort(key=lambda r: self._act_key(r) in recent)
                return {
                    "source": "audio",
                    "artists": audio_names[:10],
                    "tracks": [render_row(r, ctx) for r in rows[: int(limit)]],
                    "owned": True,
                }

        names, source = enrich.similar_artists(self.lib.conn, artist, limit=20)
        if names:
            keys = [navidrome.name_key(name) for name in names]
            # Match the record's artist, not the track credit: an album artist's
            # tracks with guests are still that artist.
            rows = track_queries.tracks_by_artist_keys(self.lib.conn, keys, random_order=True)
            rows = self.lib.rows_on_station(rows, station)
            # Stable sort: the artist that played ten minutes ago goes to the back,
            # the random order survives within each group.
            rows.sort(key=lambda r: self._act_key(r) in recent)
            rows = rows[: int(limit)]
            # Names found but nothing owned is a real answer: report the similar
            # artists and an empty track list rather than pretending nothing was found.
            return {
                "source": source,
                "artists": names[:10],
                "tracks": [render_row(r, ctx) for r in rows],
                "owned": bool(rows),
            }

        # Grouped by the record's artist, so "L’Impératrice & Fabiana Martone" shares
        # the band's dominant genre instead of having none. Matched on the folded key,
        # because the caller types the name.
        key = navidrome.name_key(artist)
        genre = track_queries.dominant_genre(self.lib.conn, key)
        if genre:
            # The one branch that had no station filter at all, so it could hand the
            # hand the picker a track the station is not allowed to play.
            rows = track_queries.tracks_in_genre_excluding_artist(self.lib.conn, genre, key)
            rows = self.lib.rows_on_station(rows, station)
            rows.sort(key=lambda r: self._act_key(r) in recent)
            rows = rows[: int(limit)]
            if rows:
                return {
                    "source": "genre",
                    "genre": genre,
                    "artists": sorted({
                        (r["album_artist"] or r["artist"]) for r in rows
                    })[:10],
                    "tracks": [render_row(r, ctx) for r in rows],
                }
        return {"source": "none", "artists": [], "tracks": []}

    def redraw_albums(self, station: config.Station | None = None, **_) -> dict:
        """Three more records from the racks, never ones already dealt this pick.

        The picker keeps choosing from ids it was handed rather than going looking, so
        "nothing here fits" needed an answer that produces ids: another three covers,
        and another three after that.
        """
        _, recent_acts = self.recent_labels(station, 6)
        albums = self.sample_albums(station, avoid_acts=set(recent_acts))
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
        rows = {
            row["id"]: row
            for row in track_queries.tracks_by_ids(
                self.lib.conn,
                [entry.get("id") or "" for entry in plays],
                "id, title, album_artist, artist, album, year, genre, duration",
            )
        }
        out: list[dict] = []
        for ago, entry in enumerate(plays):
            track_id = entry.get("id") or ""
            row = rows.get(track_id)
            if row is not None:
                item = render_row(row, ctx)
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
        return fit_rows(out)

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
        rows = track_queries.catalogue(self.lib.conn, artist, album)
        if rows is None:
            return {"error": "give an artist, an album, or both"}
        rows = self.lib.rows_on_station(rows, station)
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
            # album: alphabetical order always opens on the same records, which is a
            # favourite in disguise.
            grouped: dict[str, list] = {}
            for row in rows:
                grouped.setdefault(row["album_id"] or row["album"], []).append(row)
            ordered = sorted(grouped.values(), key=lambda rs: self._shuffle_key(rs[0]))
            rows = [r for rs in ordered for r in rs]

        albums: dict[str, dict] = {}
        kept = 0
        played_recently = False
        for row in rows[:limit]:
            item = lean_row(row, ctx)
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

    def _context(self, station: config.Station | None) -> dict:
        """Everything a track row needs, gathered once per call rather than per row.

        Genres, how well known each track is for its artist, whether it should come
        round again yet, and whether it is already queued: the four things every row
        should carry.
        """
        rows = self.lib._station_rows(station)
        played = ledger.recently_out(station) if station else set()
        try:
            queued = set(mixer.queued_ids(station)) if station else set()
        except Exception:
            queued = set()
        return {
            "labels": self.lib.labels_by_track(),
            "ranks": self.popularity_ranks(rows),
            "played": played,
            "queued": queued,
            "analysis": self.lib._features(),
        }

# Tool name as the model sees it -> the method that answers it.
HANDLERS = {
    "history": Tools.history,
    "catalogue": Tools.catalogue,
    "similar": Tools.similar,
    "redraw": Tools.redraw_albums,
    "search": Tools.search,
    "filter": Tools.filter,
}
_tool_names = {t["function"]["name"] for t in TOOLS}
if set(HANDLERS) != _tool_names:
    raise RuntimeError(
        f"tool schemas and handlers disagree: {sorted(_tool_names ^ set(HANDLERS))}"
    )
