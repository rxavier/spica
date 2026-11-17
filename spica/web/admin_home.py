"""The console's front page, the job table and the library page."""

from __future__ import annotations

import os
from datetime import datetime

from .. import analysis
from .. import config
from .. import jobs
from .. import loudness
from .. import navidrome
from .. import pool
from .. import queue
from .. import store
from .. import subsonic
from .. import auth
from fastapi import APIRouter, Form, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
)

from .shared import (
    LOCAL_TZ,
    templates,
    _restart_mixer,
    _conn,
)

router = APIRouter()


def _when(iso: str) -> str:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(
            LOCAL_TZ
        ).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(iso or "")


@router.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request):
    """The console has no dashboard: stations are the first thing you configure."""
    if not auth.is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    return RedirectResponse("/admin/stations", status_code=303)


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
    coverage = jobs.coverage(conn)
    analysis_coverage = analysis.coverage_for(conn)
    if analysis_coverage is not None:
        coverage["analysis"] = analysis_coverage
    loudness_coverage = loudness.coverage(conn)
    if loudness_coverage is not None:
        coverage["loudness"] = loudness_coverage
    analysis_stations = (analysis_coverage or {}).get("stations") or {}
    analysis_on = analysis.enabled()
    dj = []
    for st in config.load_stations():
        picks = queue.recent_picks(st, 1)
        last = picks[-1] if picks else {}
        path = str(last.get("path") or "")
        cov = last.get("coverage")
        a = analysis_stations.get(st.id) or {}
        if not analysis_on:
            offer = "shelf"
            why = "Audio analysis is off; the DJ uses the artist shelf."
        elif path:
            offer = path
            why = {
                "table": "The last set came from the record table.",
                "table-partial": "The last set came from the table while the backfill was partial.",
                "shelf": "The last set fell back to the artist shelf.",
            }.get(path, "The last set used the " + path + " path.")
        elif a.get("percent", 0) >= 90:
            # The table also needs an artist centroid and an on-air anchor; the
            # coverage is the part the library page can state without drawing one.
            offer = "table ready"
            why = ("Analysis is ready on this station; the next set uses the table "
                   "unless there is no record on air to anchor it.")
        elif a:
            offer = "shelf"
            why = (f"Analysis is at {a.get('percent', 0)}% on this station; the shelf "
                   "is the offer until it reaches 90%.")
        else:
            offer = "shelf"
            why = "No measured tracks on this station yet; the shelf is the offer."
        dj.append({
            "station": st,
            "offer": offer,
            "why": why,
            "last_at": _when(last.get("t")),
            "last_coverage": round(cov * 100, 1) if cov is not None else None,
            "shelf": len(last.get("shelf") or []),
            "analysis": a,
        })
    return templates.TemplateResponse(
        request,
        "admin_library.html",
        {
            "coverage": coverage,
            "analysis_enabled": analysis_on,
            "providers": config.provider_settings(),
            "dj": dj,
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
