"""The genre vocabulary: merge, assign by hand, and explore tracks."""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode

from .. import genres as genre_lib
from .. import navidrome
from .. import pool
from .. import store
from .. import auth
from ..queries import labels as label_queries
from ..queries import tracks as track_queries
from fastapi import APIRouter, Form, Query, Request, Depends
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
)

from .shared import (
    templates,
)

router = APIRouter(dependencies=[Depends(auth.require_admin_page)])


@router.get("/admin/genres", response_class=HTMLResponse)
async def admin_genres(request: Request, msg: str = "", merged: str = "", into: str = ""):

    conn = store.conn()
    untagged = await asyncio.to_thread(track_queries.untagged_count, conn)
    counts = await asyncio.to_thread(label_queries.genre_counts, conn)
    operator = await asyncio.to_thread(genre_lib.operator_aliases)
    total = await asyncio.to_thread(
        lambda: track_queries.library_size(conn, playable=False)
    )
    return templates.TemplateResponse(
        request,
        "admin_genres.html",
        {
            "msg": msg or (f"Merged {merged} label(s) into {into}; the station rotations were rebuilt."
                           if merged and into else ""),
            "untagged": untagged,
            "counts": counts,
            "operator": operator,
            "builtin_count": len(genre_lib.BUILTIN),
            "total_tracks": total,
        },
    )


def _genres_page(message: str) -> RedirectResponse:
    """Back to the pane with a line saying what just happened.

    A 303 is followed by the browser as a page load, so the tables, the counts and the
    scroll are rebuilt from the database.
    """
    return RedirectResponse(f"/admin/genres?{urlencode({'msg': message})}", status_code=303)


@router.post("/admin/genres/merge", response_class=HTMLResponse)
async def admin_genres_merge(
    request: Request,
    labels: list[str] = Form(default=[]),
    into: str = Form(""),
    relabel: str = Form(""),
):

    conn = store.conn()
    covered = 0
    if labels and into.strip():
        covered = await asyncio.to_thread(
            genre_lib.merge, conn, labels, into, relabel=bool(relabel)
        )
        if covered:
            await asyncio.to_thread(pool.write_pools, conn, store.stations())
    if not labels:
        return _genres_page("Nothing was selected, so nothing changed.")
    if not covered:
        return _genres_page(f"Everything selected was already called {genre_lib.pretty(into)}.")
    verb = "relabelled and folded into" if relabel else "now answers"
    return _genres_page(
        f"{covered} label{'s' if covered > 1 else ''} {verb} {genre_lib.pretty(into)}; "
        "the station rotations were rebuilt."
    )


@router.post("/admin/genres/add", response_class=HTMLResponse)
async def admin_genres_add(request: Request, source: str = Form(...), target: str = Form(...)):

    conn = store.conn()
    await asyncio.to_thread(genre_lib.merge, conn, [source], target)
    return _genres_page(f"Mapped {source} to {genre_lib.pretty(target)}.")


@router.post("/admin/genres/delete", response_class=HTMLResponse)
async def admin_genres_delete(request: Request, source: str = Form(...)):

    conn = store.conn()
    removed = await asyncio.to_thread(genre_lib.remove_alias, source)
    if removed:
        await asyncio.to_thread(pool.write_pools, conn, store.stations())
    note = "" if removed else " (there was no such rule)"
    return _genres_page(f"Removed the rule for {source}.{note}")


def _all_labels(conn) -> list[str]:
    return label_queries.distinct(conn)


def _genre_tracks_context(conn, labels: list[str]) -> dict:
    return {
        "labels": labels,
        "all_labels": _all_labels(conn),
        **navidrome.label_explorer(conn, labels),
    }


@router.post("/admin/genres/unassign", response_class=HTMLResponse)
async def admin_genres_unassign(
    request: Request,
    tracks: list[str] = Form(default=[]),
    label: str = Form(""),
    labels: list[str] = Form(default=[]),
):
    """Take a label off the chosen tracks, and remember the refusal so a rebuild
    does not quietly put it back."""

    conn = store.conn()
    name = genre_lib.pretty(label.strip())
    if tracks and name:
        def _unassign() -> None:
            with conn:
                label_queries.veto(conn, [(track, name) for track in tracks])
                label_queries.delete_for_tracks(conn, tracks, name)
            pool.write_pools(conn, store.stations())

        await asyncio.to_thread(_unassign)
    context = await asyncio.to_thread(_genre_tracks_context, conn, labels)
    return templates.TemplateResponse(request, "_genre_tracks.html", context)


@router.get("/admin/genres/tracks", response_class=HTMLResponse)
async def admin_genres_tracks(request: Request, label: list[str] = Query(default=[])):
    conn = store.conn()
    context = await asyncio.to_thread(_genre_tracks_context, conn, label)
    return templates.TemplateResponse(request, "_genre_tracks.html", context)


@router.post("/admin/genres/assign", response_class=HTMLResponse)
async def admin_genres_assign(
    request: Request,
    tracks: list[str] = Form(default=[]),
    label: str = Form(""),
    labels: list[str] = Form(default=[]),
):
    """Put a label on the chosen tracks by hand: it outranks every provider."""

    conn = store.conn()
    name = genre_lib.pretty(label.strip())
    if tracks and name:
        def _assign() -> None:
            with conn:
                label_queries.insert(conn, [(track, name) for track in tracks], "manual")
            pool.write_pools(conn, store.stations())

        await asyncio.to_thread(_assign)
    context = await asyncio.to_thread(_genre_tracks_context, conn, labels)
    return templates.TemplateResponse(request, "_genre_tracks.html", context)
