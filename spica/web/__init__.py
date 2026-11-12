"""The listener site and the admin console.

Server-rendered with Jinja2 and a vendored htmx: no build step, no node, and the
browser only ever talks to this process. The listener side is public; anything
that writes sits under /admin and needs the password from the environment.

The routes are split by audience — listen, media, login, and one module per admin
pane — and share one router here, so `app.include_router(web.router)` is the only
thing the application has to know about. What the rest of the process needs from
here is re-exported below, so it does not have to know which pane a helper lives in.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import admin_genres, admin_home, admin_settings, admin_stations, listen, login, media
from .shared import now_playing, static, templates

router = APIRouter()
for part in (listen, media, login, admin_home, admin_stations, admin_genres, admin_settings):
    router.include_router(part.router)

__all__ = ["router", "static", "templates", "now_playing"]
