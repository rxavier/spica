"""The genre vocabulary: merge, assign by hand, and explore tracks."""

from __future__ import annotations

import json
from urllib.parse import urlencode

from .. import navidrome
from .. import pool
from .. import store
from .. import auth
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
)

from .shared import (
    known_labels,
    templates,
    stations,
    _conn,
)

router = APIRouter()


@router.get("/admin/genres", response_class=HTMLResponse)
async def admin_genres(request: Request, msg: str = "", merged: str = "", into: str = ""):
    if not auth.is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    from .. import genres as genre_lib
    from ..navidrome import genre_counts

    conn = _conn()
    untagged = conn.execute(
        "SELECT COUNT(*) c FROM tracks t WHERE NOT EXISTS"
        " (SELECT 1 FROM track_genres g WHERE g.track_id = t.id)"
    ).fetchone()["c"]
    return templates.TemplateResponse(
        request,
        "admin_genres.html",
        {
            "msg": msg or (f"Merged {merged} label(s) into {into}; the pools were rebuilt."
                           if merged and into else ""),
            "untagged": untagged,
            "counts": genre_counts(conn),
            "operator": genre_lib.operator_aliases(),
            "builtin_count": len(genre_lib.BUILTIN),
            "total_tracks": conn.execute("SELECT COUNT(*) c FROM tracks").fetchone()["c"],
        },
    )


def _genres_page(message: str) -> RedirectResponse:
    """Back to the pane with a line saying what just happened.

    A 303 is followed by the browser as a page load, so the tables, the counts and the
    scroll are rebuilt from the database — the earlier HX-Refresh on a redirect was
    dropped by the fetch, which is why a merge looked like it did nothing.
    """
    return RedirectResponse(f"/admin/genres?{urlencode({'msg': message})}", status_code=303)


@router.post("/admin/genres/merge", response_class=HTMLResponse)
async def admin_genres_merge(
    request: Request,
    labels: list[str] = Form(default=[]),
    into: str = Form(""),
):
    auth.require_admin(request)
    from .. import genres as genre_lib

    conn = _conn()
    moved = 0
    if labels and into.strip():
        moved = genre_lib.merge(conn, labels, into)
        if moved:
            pool.write_pools(conn, store.stations())
    if not labels:
        return _genres_page("Nothing was selected, so nothing changed.")
    if not moved:
        return _genres_page(f"Everything selected was already called {genre_lib.pretty(into)}.")
    return _genres_page(
        f"Merged {moved} label{'s' if moved > 1 else ''} into {genre_lib.pretty(into)}; "
        "the pools were rebuilt."
    )


@router.post("/admin/genres/add", response_class=HTMLResponse)
async def admin_genres_add(request: Request, source: str = Form(...), target: str = Form(...)):
    auth.require_admin(request)
    from .. import genres as genre_lib

    conn = _conn()
    genre_lib.merge(conn, [source], target)
    return _genres_page(f"Mapped {source} to {genre_lib.pretty(target)}.")


@router.post("/admin/genres/delete", response_class=HTMLResponse)
async def admin_genres_delete(request: Request, source: str = Form(...)):
    auth.require_admin(request)
    from .. import genres as genre_lib

    path = genre_lib._FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {"aliases": []}
    raw["aliases"] = [a for a in raw.get("aliases", []) if a.get("from") != source]
    path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return _genres_page(f"Removed the rule for {source}.")


def _all_labels(conn) -> list[str]:
    return [
        row["label"]
        for row in conn.execute("SELECT label FROM track_genres GROUP BY label ORDER BY label")
    ]


@router.post("/admin/genres/unassign", response_class=HTMLResponse)
async def admin_genres_unassign(
    request: Request,
    tracks: list[str] = Form(default=[]),
    label: str = Form(""),
    labels: list[str] = Form(default=[]),
):
    """Take a label off the chosen tracks, and remember the refusal so a rebuild
    does not quietly put it back."""
    auth.require_admin(request)
    from .. import genres as genre_lib

    conn = _conn()
    name = genre_lib.pretty(label.strip())
    if tracks and name:
        with conn:
            conn.executemany(
                "INSERT OR IGNORE INTO track_genre_veto (track_id, label) VALUES (?,?)",
                [(track, name) for track in tracks],
            )
            conn.executemany(
                "DELETE FROM track_genres WHERE track_id = ? AND lower(label) = lower(?)",
                [(track, name) for track in tracks],
            )
        pool.write_pools(conn, store.stations())
    return templates.TemplateResponse(
        request,
        "_genre_tracks.html",
        {
            "labels": labels,
            "all_labels": _all_labels(conn),
            **navidrome.label_explorer(conn, labels),
        },
    )


@router.get("/admin/genres/tracks", response_class=HTMLResponse)
async def admin_genres_tracks(request: Request, label: list[str] = Query(default=[])):
    if not auth.is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    conn = _conn()
    return templates.TemplateResponse(
        request,
        "_genre_tracks.html",
        {
            "labels": label,
            "all_labels": _all_labels(conn),
            **navidrome.label_explorer(conn, label),
        },
    )


@router.post("/admin/genres/assign", response_class=HTMLResponse)
async def admin_genres_assign(
    request: Request,
    tracks: list[str] = Form(default=[]),
    label: str = Form(""),
    labels: list[str] = Form(default=[]),
):
    """Put a label on the chosen tracks by hand: it outranks every provider."""
    auth.require_admin(request)
    from .. import genres as genre_lib

    conn = _conn()
    name = genre_lib.pretty(label.strip())
    if tracks and name:
        with conn:
            conn.executemany(
                "INSERT OR IGNORE INTO track_genres (track_id, label, source) VALUES (?,?, 'manual')",
                [(track, name) for track in tracks],
            )
        pool.write_pools(conn, store.stations())
    return templates.TemplateResponse(
        request,
        "_genre_tracks.html",
        {
            "labels": labels,
            "all_labels": _all_labels(conn),
            **navidrome.label_explorer(conn, labels),
        },
    )
