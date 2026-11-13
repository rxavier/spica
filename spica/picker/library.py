"""The query layer the tools sit on.

Six read-only questions over the index — search, filter, catalogue, similar,
history, station map — and the one place a tool call is dispatched. Nothing here
talks to the network; the tools only ever read what the rebuild wrote.
"""

from __future__ import annotations

import difflib
import hashlib
import html
import random
import sqlite3
import time
from collections import Counter

from .. import config, mixer, navidrome, queue
from .. import genres as genres_module
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


# How many tracks one act may contribute to a page of `filter` results.
PER_ARTIST = 3


class Library:
    # Set by the loop once per pick; a standalone call gets its own throwaway seed.
    draw_seed: int = 0

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

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

    SEARCH_COLUMNS = "title LIKE ? OR artist LIKE ? OR album LIKE ? OR album_artist LIKE ? OR album_artist_key LIKE ?"

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
        where = ["uri != ''"]
        args: list = []
        for token in tokens:
            where.append("(" + self.SEARCH_COLUMNS + ")")
            args += self._search_args(token)
        rows = self.conn.execute(
            "SELECT * FROM tracks WHERE " + " AND ".join(where)
            + " ORDER BY album_artist, album, track_no LIMIT ?",
            (*args, limit * 40),
        ).fetchall()
        if not rows and len(tokens) > 1:
            # The words may be scattered rather than all present; fall back to the
            # whole string, which is what the tool did before tokens.
            rows = self.conn.execute(
                "SELECT * FROM tracks WHERE " + self.SEARCH_COLUMNS
                + " ORDER BY album_artist, album, track_no LIMIT ?",
                (*self._search_args(text), limit * 40),
            ).fetchall()

        on_station = self.rows_on_station(rows, station)
        if on_station:
            ctx = self._context(station)
            return [self._row(r, ctx) for r in on_station[:limit]]
        return self._search_miss(text, station)

    def _search_args(self, token: str) -> list:
        like = f"%{token}%"
        return [like, like, like, like, f"%{navidrome.name_key(token)}%"]

    def _search_miss(self, text: str, station) -> dict:
        """A name that plays nowhere here, answered with the acts that do.

        What the model wanted was a sound, not a spelling: "Parcels" is a way of
        asking for what Parcels sound like, and telling it the name is absent leaves
        it with nothing. So the miss asks the same similar-artist machinery `similar`
        uses, keeps only the acts this station plays, and hands back a track id for
        each — one call, and the next move is a pick rather than another search.
        Whether the name exists on disk is not the question; whether it can play is.
        """
        alike = self.similar_here(text, station)
        if alike:
            note = (
                f"nothing by '{text}' plays on this station. These do, and they are "
                "what it sounds like — each carries a track id you can use."
            )
        else:
            note = (
                f"nothing by '{text}' plays on this station, and nothing here sounds "
                "much like it. Take the set somewhere else: filter draws from the "
                "whole station, catalogue knows what you own by an act you can name."
            )
        payload: dict = {"results": [], "note": note, "similar_here": alike}
        if not alike:
            # Nothing here sounds like it; the only useful thing left is a spelling
            # correction for a name that does exist.
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

        def gather(names: list[str]) -> list[dict]:
            found = []
            for candidate in names:
                hits = by_act.get(navidrome.name_key(candidate))
                if not hits:
                    continue
                lead = hits[0]
                found.append({
                    "artist": lead["album_artist"] or lead["artist"],
                    "tracks": len(hits),
                    "id": lead["id"],
                    "title": lead["title"],
                    "album": lead["album"],
                    "year": lead["year"],
                })
            found.sort(key=lambda d: (-d["tracks"], str(d["artist"]).casefold()))
            return found

        names, _source = enrich.similar_artists(self.conn, name, limit=25)
        out = gather(names)
        if len(out) < 3:
            # A cached or short list is twenty names, and on a library of sixty acts
            # the act that plays here is often the twenty-first. Ask for a hundred.
            wider = enrich.lfm_similar(name, 100)
            fresh = [n for n in wider if n not in names]
            if fresh:
                more = gather(fresh)
                seen = {d["artist"] for d in out}
                out += [d for d in more if d["artist"] not in seen]
                out.sort(key=lambda d: (-d["tracks"], str(d["artist"]).casefold()))
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
        for row in self.conn.execute(
            "SELECT album_artist AS name FROM tracks WHERE album_artist != '' GROUP BY 1"
        ):
            folded = navidrome.name_key(row["name"])
            if not folded:
                continue
            ratio = difflib.SequenceMatcher(None, key, folded).ratio()
            if key in folded or folded in key:
                ratio = max(ratio, 0.85)
            if ratio >= 0.6:
                scored.append((ratio, row["name"]))
        scored.sort(reverse=True)

        out: list[dict] = []
        for _, name in scored:
            rows = self.conn.execute(
                "SELECT * FROM tracks WHERE album_artist = ?", (name,)
            ).fetchall()
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
            target = genres_module.targets([want])
            by_track = navidrome.labels_by_track(self.conn)
            rows = [r for r in rows if genres_module.matches(by_track.get(r["id"], ()), target)]

        # A shuffle keyed to this pick, not to this call. `offset` can only mean
        # "the next page" if the order holds still between the two calls, so the
        # seed is drawn once per pick — every call within one decision pages the
        # same list, and the next pick gets a fresh order.
        rows = sorted(rows, key=self._shuffle_key)
        total = len(rows)

        limit = clamp(kw.get("limit"), 30, 200)
        offset = max(0, int(kw.get("offset") or 0))
        # Spread the page across acts. A random draw over a label with one big name in
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
        return {
            "matching": total,
            "offset": offset,
            "returned": len(picked),
            "results": [self._row(r, ctx) for r in picked],
        }

    def roster(self, station: config.Station | None = None, limit: int = 60) -> dict:
        """Who and what this station holds, in the shape the system prompt wants.

        The numbers the DJ needs before it can choose anything: what this station is
        made of, in the only terms that matter to it — labels with weight, and every act
        it can play. Two tools used to answer this a page at a time; a prompt can simply
        state it, and a stated fact costs no rounds.
        """
        rows = self._station_rows(station)

        acts: Counter = Counter()
        for row in rows:
            acts[(row["album_artist"] or row["artist"]) or "?"] += 1
        labels: Counter = Counter()
        by_track = navidrome.labels_by_track(self.conn)
        for row in rows:
            for label in by_track.get(row["id"], ()):
                labels[label] += 1

        return {
            "tracks": len(rows),
            "acts": acts.most_common(limit),
            "acts_total": len(acts),
            "labels": labels.most_common(limit),
            "labels_total": len(labels),
        }

    # Five records in the pile, five tracks visible per record. A DJ flipping crates
    # sees covers and a handful of titles, not a catalogue.
    CRATE_ALBUMS = 5
    CRATE_TRACKS = 5

    def _shuffle_key(self, row: sqlite3.Row):
        seed = self.draw_seed or 0
        return hashlib.blake2b(f"{seed}|{row['id']}".encode(), digest_size=8).digest()

    def popularity_ranks(self, rows: list) -> dict[str, float]:
        """Each track's standing among its own act's tracks, 0 = the deep cut.

        Absolute play counts only say how big the act is; the question in front of the
        DJ is which of their songs people actually know.
        """
        from .. import pool

        return pool._popularity_percentiles(self.conn, rows)

    def crate(self, station: config.Station | None = None, *, size: int | None = None,
              tracks_shown: int | None = None, avoid_acts: set[str] | None = None) -> list[dict]:
        """A handful of records pulled out at random, the way you'd flip a crate.

        Nothing here is curated: it is the pile, and the DJ may take one or ignore all
        of them. Acts it has just played are left out, and each album comes from a
        different act, so the five are five directions rather than five De Lux records.
        """
        rows = self._station_rows(station)
        if not rows:
            return []

        size = size or self.CRATE_ALBUMS
        tracks_shown = tracks_shown or self.CRATE_TRACKS
        skip = {navidrome.name_key(a) for a in (avoid_acts or set()) if a}

        by_album: dict[str, list] = {}
        for row in rows:
            if navidrome.name_key(row["album_artist"] or row["artist"]) in skip:
                continue
            by_album.setdefault(row["album_id"], []).append(row)
        if not by_album:
            return []

        # One album per act, then take the acts at random: a crate of five, not a crate
        # of five records by whoever owns the most of the library.
        by_act: dict[str, list[str]] = {}
        for album_id, tracks in by_album.items():
            by_act.setdefault(navidrome.name_key(tracks[0]["album_artist"] or tracks[0]["artist"]), []).append(album_id)
        acts = list(by_act)
        random.shuffle(acts)

        picks: list[str] = []
        for act in acts:
            picks.append(random.choice(by_act[act]))
            if len(picks) >= size:
                break

        labels = navidrome.labels_by_track(self.conn)
        ranks = self.popularity_ranks([r for album in picks for r in by_album[album]])

        out: list[dict] = []
        for album_id in picks:
            tracks = sorted(by_album[album_id], key=lambda r: (r["track_no"] or 0, r["title"] or ""))
            lead = tracks[0]
            album_labels = Counter()
            for track in tracks:
                for label in labels.get(track["id"], ()):
                    album_labels[label] += 1
            # The best-known song on the record, not its median: a median over one
            # album's own tracks is ~0.5 by construction and says nothing.
            known = max((ranks.get(t["id"], 0.0) for t in tracks), default=0.0)
            out.append({
                "album": lead["album"],
                "artist": lead["album_artist"] or lead["artist"],
                "year": lead["year"],
                "genre": (album_labels.most_common(1) or [("", 0)])[0][0],
                "known": known,
                "tracks": [
                    {"id": t["id"], "title": t["title"], "no": t["track_no"]}
                    for t in tracks[:tracks_shown]
                ],
                "total": len(tracks),
            })
        return out

    def recent_labels(self, station: config.Station, n: int = 5) -> tuple[list[str], list[str]]:
        """The labels and acts of the last few songs, newest first.

        This is the evidence a DJ would use to notice he has been in one corner all
        night: not what he meant to play, what actually went out.
        """
        plays = queue.recent(station, n)
        labels = navidrome.labels_by_track(self.conn)
        acts: list[str] = []
        counted: Counter = Counter()
        for entry in plays:
            track_id = entry.get("id") or ""
            row = self.conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone() if track_id else None
            if row is not None:
                acts.append(row["album_artist"] or row["artist"] or "")
                for label in labels.get(track_id, ()):
                    counted[label] += 1
            elif entry.get("artist"):
                acts.append(entry["artist"])
        return [label for label, _ in counted.most_common(3)], acts

    def by_id(self, track_id: str) -> navidrome.Track | None:
        row = self.conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        return self._full(row) if row else None

    def count(self, station: config.Station) -> int:
        return len(navidrome.rows_for_station(self.conn, station))

    def on_station(self, station: config.Station, track_id: str) -> bool:
        """Whether this station's own rules let that track play.

        A station whose rules match nothing is on air with the whole library (see
        rows_for_station), so its picks are allowed the same way — otherwise a typo
        in a genre list would leave the DJ unable to answer at all.
        """
        row = self.conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if row is None:
            return False
        ok = navidrome.station_matcher(self.conn, station)
        if ok(row):
            return True
        return not any(ok(r) for r in self.conn.execute("SELECT * FROM tracks WHERE uri != ''"))

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
        names, source = enrich.similar_artists(self.conn, artist, limit=20)
        if names:
            marks = ",".join("?" for _ in names)
            # Filter, then take the limit. The other order sampled rows the station
            # cannot play and reported `owned: false` even when it owns plenty of
            # tracks by those artists.
            rows = self.conn.execute(
                f"SELECT * FROM tracks WHERE artist IN ({marks}) ORDER BY RANDOM()",
                (*names,),
            ).fetchall()
            rows = self.rows_on_station(rows, station)
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
        row = self.conn.execute(
            """SELECT genre FROM tracks WHERE album_artist_key = ? AND genre != ''
               GROUP BY genre ORDER BY COUNT(*) DESC LIMIT 1""",
            (key,),
        ).fetchone()
        if row:
            # The one branch that had no station filter at all, so it could hand the
            # DJ a track the station is not allowed to play.
            rows = self.conn.execute(
                "SELECT * FROM tracks WHERE genre = ? AND album_artist_key <> ? ORDER BY RANDOM()",
                (row["genre"], key),
            ).fetchall()
            rows = self.rows_on_station(rows, station)
            rows = rows[: int(limit)]
            if rows:
                return {
                    "source": "genre",
                    "genre": row["genre"],
                    "artists": sorted({r["artist"] for r in rows})[:10],
                    "tracks": [self._row(r, ctx) for r in rows],
                }
        return {"source": "none", "artists": [], "tracks": []}

    def history(self, station: config.Station, limit: int = 20, **_) -> list[dict]:
        """The station's recent past, newest first, with how long ago each played."""
        limit = clamp(limit, 20, 100)
        plays = list(reversed(queue.recent(station, limit)))
        ctx = self._context(station)
        out: list[dict] = []
        for ago, entry in enumerate(plays):
            track_id = entry.get("id") or ""
            row = self.conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone() if track_id else None
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
        return out

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
        """
        limit = clamp(limit, 25, 100)
        where, args = ["uri != ''"], []
        if artist:
            # Either name finds the record: the credit ("& Fabiana Martone") and the
            # act are the same shelf, and a listener naming the guest should land on it.
            # Matched on the key, so an ASCII apostrophe finds the typographic one and
            # a missing accent still lands: the model types names, it does not copy them.
            where.append("(album_artist_key = ? OR artist = ? COLLATE NOCASE)")
            args.extend([navidrome.name_key(artist), artist.strip()])
        if album:
            where.append("album LIKE ?")
            args.append(f"%{album.strip()}%")
        if not artist and not album:
            return {"error": "give an artist, an album, or both"}

        rows = self.conn.execute(
            "SELECT * FROM tracks WHERE " + " AND ".join(where)
            + " ORDER BY artist, album, track_no", args
        ).fetchall()
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
        albums: dict[str, dict] = {}
        tracks: list[dict] = []
        for row in rows[:limit]:
            item = self._row(row, ctx)
            item["track_no"] = row["track_no"]
            tracks.append(item)
            key = row["album"] or ""
            albums.setdefault(key, {"album": key, "year": row["year"], "tracks": []})
            albums[key]["tracks"].append(item)
        return {
            "artist": rows[0]["album_artist"] or rows[0]["artist"],
            "album": album or "",
            "albums": list(albums.values()),
            "tracks": tracks,
            "played_recently": any(t["played"] for t in tracks),
        }

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
            return self.conn.execute("SELECT * FROM tracks WHERE uri != ''").fetchall()
        cached = getattr(self, "_rows_cache", None)
        now = time.monotonic()
        if cached and cached[0] is station and now - cached[1] < 5.0:
            return cached[2]
        rows = navidrome.rows_for_station(self.conn, station)
        self._rows_cache = (station, now, rows)
        return rows

    def _context(self, station: config.Station | None) -> dict:
        """Everything a track row needs, gathered once per call rather than per row.

        Genres, how well known each track is for its act, whether it is inside the
        avoid window, and whether it is already queued — the four things the DJ needs
        to judge a row, and the four things only some tools used to carry.
        """
        rows = self._station_rows(station)
        played = queue.recent_ids(station, 40) if station else set()
        try:
            queued = set(mixer.queued_ids(station)) if station else set()
        except Exception:
            queued = set()
        return {
            "labels": navidrome.labels_by_track(self.conn),
            "ranks": self.popularity_ranks(rows),
            "played": played,
            "queued": queued,
        }

    @staticmethod
    def _clock(seconds: int | None) -> str:
        total = int(seconds or 0)
        return f"{total // 60}:{total % 60:02d}" if total else ""

    def _row(self, row: sqlite3.Row, ctx: dict) -> dict:
        """The row every tool returns, so the model never has to guess what is missing.

        The record's artist, not the track credit: the DJ reasons about acts, and
        "L’Impératrice & Fabiana Martone" is L’Impératrice playing with a guest.
        """
        keys = row.keys()
        genres = ctx["labels"].get(row["id"]) or ([row["genre"]] if row["genre"] else [])
        return {
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

    @staticmethod
    def _full(row: sqlite3.Row) -> navidrome.Track:
        return navidrome.Track(
            id=row["id"],
            uri=row["uri"],
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
    "search": Library.search,
    "filter": Library.filter,
}
_offered = {t["function"]["name"] for t in TOOLS}
if set(HANDLERS) != _offered:
    raise RuntimeError(
        f"tool schemas and handlers disagree: {sorted(_offered ^ set(HANDLERS))}"
    )
