"""The station rack: add, rename, delete, and each station's rules."""

from __future__ import annotations

from urllib.parse import quote

from .. import config
from .. import pool
from .. import store
from .. import auth
from fastapi import APIRouter, Form, HTTPException, Request
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

router = APIRouter()


@router.get("/admin/stations", response_class=HTMLResponse)
async def admin_stations(request: Request):
    if not auth.is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    return templates.TemplateResponse(
        request, "admin_stations.html", {"items": _raw_stations(), "stations": stations()}
    )


@router.get("/admin/stations/{station_id}", response_class=HTMLResponse)
async def admin_station_form(request: Request, station_id: str):
    if not auth.is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    items = {item.get("id"): item for item in _raw_stations()}
    item = items.get(station_id)
    if item is None:
        raise HTTPException(404, "no such station")
    # Most-used first: the genre you want is nearly always near the top, and the
    # counts say how much a station is about to gain.
    labels = [
        (row["label"], row["n"])
        for row in _conn().execute(
            "SELECT label, COUNT(*) n FROM track_genres GROUP BY label ORDER BY n DESC, label"
        )
    ]
    # Every artist in the index, most recorded first, for the typeahead.
    artists = [
        row["artist"]
        for row in _conn().execute(
            "SELECT artist, COUNT(*) n FROM tracks GROUP BY artist ORDER BY n DESC, artist"
        )
        if row["artist"]
    ]
    return templates.TemplateResponse(
        request,
        "admin_station.html",
        {
            "item": item,
            "station": station_or_404(station_id),
            "preview": pool_preview(station_or_404(station_id)),
            "all_labels": labels,
            "all_artists": artists,
            "codecs": config.CODECS,
            "labels": config.CODEC_LABEL,
            "extensions": config.CODEC_EXT,
        },
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
    auth.require_admin(request)
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
    counts = pool.write_pools(_conn(), store.reload_stations())
    if note or streams_changed:
        # A new or changed stream is a new Liquidsoap output, so the mixer has to
        # come back with a regenerated script.
        note += " " + _apply_station_script()
    message = f"Saved. Pool: {counts.get(station_id, 0)} tracks.{note}"
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
    auth.require_admin(request)
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
    pool.write_pools(_conn(), store.reload_stations())
    _apply_station_script()
    return RedirectResponse(f"/admin/stations/{station_id}?made=1", status_code=303)


@router.post("/admin/stations/{station_id}/delete", response_class=HTMLResponse)
async def admin_station_delete(request: Request, station_id: str):
    """Remove a station. Its history stays on disk, so re-creating the id brings it back."""
    auth.require_admin(request)
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
    pool.write_pools(_conn(), store.reload_stations())
    note = _apply_station_script()
    return RedirectResponse(f"/admin/stations?deleted={station_id}", status_code=303)
