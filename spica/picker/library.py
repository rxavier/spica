"""Lookup facade for the picker: ids, station rows, caches and shared query helpers.

Tool implementations and dispatch live in `picker.tools`. Nothing here talks to the
network; every read comes from the index the worker rebuilt.
"""

from __future__ import annotations

import sqlite3
from collections import Counter

from .. import config, navidrome, queue
from ..queries import tracks as track_queries
from . import rows as row_shapes
from .context import LibraryCaches
from .memory import recent_artists
from .tools import Tools


class Library:
    # Set by the loop once per pick; a standalone call gets its own throwaway seed.
    draw_seed: int = 0

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.caches = LibraryCaches(conn)
        self.tools = Tools(self)

    # Cache access, kept here so the tools and the candidate builders do not know
    # how each cache is keyed.
    def analysis_store(self):
        return self.caches.analysis_store()

    def features(self) -> dict:
        return self.caches.features()

    def track_vectors(self) -> dict:
        return self.caches.track_vectors()

    def sound_line(self, names: list[str]) -> str:
        return self.caches.sound_line(names)

    def audio_artists(self, name: str, k: int) -> list[str]:
        return self.caches.audio_artists(name, k)

    def labels_by_track(self) -> dict[str, list[str]]:
        return self.caches.labels_by_track()

    def _matcher(self, station: config.Station):
        return self.caches.matcher(station)

    def _station_state(self, station: config.Station | None) -> tuple[list, bool]:
        return self.caches.station_state(station)

    def station_rows(self, station: config.Station | None) -> list:
        return self.caches.station_rows(station)

    def _measured(self) -> dict:
        return self.caches.measured()

    def rows_on_station(self, rows: list, station: config.Station | None) -> list:
        """Drop whatever the station's rules would not let on air.

        Every tool result goes through this, so the station's rules constrain what the
        model can return instead of being a suggestion in the prompt.
        """
        if station is None:
            return rows
        ok = self._matcher(station)
        return [r for r in rows if ok(r)]

    def by_id(self, track_id: str) -> navidrome.Track | None:
        row = track_queries.track_by_id(self.conn, track_id)
        return self.full(row) if row else None

    def by_ids(self, track_ids) -> dict[str, navidrome.Track]:
        """The same lookup for many ids in one query, keyed by id."""
        rows = track_queries.tracks_by_ids(self.conn, list(track_ids))
        return {row["id"]: self.full(row) for row in rows}

    def artists_by_ids(self, track_ids) -> dict[str, str]:
        """Record artist per id, without building a full Track (or reading gains)."""
        rows = track_queries.tracks_by_ids(
            self.conn, list(track_ids), "id, album_artist, artist"
        )
        return {
            row["id"]: (row["album_artist"] or row["artist"] or "")
            for row in rows
        }

    def on_station(self, station: config.Station, track_id: str) -> bool:
        """Whether this station's own rules let that track play.

        A station whose rules match nothing is on air with the whole library (see
        rows_for_station), so its picks are allowed the same way — otherwise a typo
        in a genre list would leave the picker unable to answer at all.
        """
        row = track_queries.track_by_id(self.conn, track_id)
        if row is None:
            return False
        _rows, fallback = self._station_state(station)
        return fallback or self._matcher(station)(row)

    def popularity_ranks(self, rows: list) -> dict[str, float]:
        """Each track's standing among its own artist's tracks, 0 = the least known."""
        from .. import pool

        return pool._popularity_percentiles(self.conn, rows)

    def catalog_summary(self, station: config.Station | None = None,
                        limit: int = 60) -> dict:
        """What this station holds, in the shape the system prompt wants."""
        rows = self.station_rows(station)
        artists: Counter = Counter()
        for row in rows:
            artists[(row["album_artist"] or row["artist"]) or "?"] += 1
        labels: Counter = Counter()
        by_track = self.labels_by_track()
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

    def recent_labels(self, station: config.Station,
                      n: int = 5) -> tuple[list[str], list[str]]:
        """The labels and artists of the last few songs, oldest first."""
        plays = queue.recent(station, n)
        labels = self.labels_by_track()
        counted: Counter = Counter()
        for entry in plays:
            for label in labels.get(entry.get("id") or "", ()):
                counted[label] += 1
        return (
            [label for label, _ in counted.most_common(3)],
            recent_artists(self, station, n),
        )

    # Tool surface. The Library keeps the picker's public names; Tools owns the
    # implementations and the model-facing contract.
    def search(self, query: str, limit: int = 20,
               station: config.Station | None = None, **kw):
        return self.tools.search(query, limit=limit, station=station, **kw)

    def filter(self, station: config.Station | None = None, **kw):
        return self.tools.filter(station=station, **kw)

    def similar_here(self, name: str, station: config.Station | None = None,
                     limit: int = 6):
        return self.tools.similar_here(name, station=station, limit=limit)

    def nearest(self, text: str, station: config.Station | None = None,
                limit: int = 5):
        return self.tools.nearest(text, station=station, limit=limit)

    def sample_albums(self, station: config.Station | None = None, *,
                      size: int | None = None, tracks_shown: int | None = None,
                      avoid_acts: set[str] | None = None):
        return self.tools.sample_albums(
            station, size=size, tracks_shown=tracks_shown, avoid_acts=avoid_acts
        )

    def similar(self, artist: str, limit: int = 12,
                station: config.Station | None = None):
        return self.tools.similar(artist, limit=limit, station=station)

    def redraw_albums(self, station: config.Station | None = None, **kw):
        return self.tools.redraw_albums(station=station, **kw)

    def history(self, station: config.Station, limit: int = 20, **kw):
        return self.tools.history(station, limit=limit, **kw)

    def catalogue(self, artist: str | None = None, album: str | None = None,
                  limit: int = 25, station: config.Station | None = None, **kw):
        return self.tools.catalogue(
            artist=artist, album=album, limit=limit, station=station, **kw
        )

    def run_tool(self, name: str, args: dict, station: config.Station):
        return self.tools.run_tool(name, args, station)

    def row(self, row: sqlite3.Row, ctx: dict) -> dict:
        return row_shapes.row(row, ctx)

    def full(self, row: sqlite3.Row) -> navidrome.Track:
        return row_shapes.full(row, self._measured())
