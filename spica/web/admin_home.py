"""The console's front page, the job table and the library page."""

from __future__ import annotations

import os

from .. import config
from .. import jobs
from .. import navidrome
from .. import pool
from .. import store
from .. import subsonic
from .. import auth
from fastapi import APIRouter, Form, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
)

from .shared import (
    templates,
    stations,
    _restart_mixer,
    _conn,
)

router = APIRouter()


@router.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request):
    if not auth.is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"coverage": jobs.coverage(_conn()), "stations": stations(), "jobs": jobs.recent(_conn(), 5)},
    )


@router.get("/admin/jobs", response_class=HTMLResponse)
async def admin_jobs(request: Request):
    if not auth.is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    return templates.TemplateResponse(
        request, "_jobs.html", {"jobs": jobs.recent(_conn(), 10)}
    )


@router.post("/admin/jobs", response_class=HTMLResponse)
async def admin_job_create(request: Request, kind: str = Form(...)):
    auth.require_admin(request)
    conn = _conn()
    try:
        jobs.create(conn, kind)
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "_jobs.html", {"jobs": jobs.recent(conn, 10), "error": str(exc)}
        )
    return templates.TemplateResponse(
        request, "_jobs.html", {"jobs": jobs.recent(conn, 10)}
    )


@router.get("/admin/library", response_class=HTMLResponse)
async def admin_library(request: Request):
    if not auth.is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    conn = _conn()
    try:
        status = subsonic.scan_status()
        scan = (status.get("lastScan") or "")[:16].replace("T", " ")
        library_tracks, library_error = status.get("count"), ""
    except Exception as exc:  # Navidrome down, or the credentials are wrong
        scan, library_tracks = "", None
        library_error = f"Navidrome did not answer: {exc}"
    return templates.TemplateResponse(
        request,
        "admin_library.html",
        {
            "coverage": jobs.coverage(conn),
            "jobs": jobs.recent(conn, 10),
            "index_tracks": conn.execute("SELECT count(*) FROM tracks").fetchone()[0],
            "library_tracks": library_tracks,
            "library_scan": scan,
            "library_error": library_error,
            "check_minutes": max(1, config.REFRESH_CHECK_S // 60),
        },
    )


@router.post("/admin/rebuild", response_class=HTMLResponse)
async def admin_rebuild(request: Request):
    auth.require_admin(request)

    conn = _conn()
    n = navidrome.rebuild_index(conn, log=lambda *_: None)
    store.forget_tracks()
    counts = pool.write_pools(conn, store.reload_stations())
    return templates.TemplateResponse(
        request,
        "_saved.html",
        {
            "message": f"Índice: {n} tracks. Pools: " + ", ".join(f"{k} {v}" for k, v in counts.items()),
            "station_id": "",
        },
    )


@router.post("/admin/reload", response_class=HTMLResponse)
async def admin_reload(request: Request):
    auth.require_admin(request)
    import os

    from .. import liqgen

    password = os.environ.get("ICECAST_SOURCE_PASSWORD", "")
    target = config.CONFIG_DIR / "spica.liq"
    target.write_text(liqgen.render(store.reload_stations(), password), encoding="utf-8")
    return templates.TemplateResponse(
        request, "_saved.html", {"message": _restart_mixer(), "station_id": ""}
    )
