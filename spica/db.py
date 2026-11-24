"""Shared pieces for the SQLite stores.

All SQL lives in `spica.queries`; this module holds the fragments, the open helper
and the coverage shape those queries share, so a rule like "a track is playable
when it has a URI" is stated once instead of at every call site.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# (device, inode) of stores already migrated in this process. SQLite writes change
# mtime, so the inode is the stable identity of one store file.
_READY: set[tuple[int, int]] = set()

# A track can only air when the station has something to fetch.
PLAYABLE = "uri != ''"


def artist_expr(alias: str = "") -> str:
    """The record-artist rule: the album artist when set, else the track artist."""
    prefix = f"{alias}." if alias else ""
    return f"COALESCE(NULLIF({prefix}album_artist, ''), {prefix}artist)"


def where_in(column: str, values) -> tuple[str, list]:
    """`column IN (?, ?, …)`, with a false predicate for an empty list."""
    values = list(values)
    if not values:
        return "0", []
    return f"{column} IN ({', '.join('?' * len(values))})", values


def open_db(
    path: str | Path,
    *,
    readonly: bool = False,
    timeout: float = 30.0,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    path = Path(path)
    if readonly:
        conn = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, timeout=timeout,
            check_same_thread=check_same_thread,
        )
    else:
        conn = sqlite3.connect(path, timeout=timeout, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    return conn


def open_store(
    path: str | Path,
    *,
    create: bool = False,
    readonly: bool = False,
    timeout: float = 30.0,
    ensure=None,
) -> sqlite3.Connection | None:
    """Open an optional store: None when it does not exist, created when asked.

    The analysis and loudness stores both outlive the index and are optional, so a
    missing file means "that feature never ran" rather than an error. ``ensure`` is
    the schema/migration step, run only when the store is being created.
    """
    target = Path(path)
    existed = target.exists()
    if readonly and not target.exists():
        return None
    if not create and not target.exists():
        return None
    if create:
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(
        target, readonly=readonly, timeout=timeout,
        check_same_thread=False if readonly else True,
    )
    if create and ensure is not None:
        key = (str(target), target.stat().st_ino)
        if not existed or key not in _READY:
            ensure(conn)
            _READY.add(key)
    return conn


def coverage(known: int, total: int) -> dict:
    """The four numbers every cover widget needs."""
    known, total = int(known), int(total)
    return {
        "known": known,
        "total": total,
        "remaining": max(0, total - known),
        "percent": round(known * 100.0 / total, 1) if total else 0.0,
    }
