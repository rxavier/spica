"""What the queue is doing, and what the DJ saw when it filled it.

The panes elsewhere answer "is the library healthy" and "what is on air". This one
answers the question that comes up at midnight: why *this* track, right after *that*
one — with the brief the DJ read, the tools it called and the reply it gave, beside
what the station actually did with it.

Read-only and deliberately plain: the pick log is the record, this page is a window
onto it. When something new needs watching (a request's turn, the pool's position,
whatever comes next), it goes here.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import icecast
from .. import mixer
from .. import presence
from .. import queue
from .. import store
from .. import requests as requests_mod
from .shared import LOCAL_TZ, now_playing, stations, templates

log = logging.getLogger("radio")

router = APIRouter()

# How many picks the pane shows. The log keeps more; this is what fits a screen
# without turning "read the last few" into a scroll.
ROWS = 100


def _local(iso: str) -> str:
    """An ISO stamp in the station's own clock, or the raw string if it will not parse."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(
            LOCAL_TZ
        ).strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return iso or ""


def _stations_view() -> list[dict]:
    states = icecast.listen_states()
    out = []
    for st in stations():
        live = icecast.live_mount(states, st)
        n = live[1].listeners if live else 0
        picks = []
        for entry in reversed(queue.recent_picks(st, ROWS)):
            picks.append(
                {
                    "at": _local(entry.get("t", "")),
                    "title": entry.get("title", ""),
                    "artist": entry.get("artist", ""),
                    "why": entry.get("why", ""),
                    "presence": entry.get("presence", ""),
                    "brief": entry.get("brief", ""),
                    "reply": entry.get("reply", ""),
                    "tools": entry.get("tools") or [],
                }
            )
        out.append(
            {
                "station": st,
                "now": now_playing(st),
                "listeners": n,
                "listening": presence.listening(st.id, n),
                "present_reason": presence.reason(st.id, n),
                "slot": queue.queued(st),
                "ahead": mixer.pending(st),
                "requests": requests_mod.active(st),
                "picks": picks,
            }
        )
    return out


@router.get("/admin/queue", response_class=HTMLResponse)
def admin_queue(request: Request):
    return templates.TemplateResponse(
        request,
        "admin_queue.html",
        {"views": _stations_view(), "rows": ROWS},
    )
