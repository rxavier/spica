"""Row shapes for the picker's tool results.

The model sees these dicts; keeping them here means a change to what a row carries
is one file, not a hunt through the Library.
"""

from __future__ import annotations

import json
import sqlite3

from .. import loudness, navidrome

# How much of the tool result a page may spend on rows, leaving room for the envelope.
# A page has to fit before the model sees it.
ROW_BUDGET = 5200


def clock(seconds: int | None) -> str:
    total = int(seconds or 0)
    return f"{total // 60}:{total % 60:02d}" if total else ""


def row(row: sqlite3.Row, ctx: dict) -> dict:
    """The row every tool returns, so the model never has to guess what is missing."""
    keys = row.keys()
    genres = ctx["labels"].get(row["id"]) or ([row["genre"]] if row["genre"] else [])
    item = {
        "id": row["id"],
        "title": row["title"],
        "artist": (row["album_artist"] if "album_artist" in keys else "") or row["artist"],
        "album": row["album"],
        "year": row["year"],
        "genre": " / ".join(genres),
        "duration": clock(row["duration"] if "duration" in keys else 0),
        "popularity": round(ctx["ranks"].get(row["id"], 0.5), 2),
        "played": row["id"] in ctx["played"],
        "queued": row["id"] in ctx["queued"],
    }
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


def visible(rows: list, ctx: dict) -> list[dict]:
    """The rows of a page that fit the budget, so the count beside them is true."""
    return fit_rows([row(item, ctx) for item in rows])


def fit_rows(items: list[dict], budget: int = ROW_BUDGET) -> list[dict]:
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


def lean_row(row: sqlite3.Row, ctx: dict) -> dict:
    """One track inside an album block: enough to choose it, nothing the album says."""
    return {
        "id": row["id"],
        "title": row["title"],
        "track_no": row["track_no"],
        "duration": clock(row["duration"]),
        "popularity": round(ctx["ranks"].get(row["id"], 0.5), 2),
        "played": row["id"] in ctx["played"],
    }


def full(row: sqlite3.Row, measured: dict) -> navidrome.Track:
    """The full Track with its loudness annotation, for the mixer."""
    return navidrome.Track(
        id=row["id"],
        uri=loudness.annotate(row["uri"], loudness.for_row(row, measured.get(row["id"]))),
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
