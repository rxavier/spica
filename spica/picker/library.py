"""The query layer the tools sit on.

Six read-only questions over the index — search, filter, catalogue, similar,
history, station map — and the one place a tool call is dispatched. Nothing here
talks to the network; the tools only ever read what the rebuild wrote.
"""

from __future__ import annotations

import sqlite3
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


class Library:
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

    def search(self, query: str, limit: int = 20, station: config.Station | None = None) -> list[dict]:
        limit = clamp(limit, 20, 200)
        like = f"%{query.strip()}%"
        rows = self.conn.execute(
            """SELECT * FROM tracks WHERE title LIKE ? OR artist LIKE ? OR album LIKE ?
               ORDER BY artist, album, track_no LIMIT ?""",
            (like, like, like, limit * 40),
        ).fetchall()
        rows = self.rows_on_station(rows, station)
        return [self._brief(r) for r in rows[:limit]]

    def filter(self, station: config.Station | None = None, **kw) -> list[dict]:
        where, args = ["uri != ''"], []
        if kw.get("year_from"):
            where.append("year >= ?")
            args.append(int(kw["year_from"]))
        if kw.get("year_to"):
            where.append("year <= ?")
            args.append(int(kw["year_to"]))
        sql = "SELECT * FROM tracks WHERE " + " AND ".join(where) + " ORDER BY RANDOM()"
        rows = self.conn.execute(sql, args).fetchall()

        # Genre goes through the same fold/refinement the stations use, so
        # "Punk" finds "Punk Rock" here exactly as it does on air.
        want = kw.get("genre")
        if want:
            target = genres_module.targets([want])
            by_track = navidrome.labels_by_track(self.conn)
            rows = [r for r in rows if genres_module.matches(by_track.get(r["id"], ()), target)]

        rows = self.rows_on_station(rows, station)

        limit = clamp(kw.get("limit"), 30, 200)
        return [self._brief(r) for r in rows[:limit]]

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
                "tracks": [self._brief(r) for r in rows],
                "owned": bool(rows),
            }

        row = self.conn.execute(
            """SELECT genre FROM tracks WHERE artist = ? AND genre != ''
               GROUP BY genre ORDER BY COUNT(*) DESC LIMIT 1""",
            (artist,),
        ).fetchone()
        if row:
            # The one branch that had no station filter at all, so it could hand the
            # DJ a track the station is not allowed to play.
            rows = self.conn.execute(
                "SELECT * FROM tracks WHERE genre = ? AND artist <> ? ORDER BY RANDOM()",
                (row["genre"], artist),
            ).fetchall()
            rows = self.rows_on_station(rows, station)
            rows = rows[: int(limit)]
            if rows:
                return {
                    "source": "genre",
                    "genre": row["genre"],
                    "artists": sorted({r["artist"] for r in rows})[:10],
                    "tracks": [self._brief(r) for r in rows],
                }
        return {"source": "none", "artists": [], "tracks": []}

    def history(self, station: config.Station, limit: int = 20, **_) -> list[dict]:
        """The station's recent past, newest first, with how long ago each played."""
        limit = clamp(limit, 20, 100)
        plays = list(reversed(queue.recent(station, limit)))
        out: list[dict] = []
        for ago, entry in enumerate(plays):
            track_id = entry.get("id") or ""
            track = self.by_id(track_id) if track_id else None
            out.append(
                {
                    "id": track_id,
                    "title": entry.get("title") or (track.title if track else ""),
                    "artist": entry.get("artist") or (track.artist if track else ""),
                    "album": track.album if track else "",
                    "year": track.year if track else None,
                    "songs_ago": ago,
                    "on_air": ago == 0,
                }
            )
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
            where.append("artist = ? COLLATE NOCASE")
            args.append(artist.strip())
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
                counts[row["artist"]] = counts.get(row["artist"], 0) + 1
            artist = max(counts, key=counts.get)
            rows = [r for r in rows if r["artist"] == artist]

        played = {e["id"] for e in queue.recent(station, 40) if e.get("id")} if station else set()
        queued = set(mixer.queued_ids(station)) if station else set()
        albums: dict[str, dict] = {}
        tracks: list[dict] = []
        for row in rows[:limit]:
            item = self._brief(row)
            item["track_no"] = row["track_no"]
            item["played"] = row["id"] in played
            item["queued"] = row["id"] in queued
            tracks.append(item)
            key = row["album"] or ""
            albums.setdefault(key, {"album": key, "year": row["year"], "tracks": []})
            albums[key]["tracks"].append(item)
        return {
            "artist": rows[0]["artist"],
            "album": album or "",
            "albums": list(albums.values()),
            "tracks": tracks,
            "played_recently": any(t["played"] for t in tracks),
        }

    def station_map(self, station: config.Station, **_) -> dict:
        """What this station is made of, in numbers the model can reason with."""
        rows = navidrome.rows_for_station(self.conn, station)
        labels = navidrome.labels_by_track(self.conn)
        genre_counts: Counter = Counter()
        artist_counts: Counter = Counter()
        decades: Counter = Counter()
        for row in rows:
            for label in labels.get(row["id"], ()):
                genre_counts[label] += 1
            artist_counts[row["artist"] or "?"] += 1
            year = row["year"] or 0
            if year >= 1900:
                decades[(year // 10) * 10] += 1
        # Bogus years in file tags (1006, 1800s reissues) would otherwise make the
        # station look like it spans a millennium; count them, don't rank them.
        years = [r["year"] for r in rows if r["year"] and r["year"] >= 1900]
        odd = sum(1 for r in rows if r["year"] and r["year"] < 1900)
        undated = sum(1 for r in rows if not r["year"])
        return {
            "station": station.name,
            "tracks": len(rows),
            "genres": [
                {"label": label, "tracks": n} for label, n in genre_counts.most_common(8)
            ],
            "years": {
                "from": min(years) if years else None,
                "to": max(years) if years else None,
                "decades": {
                    f"{d}s": n for d, n in sorted(decades.items()) if d >= 1900
                },
                "undated": undated,
                "ignored_bad_years": odd,
            },
            "artists": [{"name": name, "tracks": n} for name, n in artist_counts.most_common(10)],
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

    @staticmethod
    def _brief(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "title": row["title"],
            "artist": row["artist"],
            "album": row["album"],
            "year": row["year"],
            "genre": row["genre"],
        }

    @staticmethod
    def _full(row: sqlite3.Row) -> navidrome.Track:
        return navidrome.Track(
            id=row["id"],
            uri=row["uri"],
            title=row["title"],
            artist=row["artist"],
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
    "station_map": Library.station_map,
    "similar": Library.similar,
    "search": Library.search,
    "filter": Library.filter,
}
_offered = {t["function"]["name"] for t in TOOLS}
if set(HANDLERS) != _offered:
    raise RuntimeError(
        f"tool schemas and handlers disagree: {sorted(_offered ^ set(HANDLERS))}"
    )
