"""The listener's page, its fragments, and the stream relay."""

from __future__ import annotations

import asyncio
import httpx

from .. import config
from .. import icecast
from .. import mixer
from .. import picker
from .. import pool
from .. import presence
from .. import queue
from .. import store
from .. import requests as requests_mod
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from .shared import (
    templates,
    stations,
    station_or_404,
    now_playing,
    history_of,
    request_rows,
    station_context,
)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """One page: land on the first station, with every other one a click away."""
    first = stations()[0]
    return RedirectResponse(f"/s/{first.id}", status_code=303)


@router.get("/s/{station_id}", response_class=HTMLResponse)
async def station_page(request: Request, station_id: str):
    st = station_or_404(station_id)
    return templates.TemplateResponse(request, "station.html", station_context(st))


@router.get("/s/{station_id}/body", response_class=HTMLResponse)
async def station_body(request: Request, station_id: str):
    """Just the station, for the bar at the top to swap without a page load."""
    st = station_or_404(station_id)
    return templates.TemplateResponse(
        request, "_station_body.html", station_context(st)
    )


@router.get("/s/{station_id}/live", response_class=HTMLResponse)
async def station_live(request: Request, station_id: str):
    """The now-playing block on its own — what the page polls every five seconds."""
    st = station_or_404(station_id)
    # One reading, and only what the fragment renders: the ledger has its own
    # endpoint, and this used to build history and the request list as well.
    return templates.TemplateResponse(request, "_live.html", {"now": now_playing(st)})


@router.post("/s/{station_id}/hear", status_code=204)
async def hear(station_id: str):
    """The page saying it is still playing. Cheap, public, and the only honest signal
    that a browser has not just left a socket open."""
    station_or_404(station_id)
    presence.heard(station_id)
    return Response(status_code=204)


async def _relay(st, codec, request: Request):
    """Open one stream's mount if needed, then copy the bytes to the client.

    Mounts start closed, so a client that cannot run the page (Music Assistant,
    VLC, a car head unit) would otherwise get a 404. This makes the station behave
    like an ordinary always-on stream: the picker opens that one output, waits for
    Icecast, and relays it.
    """
    mount = st.mount_for(codec)
    await asyncio.to_thread(mixer.start, st, codec)
    for _ in range(40):
        # Somebody is waiting for a specific mount to appear, so this is the one
        # place worth reading Icecast out of turn — the shared snapshot is a couple
        # of seconds old by design.
        states = await asyncio.to_thread(icecast.refresh)
        if mount in states:
            break
        await asyncio.sleep(0.25)

    client = httpx.AsyncClient(timeout=None)
    upstream = await client.send(
        client.build_request("GET", f"{config.ICECAST_URL}{mount}"), stream=True
    )
    if upstream.status_code != 200:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(503, "stream not on air")

    token = presence.relay_open(st.id, request.headers.get("user-agent", ""))

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
                presence.relay_tick(st.id, token)
        finally:
            presence.relay_close(st.id, token)
            await upstream.aclose()
            await client.aclose()

    icy = st.name if codec == st.stream_set[0] else f"{st.name} ({config.CODEC_LABEL[codec]})"
    return StreamingResponse(
        relay(),
        media_type=config.CODEC_MEDIA[codec],
        headers={
            "icy-name": icy,
            "cache-control": "no-store",
            "x-accel-buffering": "no",
            # A live stream has no beginning to seek to. Without this, Chrome
            # probes with Range requests and reconnects every twenty seconds.
            "accept-ranges": "none",
        },
    )


@router.get("/listen/{station_id}")
async def listen(station_id: str, request: Request):
    """The station's default stream, as a URL any player can hold."""
    st = station_or_404(station_id)
    return await _relay(st, st.stream_set[0], request)


@router.get("/listen/{station_id}/{codec}")
async def listen_stream(station_id: str, codec: str, request: Request):
    """A specific stream: /listen/alt/opus, /listen/alt/flac."""
    st = station_or_404(station_id)
    codec = codec.strip().lower()
    if not st.has_codec(codec):
        raise HTTPException(404, f"{st.name} does not publish {codec}")
    return await _relay(st, codec, request)


@router.post("/s/{station_id}/start")
async def station_start(station_id: str, stream: str = ""):
    st = station_or_404(station_id)
    chosen = stream.strip().lower() if stream else ""
    started = await asyncio.to_thread(mixer.start, st, chosen or None)
    return JSONResponse({"started": started, "codec": chosen or st.stream_set[0]})


@router.get("/s/{station_id}/state")
async def station_state(station_id: str, stream: str = ""):
    st = station_or_404(station_id)
    chosen = (stream or "").strip().lower() or st.stream_set[0]
    states = icecast.listen_states()
    if st.mount_for(chosen) not in states:
        # The player asks about a stream it has just asked us to open, and it is
        # polling every 350 ms: give it a fresh answer rather than one from before
        # the mount existed.
        states = await asyncio.to_thread(icecast.refresh)
    live = icecast.live_mount(states, st)
    return JSONResponse(
        {
            "live": live is not None,
            "listeners": icecast.total_listeners(states, st),
            "stream": chosen,
            "stream_live": st.mount_for(chosen) in states,
            "streams": [
                {
                    "codec": c,
                    "label": config.CODEC_LABEL[c],
                    "mount": st.mount_for(c),
                    "listeners": states[st.mount_for(c)].listeners if st.mount_for(c) in states else 0,
                    "live": st.mount_for(c) in states,
                }
                for c in st.stream_set
            ],
        }
    )


@router.get("/s/{station_id}/history", response_class=HTMLResponse)
async def station_history(request: Request, station_id: str):
    st = station_or_404(station_id)
    return templates.TemplateResponse(
        request,
        "_history.html",
        {"station": st, "history": history_of(st, 15, now_playing(st))},
    )


@router.get("/s/{station_id}/requests", response_class=HTMLResponse)
async def station_requests(request: Request, station_id: str):
    """The list of requests in force. The form beside it is never replaced."""
    st = station_or_404(station_id)
    return templates.TemplateResponse(request, "_request_list.html", {"requests": request_rows(st)})


@router.post("/s/{station_id}/request", response_class=HTMLResponse)
async def station_request(request: Request, station_id: str, text: str = Form(...)):
    st = station_or_404(station_id)
    entry = requests_mod.record(st, text, requests_mod.DEFAULT_SONGS)
    pick = await asyncio.to_thread(
        picker.pick_next, store.library(), st, text, queue.recent_ids(st)
    )
    message = "Pedido registrado. La próxima elección lo va a tener en cuenta."
    if pick is not None:
        # The request goes next, whatever was waiting. The track it displaces is
        # back at the head of the pool, so it plays after this one instead of
        # disappearing for a rotation.
        displaced = queue.replace(st, pick.track.uri, pick.track.as_line())
        if displaced:
            pool.rewind_to(st, displaced)
        else:
            pool.skip_past([st], pick.track.id)
        queue.record_pick(st, pick.track.id, pick.track.title, pick.track.artist, pick.why)
        requests_mod.attach_track(st, entry, pick.track)
        message = f"{pick.track.as_line()} — {pick.why}"
    return templates.TemplateResponse(
        request,
        "_request_result.html",
        {"station": st, "message": message, "requests": request_rows(st)},
    )
