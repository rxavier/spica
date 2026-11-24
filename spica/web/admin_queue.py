"""What the queue is doing, and what the picker saw when it filled it.

The panes elsewhere answer "is the library healthy" and "what is on air". This one
answers the question that comes up at midnight: why *this* track, right after *that*
one — with the prompt context the picker read, the tools it called and the reply it gave, beside
what the station actually did with it.

Read-only and deliberately plain: the pick log is the record, this page is a window
onto it. When something new needs watching (a request's turn, the pool's position,
whatever comes next), it goes here.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse

from .. import auth
from .. import config, ledger
from .. import icecast
from .. import mixer
from .. import plan
from .. import presence
from .. import queue
from .. import store
from .. import requests as requests_mod
from .shared import LOCAL_TZ, now_playing, templates

log = logging.getLogger("radio")

router = APIRouter(dependencies=[Depends(auth.require_admin_page)])

# How many picks the pane shows. The log keeps more; this is what fits a screen
# without turning "read the last few" into a scroll.
ROWS = 100


def _local(iso: str) -> str:
    """An ISO stamp in the station's own clock, or the raw string if it will not parse."""
    moment = ledger.parse_stamp(iso)
    if moment is None:
        return iso or ""
    return datetime.fromtimestamp(moment, tz=LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _stations_view() -> list[dict]:
    states = icecast.listen_states()
    out = []
    for st in store.stations():
        live = icecast.live_mount(states, st)
        n = live[1].listeners if live else 0
        picks = []
        for entry in reversed(ledger.recent_picks(st, ROWS)):
            picks.append(
                {
                    "at": _local(entry.get("t", "")),
                    "title": entry.get("title", ""),
                    "artist": entry.get("artist", ""),
                    "why": entry.get("reason", ""),
                    "presence": entry.get("presence", ""),
                    "prompt_context": entry.get("prompt_context", ""),
                    "reply": entry.get("reply", ""),
                    "tools": entry.get("tools") or [],
                    "path": entry.get("strategy", ""),
                    "path_label": {
                        "tracks": "Tracks",
                        "tracks-partial": "Tracks (partial)",
                        "artists": "Artists",
                    }.get(entry.get("strategy", ""), ""),
                    "coverage": entry.get("coverage"),
                    "candidate_artists": entry.get("candidate_artists") or [],
                }
            )
        held = plan.load(st) or {}
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
                "set_rationale": str(held.get("set_rationale") or ""),
                "set_remaining": len(held.get("ids") or []),
                "picks": picks,
            }
        )
    return out


@router.get("/admin/queue", response_class=HTMLResponse)
def admin_queue(request: Request):
    views = _stations_view()
    wanted = (request.query_params.get("station") or "").strip()
    selected = next((v for v in views if v["station"].id == wanted), views[0] if views else None)
    mode = config.dj_mode()
    return templates.TemplateResponse(
        request,
        "admin_queue.html",
        {
            "views": views,
            "view": selected,
            "rows": ROWS,
            "mode_label": config.DJ_MODE_LABELS.get(mode, mode),
        },
    )
