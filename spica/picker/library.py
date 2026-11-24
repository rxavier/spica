"""The query layer the tools sit on.

Six read-only questions over the index — search, filter, catalogue, similar,
history, station map — and the one place a tool call is dispatched. Nothing here
talks to the network; the tools only ever read what the rebuild wrote.
"""

from __future__ import annotations

import sqlite3

from .. import config, loudness, navidrome
from ..queries import tracks as track_queries
from .context import LibraryCaches
from .rows import full
from .rows import row as render_row
from .tools import Tools


class Library:
    # Set by the loop once per pick; a standalone call gets its own throwaway seed.
    draw_seed: int = 0

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.caches = LibraryCaches(conn)
        self.tools = Tools(self)

    # Thin views over the caches, kept so query methods do not know how each is keyed.
    def analysis_store(self):
        return self.caches.analysis_store()

    def _features(self) -> dict:
        return self.caches.features()

    def track_vectors(self) -> dict:
        return self.caches.track_vectors()

    def sound_line(self, names: list[str]) -> str:
        return self.caches.sound_line(names)

    def _audio_artists(self, name: str, k: int) -> list[str]:
        return self.caches.audio_artists(name, k)

    def labels_by_track(self) -> dict[str, list[str]]:
        return self.caches.labels_by_track()

    def _matcher(self, station: config.Station):
        return self.caches.matcher(station)

    def _station_state(self, station: config.Station | None) -> tuple[list, bool]:
        return self.caches.station_state(station)

    def _station_rows(self, station: config.Station | None) -> list:
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
        return self._full(row) if row else None

    def by_ids(self, track_ids) -> dict[str, navidrome.Track]:
        """The same lookup for many ids in one query, keyed by id."""
        rows = track_queries.tracks_by_ids(self.conn, list(track_ids))
        return {row["id"]: self._full(row) for row in rows}

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
        if self._matcher(station)(row):
            return True
        _rows, fallback = self._station_state(station)
        return fallback

    # Tool surface: the Library answers lookups, the Tools object answers questions.
    def search(self, *args, **kwargs):
        return self.tools.search(*args, **kwargs)

    def filter(self, *args, **kwargs):
        return self.tools.filter(*args, **kwargs)

    def similar_here(self, *args, **kwargs):
        return self.tools.similar_here(*args, **kwargs)

    def nearest(self, *args, **kwargs):
        return self.tools.nearest(*args, **kwargs)

    def catalog_summary(self, *args, **kwargs):
        return self.tools.catalog_summary(*args, **kwargs)

    def sample_albums(self, *args, **kwargs):
        return self.tools.sample_albums(*args, **kwargs)

    def recent_labels(self, *args, **kwargs):
        return self.tools.recent_labels(*args, **kwargs)

    def similar(self, *args, **kwargs):
        return self.tools.similar(*args, **kwargs)

    def redraw_albums(self, *args, **kwargs):
        return self.tools.redraw_albums(*args, **kwargs)

    def history(self, *args, **kwargs):
        return self.tools.history(*args, **kwargs)

    def catalogue(self, *args, **kwargs):
        return self.tools.catalogue(*args, **kwargs)

    def popularity_ranks(self, *args, **kwargs):
        return self.tools.popularity_ranks(*args, **kwargs)

    def run_tool(self, *args, **kwargs):
        return self.tools.run_tool(*args, **kwargs)

    def _row(self, row: sqlite3.Row, ctx: dict) -> dict:
        return render_row(row, ctx)

    def _full(self, row: sqlite3.Row) -> navidrome.Track:
        return full(row, self._measured())
