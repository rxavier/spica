"""Cache and store access for the picker's Library.

The analysis store, the label map, the station matcher, the station rows and the
loudness map each have their own invalidation key. Keeping them here lets the
Library answer queries without owning the cache lifecycle.
"""

from __future__ import annotations

import sqlite3
import time

from .. import config, loudness, navidrome
from .. import genres as genres_module
from ..queries import tracks as track_queries

# The analysis store is optional, but schema drift must be reported once.
_analysis_warned = False


def _analysis_error(exc: Exception) -> None:
    global _analysis_warned
    if not _analysis_warned:
        _analysis_warned = True
        print(f"analysis: could not read the store: {exc}", flush=True)


class LibraryCaches:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._analysis_conn = None
        self._analysis_at: float | None = None
        self._analysis_features: dict = {}
        self._vectors: dict = {}
        self._vectors_at: float | None = None
        self._labels_cache = None
        self._matcher_cache = None
        self._rows_cache = None
        self._measured_cache = None

    def analysis_store(self):
        """The read-only analysis store, reopened at most once a minute."""
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
            try:
                self._analysis_features = analysis.feature_map(self._analysis_conn)
            except Exception as exc:
                _analysis_error(exc)
                self._analysis_features = {}
        return self._analysis_conn

    def features(self) -> dict:
        self.analysis_store()
        return self._analysis_features

    def track_vectors(self) -> dict:
        """Every analysed track's unit embedding, cached until the store changes."""
        store = self.analysis_store()
        if store is None:
            return {}
        from .. import analysis

        try:
            stamp = analysis.store_path().stat().st_mtime
        except OSError:
            return {}
        if self._vectors_at != stamp:
            try:
                self._vectors = analysis.vector_map(store)
            except Exception as exc:
                _analysis_error(exc)
                self._vectors = {}
            self._vectors_at = stamp
        return self._vectors

    def sound_line(self, names: list[str]) -> str:
        """One compact line on how the candidate artists sound, or ""."""
        if not names or self.analysis_store() is None:
            return ""
        from .. import analysis

        try:
            return analysis.style_summary(self._analysis_conn, names)
        except Exception as exc:
            _analysis_error(exc)
            return ""

    def audio_artists(self, name: str, k: int) -> list[str]:
        store = self.analysis_store()
        if store is None:
            return []
        from .. import analysis

        try:
            return analysis.nearest_artists(store, name, k=k)
        except Exception as exc:
            _analysis_error(exc)
            return []

    def index_stamp(self) -> int | None:
        """When the index was last written, to key the caches that read it."""
        try:
            return (config.STATE_DIR / "index.db").stat().st_mtime_ns
        except OSError:
            return None

    def labels_by_track(self) -> dict[str, list[str]]:
        """The stored label of every track, cached until the index changes."""
        stamp = self.index_stamp()
        if self._labels_cache is not None and self._labels_cache[0] == stamp:
            return self._labels_cache[1]
        data = navidrome.labels_by_track(self.conn)
        self._labels_cache = (stamp, data)
        return data

    def matcher(self, station: config.Station):
        """This station's rule predicate, cached until the index or aliases change."""
        try:
            aliases = genres_module.alias_path().stat().st_mtime_ns
        except OSError:
            aliases = None
        key = (station.id, self.index_stamp(), aliases)
        if self._matcher_cache is not None and self._matcher_cache[0] == key:
            return self._matcher_cache[1]
        ok = navidrome.station_matcher(self.conn, station, self.labels_by_track())
        self._matcher_cache = (key, ok)
        return ok

    def station_state(self, station: config.Station | None) -> tuple[list, bool]:
        """The station's playable rows and whether its rules matched nothing."""
        if station is None:
            return track_queries.playable_tracks(self.conn), False
        now = time.monotonic()
        if (self._rows_cache is not None and self._rows_cache[0] is station
                and now - self._rows_cache[1] < 5.0):
            return self._rows_cache[2], self._rows_cache[3]
        rows, fallback = navidrome.station_match(
            self.conn, station, matcher=self.matcher(station)
        )
        self._rows_cache = (station, now, rows, fallback)
        return rows, fallback

    def station_rows(self, station: config.Station | None) -> list:
        return self.station_state(station)[0]

    def measured(self) -> dict:
        """The loudness store as id -> (lufs, peak), refreshed when the file changes."""
        if not config.LOUDNESS:
            return {}
        try:
            stamp = loudness.store_path().stat().st_mtime_ns
        except OSError:
            return {}
        if self._measured_cache is not None and self._measured_cache[0] == stamp:
            return self._measured_cache[1]
        data = loudness.all_measured()
        self._measured_cache = (stamp, data)
        return data
