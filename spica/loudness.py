"""One static gain per track, from ReplayGain or a one-time measurement.

Records arrive at the station mastered at different levels, and the station plays
them back to back. A tag already says how far a track has to move to reach the
ReplayGain reference; this module turns that into a gain toward the station's own
target and puts it on the request as Liquidsoap's ``liq_amplify`` annotation, so
the mixer applies it before the crossfade and the programme bus is untouched.

Where a file has no tag, the worker can measure it once with ffmpeg's EBU R128
filter and keep the result in ``state/loudness.db``. That store is separate from
the index on purpose: the index is a disposable projection, and a measurement is
an hour of CPU.

This is normalisation, not the loudness-as-intensity signal the README rejects.
Nothing here is read by the DJ: loudness says how loud a record was mastered, not
how energetic it is.
"""

from __future__ import annotations

import math
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from . import config, db
from .queries import loudness as loudness_queries

# ReplayGain 2.0's reference: a tag's ``trackGain`` is the dB offset that brings
# the track to this integrated loudness, so the track itself is reference − gain.
REPLAYGAIN_REFERENCE_LUFS = -18.0

# The metadata key Liquidsoap's amplify operator reads. Its value is "<n> dB".
ANNOTATION = "liq_amplify"




# ------------------------------------------------------------------ gain maths

def _finite(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def from_replaygain(tag) -> tuple[float | None, float | None]:
    """Read an OpenSubsonic ``replayGain`` object as (lufs, peak dBFS).

    ``trackPeak`` is linear (1.0 = full scale), so it is converted here; a tag
    with a gain but no peak still yields a loudness, and the gain function then
    knows it may cut but not lift.
    """
    if not isinstance(tag, dict):
        return None, None
    gain = _finite(tag.get("trackGain"))
    if gain is None:
        return None, None
    peak = _finite(tag.get("trackPeak"))
    peak_db = 20.0 * math.log10(peak) if peak is not None and peak > 0 else None
    return REPLAYGAIN_REFERENCE_LUFS - gain, peak_db


def gain_db(
    lufs: float | None,
    peak_db: float | None,
    *,
    target: float | None = None,
    max_boost: float | None = None,
    max_cut: float | None = None,
    ceiling: float | None = None,
) -> float | None:
    """The dB to add to a track, or None when its loudness is unknown.

    A lift needs a known peak: without one the headroom is unknown, and the boost
    cap alone would let a quiet-reading track run straight into the encoder's
    ceiling. Cuts never need a peak, so a track can always be brought down.
    """
    if lufs is None:
        return None
    target = config.LOUDNESS_TARGET_LUFS if target is None else target
    max_boost = config.LOUDNESS_MAX_BOOST_DB if max_boost is None else max_boost
    max_cut = config.LOUDNESS_MAX_CUT_DB if max_cut is None else max_cut
    ceiling = config.LOUDNESS_PEAK_CEILING_DBFS if ceiling is None else ceiling

    raw = target - lufs
    if raw > 0:
        if peak_db is None:
            raw = 0.0
        else:
            raw = min(raw, max_boost, max(0.0, ceiling - peak_db))
    else:
        raw = max(raw, -max_cut)
    return round(raw, 1)


def for_tag(track_gain, track_peak) -> float | None:
    """The gain for one track's raw ReplayGain fields, or None without a tag."""
    if not config.LOUDNESS:
        return None
    lufs, peak_db = from_replaygain({"trackGain": track_gain, "trackPeak": track_peak})
    return gain_db(lufs, peak_db)


def for_row(row, measured: tuple[float | None, float | None] | None = None) -> float | None:
    """The gain for an index row: its tag first, then a stored measurement.

    ``measured`` is passed in by the callers that already hold the lookup table
    (the pool writes thousands of rows) so this stays a pure function. A row
    without either source plays untouched.
    """
    if not config.LOUDNESS:
        return None
    try:
        gain = for_tag(row["replaygain_gain"], row["replaygain_peak"])
    except (KeyError, IndexError, TypeError):
        gain = None
    if gain is not None:
        return gain
    return gain_db(*measured) if measured else None


def annotate(uri: str, gain: float | None) -> str:
    """Put the gain on a request URI, where Liquidsoap's amplify will find it.

    The format is Liquidsoap's own: ``annotate:key="value":target``. A track with
    no gain is handed over untouched, so a library without tags is unchanged.
    """
    if not uri or gain is None:
        return uri
    return f'annotate:{ANNOTATION}="{gain:.1f} dB":{uri}'


# ------------------------------------------------------------- measured store

def store_path() -> Path:
    return config.STATE_DIR / "loudness.db"


def connect(*, create: bool = False) -> sqlite3.Connection | None:
    """The measured-loudness store, or None before the worker has made one."""
    return db.open_store(
        store_path(), create=create, timeout=30, ensure=loudness_queries.ensure_schema
    )


def save(conn: sqlite3.Connection, track_id: str, lufs: float | None,
         peak: float | None, error: str = "") -> None:
    """Keep one attempt. A failed measurement is kept with its error, not retried."""
    with conn:
        loudness_queries.save(
            conn, (track_id, lufs, peak, int(time.time()), str(error)[:300])
        )


def measured(track_id: str) -> tuple[float | None, float | None] | None:
    """A stored measurement for one id, or None."""
    if not config.LOUDNESS or not track_id:
        return None
    conn = connect()
    if conn is None:
        return None
    try:
        row = loudness_queries.measured(conn, track_id)
    finally:
        conn.close()
    return (row["lufs"], row["peak"]) if row and row["lufs"] is not None else None


def all_measured() -> dict[str, tuple[float | None, float | None]]:
    """The whole store as id -> (lufs, peak), for a pool write."""
    if not config.LOUDNESS:
        return {}
    conn = connect()
    if conn is None:
        return {}
    try:
        return {
            row["track_id"]: (row["lufs"], row["peak"])
            for row in loudness_queries.all_measured(conn)
        }
    finally:
        conn.close()


def _done_ids() -> set[str]:
    conn = connect()
    if conn is None:
        return set()
    try:
        return loudness_queries.done_ids(conn)
    finally:
        conn.close()


def pending(index_conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    """Tracks with neither a ReplayGain tag nor an attempt, in album order.

    The tag filter and the LIMIT are in SQL; the attempt set lives in the other
    database, so the query fetches `limit + len(done)` rows — enough that skip-past
    cannot leave fewer than `limit` — instead of loading the whole library.
    """
    if limit <= 0:
        return []
    done = _done_ids()
    rows = loudness_queries.candidates(index_conn, limit + len(done))
    return [row for row in rows if row["id"] not in done][:limit]


def backlog(index_conn: sqlite3.Connection) -> int:
    """How many playable tracks still need a measurement."""
    done = _done_ids()
    total = 0
    for row in loudness_queries.gain_rows(index_conn):
        if row["id"] in done or row["replaygain_gain"] is not None:
            continue
        total += 1
    return total


def coverage(index_conn: sqlite3.Connection) -> dict | None:
    """How much of the playable library has a level, from either source."""
    if not config.LOUDNESS:
        return None
    playable = loudness_queries.playable_ids(index_conn)
    tagged = loudness_queries.tagged_ids(index_conn)
    seen: set[str] = set()
    errors = 0
    conn = connect()
    if conn is not None:
        try:
            for row in loudness_queries.measured_rows(conn):
                if row["lufs"] is not None:
                    seen.add(row["track_id"])
                else:
                    errors += 1
        finally:
            conn.close()
    measured_ids = seen & playable
    known = len(tagged | measured_ids)
    total = len(playable)
    return {
        "known": known,
        "total": total,
        "remaining": max(0, total - known),
        "percent": math.floor(known * 1000.0 / total) / 10 if total else 0.0,
        "tagged": len(tagged),
        "measured": len(measured_ids),
        "errors": errors,
    }


# ------------------------------------------------------------------ measuring

def measure_available() -> bool:
    return shutil.which("ffmpeg") is not None


def parse_summary(stderr: str) -> tuple[float, float | None]:
    """Integrated loudness and sample peak from ffmpeg's ebur128 summary.

    The per-frame progress lines also carry ``I:``; only the summary is read, so
    the value is the one for the whole file. A silent file has no integrated
    loudness to parse and is reported as an error rather than guessed at.
    """
    summary = (stderr or "").split("Summary:")[-1]
    lufs = re.findall(r"I:\s*(-?\d+(?:\.\d+)?)\s+LUFS", summary)
    if not lufs:
        raise ValueError("ffmpeg reported no integrated loudness")
    peaks = re.findall(r"Peak:\s*(-?\d+(?:\.\d+)?)\s+dBFS", summary)
    return float(lufs[-1]), (float(peaks[-1]) if peaks else None)


def measure(url: str, timeout: float | None = None) -> tuple[float, float | None]:
    """Measure one stream with ffmpeg's EBU R128 filter.

    ``peak=sample`` rather than ``peak=true``: it is about twice as cheap and it
    is the same sample peak a ReplayGain tag's ``trackPeak`` carries, so a
    measured track and a tagged one follow the same boost rule.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed in this worker image")
    cmd = [
        ffmpeg, "-hide_banner", "-nostats", "-loglevel", "info", "-nostdin",
        "-i", url, "-map", "0:a:0", "-threads", "1",
        "-af", "ebur128=peak=sample", "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            timeout=timeout if timeout is not None else config.LOUDNESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"ffmpeg timed out after {timeout if timeout is not None else config.LOUDNESS_TIMEOUT}s"
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited {proc.returncode}")
    return parse_summary(proc.stderr or "")
