"""The station rack: add, rename, delete, and each station's rules."""

from __future__ import annotations

import asyncio
from collections import Counter
from urllib.parse import quote

from .. import config
from .. import navidrome
from .. import pool
from .. import store
from .. import auth
from ..queries import labels as label_queries
from ..queries import tracks as track_queries
from fastapi import APIRouter, Form, HTTPException, Request, Depends
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
)

from .shared import (
    templates,
    stations,
    station_or_404,
    pool_preview,
    write_stations,
    _raw_stations,
    _slug,
    _move_state,
    _apply_station_script,
    known_labels,
    _int_or_none,
    _conn,
)

router = APIRouter(dependencies=[Depends(auth.require_admin_page)])

STATION_ARTISTS_PER_PAGE = 30


def _station_artist_page(conn, station, q: str = "", page: int = 1) -> dict:
    """One page of every artist the station's saved rules can play."""
    q = (q or "").strip()[:80]
    rows = navidrome.rows_for_station(conn, station)
    counts = Counter((row["album_artist"] or row["artist"] or "?") for row in rows)
    if q:
        folded = navidrome.name_key(q)
        counts = Counter({
            name: n for name, n in counts.items()
            if folded in navidrome.name_key(name)
        })
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))
    total = len(ordered)
    pages = max(1, (total + STATION_ARTISTS_PER_PAGE - 1) // STATION_ARTISTS_PER_PAGE)
    page = max(1, min(int(page or 1), pages))
    start = (page - 1) * STATION_ARTISTS_PER_PAGE
    items = [
        {"name": name, "tracks": n}
        for name, n in ordered[start:start + STATION_ARTISTS_PER_PAGE]
    ]
    return {
        "station_id": station.id,
        "items": items,
        "page": page,
        "pages": pages,
        "q": q,
        "total": total,
    }


@router.get("/admin/stations/{station_id}/artists", response_class=HTMLResponse)
async def admin_station_artists(request: Request, station_id: str, q: str = "", page: int = 1):
    station = station_or_404(station_id)
    context = await asyncio.to_thread(_station_artist_page, _conn(), station, q, page)
    return templates.TemplateResponse(
        request,
        "_station_artists.html",
        context,
    )


@router.get("/admin/stations", response_class=HTMLResponse)
async def admin_stations(request: Request):
    items = await asyncio.to_thread(_raw_stations)
    return templates.TemplateResponse(
        request, "admin_stations.html", {"items": items, "stations": stations()}
    )


def _station_form_context(item: dict, station) -> dict:
    # Most-used first: the genre you want is nearly always near the top, and the
    # counts say how much a station is about to gain.
    labels = [(row["label"], row["c"]) for row in label_queries.counts_by_tracks(_conn())]
    # The two dropdowns list the record's artists, most recorded first. Any name
    # already selected is kept even if the index no longer has it under that
    # spelling, so saving the form does not silently drop a rule.
    artists = [row["artist"] for row in track_queries.record_artist_counts(_conn())]
    chosen = set(item.get("include_artists") or ()) | set(item.get("exclude_artists") or ())
    have = set(artists)
    artists.extend(sorted(chosen - have, key=str.casefold))
    return {
        "item": item,
        "station": station,
        "preview": pool_preview(station),
        "all_labels": labels,
        "all_artists": artists,
        "station_artists_page": _station_artist_page(_conn(), station),
        "codecs": config.CODECS,
        "labels": config.CODEC_LABEL,
        "extensions": config.CODEC_EXT,
    }


@router.get("/admin/stations/{station_id}", response_class=HTMLResponse)
async def admin_station_form(request: Request, station_id: str):
    items = {item.get("id"): item for item in _raw_stations()}
    item = items.get(station_id)
    if item is None:
        raise HTTPException(404, "no such station")
    station = station_or_404(station_id)
    context = await asyncio.to_thread(_station_form_context, item, station)
    return templates.TemplateResponse(
        request,
        "admin_station.html",
        context,
    )


@router.post("/admin/stations/{station_id}", response_class=HTMLResponse)
async def admin_station_save(
    request: Request,
    station_id: str,
    name: str = Form(...),
    description: str = Form(""),
    genres: list[str] = Form(default=[]),
    exclude_genres: list[str] = Form(default=[]),
    include_artists: list[str] = Form(default=[]),
    exclude_artists: list[str] = Form(default=[]),
    popularity: str = Form("balanced"),
    year_from: str = Form(""),
    year_to: str = Form(""),
    new_id: str = Form(""),
    stream_codec: list[str] = Form(default=[]),
):
    raw = _raw_stations()
    wanted_id = _slug(new_id) or station_id
    renamed = wanted_id != station_id
    if renamed:
        if any(item.get("id") == wanted_id for item in raw):
            return templates.TemplateResponse(
                request,
                "_saved.html",
                {"message": f"There is already a station called “{wanted_id}”. Nothing was saved.",
                 "station_id": station_id},
            )
        moved = _move_state(station_id, wanted_id)
        raw = [
            {**item, "id": wanted_id} if item.get("id") == station_id else item for item in raw
        ]
        station_id = wanted_id
        note = (" Moved " + ", ".join(moved) + ".") if moved else ""
    else:
        note = ""

    # Checkboxes: whatever arrived, in the canonical order, and never nothing.
    wanted_streams = config.parse_codecs(stream_codec) or config.DEFAULT_CODECS
    streams_changed = False
    for item in raw:
        if item.get("id") != station_id:
            continue
        before = config.parse_codecs(item.get("streams"))
        streams_changed = before != wanted_streams
        item.update(
            {
                "streams": list(wanted_streams),
                "name": name.strip() or station_id,
                "description": description.strip(),
                "include_genres": known_labels(genres),
                "exclude_genres": known_labels(exclude_genres),
                "include_artists": [a.strip() for a in include_artists if a.strip()],
                "exclude_artists": [a.strip() for a in exclude_artists if a.strip()],
                "popularity": popularity if popularity in ("deep", "balanced", "hits") else "balanced",
                "year_from": _int_or_none(year_from),
                "year_to": _int_or_none(year_to),
            }
        )
    write_stations(raw)

    def _rebuild_pools() -> dict[str, int]:
        return pool.write_pools(_conn(), store.reload_stations())

    counts = await asyncio.to_thread(_rebuild_pools)
    if note or streams_changed:
        # A new or changed stream is a new Liquidsoap output, so the mixer has to
        # come back with a regenerated script.
        note += " " + await asyncio.to_thread(_apply_station_script)
    message = f"Saved. Rotation: {counts.get(station_id, 0)} tracks.{note}"
    response = templates.TemplateResponse(
        request, "_saved.html", {"message": message, "station_id": station_id}
    )
    if renamed:
        # The form on screen still posts the old id, and still holds it in `new_id`,
        # so the next save would quietly write nothing or rename it back. Land the
        # page on the new id and let it flash what happened.
        response.headers["HX-Redirect"] = f"/admin/stations/{station_id}?saved={quote(message)}"
    return response


@router.post("/admin/new-station", response_class=HTMLResponse)
async def admin_station_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    new_id: str = Form(""),
):
    """Add a station. It needs a new spica.liq, so the mixer is restarted."""
    raw = _raw_stations()
    station_id = _slug(new_id) or _slug(name)
    if not station_id:
        # These two forms are plain HTML posts, not htmx: a fragment here would be
        # the whole page the browser lands on, so the answer is the list with a flash.
        return RedirectResponse(
            "/admin/stations?error=" + quote("A station needs an id: letters, digits and dashes."),
            status_code=303,
        )
    if any(item.get("id") == station_id for item in raw):
        return RedirectResponse(
            "/admin/stations?error=" + quote(f"“{station_id}” already exists."), status_code=303
        )
    raw.append(
        {
            "id": station_id,
            "name": name.strip() or station_id,
            "description": description.strip(),
            "include_genres": [],
            "exclude_genres": [],
            "include_artists": [],
            "exclude_artists": [],
            "popularity": "balanced",
            "year_from": None,
            "year_to": None,
        }
    )
    write_stations(raw)

    def _rebuild_pools() -> None:
        pool.write_pools(_conn(), store.reload_stations())

    await asyncio.to_thread(_rebuild_pools)
    await asyncio.to_thread(_apply_station_script)
    return RedirectResponse(f"/admin/stations/{station_id}?made=1", status_code=303)


@router.post("/admin/stations/{station_id}/delete", response_class=HTMLResponse)
async def admin_station_delete(request: Request, station_id: str):
    """Remove a station. Its history stays on disk, so re-creating the id brings it back."""
    raw = [item for item in _raw_stations() if item.get("id") != station_id]
    if len(raw) == len(_raw_stations()):
        return RedirectResponse(
            "/admin/stations?error=" + quote("No such station."), status_code=303
        )
    if not raw:
        return RedirectResponse(
            f"/admin/stations/{station_id}?error="
            + quote("The last station cannot be deleted — the site needs one to land on."),
            status_code=303,
        )
    write_stations(raw)
    # The pool is derived, so it goes with the station; the history is not, so it
    # stays — re-creating the same id picks the ledger back up.
    for stray in (config.STATE_DIR / "pools" / f"{station_id}.m3u",
                  config.STATE_DIR / "queue" / f"{station_id}.txt"):
        stray.unlink(missing_ok=True)
    def _rebuild_pools() -> None:
        pool.write_pools(_conn(), store.reload_stations())

    await asyncio.to_thread(_rebuild_pools)
    await asyncio.to_thread(_apply_station_script)
    return RedirectResponse(f"/admin/stations?deleted={station_id}", status_code=303)
