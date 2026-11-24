"""HTTP surface + the loop that feeds listened-to stations.

One process: uvicorn serves /status and /request while a background task asks
the picker for a next track only on stations that have a listener.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from . import air
from . import analysis
from . import bootstrap
from . import config, icecast, mixer, navidrome, picker, plays, pool, presence, queue, store, web
from . import enrich as enrich_mod
from . import genres as genre_lib
from . import jobs
from . import liqgen
from . import loudness
from . import requests as requests_mod
from .queries import tracks as track_queries

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("radio")

LAST = air.LAST
QUIET_SINCE: dict[str, float] = {}
INDEX_STAMP: list[float] = [0.0]


async def loop() -> None:
    while True:
        try:
            stamp = store.db_path().stat().st_mtime if store.db_path().exists() else 0.0
            if stamp and stamp != INDEX_STAMP[0]:
                if INDEX_STAMP[0]:
                    store.forget_tracks()  # the worker rebuilt the index underneath it
                    log.info("index changed on disk — reloaded the track lookup")
                INDEX_STAMP[0] = stamp
            states = icecast.listen_states()
            for st in store.stations():
                # The mixer stamps every track it starts, which is the only witness
                # that keeps working when the mount is closed and the station plays on.
                fresh = await asyncio.to_thread(mixer.now_lines, st)
                if st.now_file.exists():
                    for line in fresh:
                        plays.note(st, line)
                else:
                    plays.note(st, states.get(st.mount).title if states.get(st.mount) else "")
                _close_if_silent(st, states)
                if queue.queued(st):
                    continue
                # A request that could not be promoted into the mixer's own queue
                # (control server down when it arrived) airs here instead; the
                # normal path promotes it immediately and never reaches this.
                due = queue.take_request(st)
                if due is not None:
                    displaced = queue.replace(st, due["uri"], due.get("label", ""))
                    if displaced:  # nothing should be waiting, but never drop one
                        await asyncio.to_thread(pool.rewind_to, st, displaced, log=log.info)
                    log.info("[%s] aired the request: %s", st.id, due.get("label", ""))
                    continue
                # No live mount among the station's streams means nobody can hear it,
                # whatever presence says. Note this also pauses picking for a poll or
                # two if Icecast itself hiccups and returns nothing: with no states,
                # every mount reads as absent. That is the right trade — nobody can
                # hear anything during an Icecast outage.
                if icecast.live_mount(states, st) is None:
                    LAST[st.id] = {**LAST.get(st.id, {}), "why": "off air — no mount"}
                    continue
                n = icecast.total_listeners(states, st)
                presence_now = presence.report(st.id, n)
                if not presence_now["listening"]:
                    LAST[st.id] = {**LAST.get(st.id, {}), "listeners": n,
                                   "why": presence_now["reason"]}
                    continue
                # Keep exactly one track waiting: the queue holds what is on air
                # too, so 1 means "playing, nothing after it". Picking further
                # ahead makes the reason describe a track that played minutes ago.
                ahead = await asyncio.to_thread(mixer.pending, st)
                if ahead is None:
                    # No control connection: fall back to a cooldown so a broken
                    # telnet can never stack picks again.
                    if time.time() - LAST.get(st.id, {}).get("pushed", 0.0) < config.PICK_COOLDOWN:
                        continue
                elif ahead >= config.KEEP_AHEAD:
                    # One waiting means one waiting: `>=` keeps the loop from
                    # stocking a second pick while the first is still queued.
                    continue
                p = await asyncio.to_thread(
                    picker.pick_next, store.library(), st, None, queue.recent_ids(st)
                )
                if p is None:
                    LAST[st.id] = {**LAST.get(st.id, {}), "listeners": n, "why": "pick failed — playlist continues"}
                    continue
                aired = await asyncio.to_thread(air.air_pick, st, p, log=log.info)
                if not aired.ok:
                    # Someone else filled the slot while the picker was thinking — a
                    # listener request, usually. Their track airs; this pick is
                    # dropped rather than replacing it, and is not logged as played.
                    LAST[st.id] = {**LAST.get(st.id, {}), "listeners": n, "why": aired.why}
                    log.info("[%s] dropped a pick: %s is already waiting", st.id, queue.queued(st))
                    continue
                log.info("[%s] queued %s — %s (%s)", st.id, p.track.title, p.track.artist, p.reason[:60])
        except Exception as exc:  # keep the loop alive whatever happens
            log.warning("loop error: %s", exc)
        await asyncio.sleep(config.POLL_S)


def _close_if_silent(st: config.Station, states: dict) -> None:
    """Stop encoding any stream of this station that nobody is listening to.

    Per stream, not per station: an Opus listener should not keep the FLAC encoder
    running.
    """
    for codec in st.stream_set:
        mount = st.mount_for(codec)
        key = f"{st.id}/{codec}"
        state = states.get(mount)
        if state is None or state.listeners > 0:
            QUIET_SINCE.pop(key, None)
            continue
        since = QUIET_SINCE.setdefault(key, time.time())
        if time.time() - since >= config.IDLE_STOP_S:
            if mixer.stop(st, codec):
                log.info("[%s] closed %s after %ds without listeners",
                         st.id, codec, config.IDLE_STOP_S)
            QUIET_SINCE.pop(key, None)


class RequestBody(BaseModel):
    text: str
    station: str | None = None
    # How many tracks the request is remembered for. A vibe change is a
    # direction for a set, not for one song.
    songs: int | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap.ensure()
    # Build the one shared Library now so the loop and the web handlers agree on
    # its caches and this pick's draw seed.
    store.library()
    # One thread reads Icecast's status page for everybody; see icecast.py.
    icecast.start_refresher()
    # Outputs start closed, so this cannot disturb anyone: the pool simply
    # begins where the station left off instead of rewinding to line one.
    pool.resume_after_last_played(
        store.stations(), lambda st: (queue.recent(st, 1) or [{}])[-1], log=log.info
    )
    task = asyncio.create_task(loop())
    yield
    task.cancel()


app = FastAPI(title="radio", lifespan=lifespan)
app.include_router(web.router)
app.mount("/static", web.static, name="static")


@app.middleware("http")
async def no_stale_assets(request, call_next):
    """A deploy must show up on the next refresh, not the next cache expiry.

    The page, its CSS and its JS change together, and a browser holding yesterday's
    player.js next to today's markup is a broken button — so all three revalidate.
    """
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    path = request.url.path
    if ctype.startswith("text/html") or (path.startswith("/static/") and path.endswith((".js", ".css"))):
        response.headers["cache-control"] = "no-store"
    return response


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "llm": bool(config.llm_settings()["key"] and config.llm_settings()["base"]),
        "model": config.llm_settings()["model"] or None,
        "stations": [st.id for st in store.stations()],
    }


@app.get("/status")
async def status(request: Request) -> dict:
    """What every station is doing, for anything that is not the web page.

    Carries the track split into artist and title, plus an absolute URL to its
    cover, so a dashboard or a Home Assistant sensor does not have to scrape the
    page or know about album ids.
    """
    states = icecast.listen_states()
    # Behind Caddy the request arrives over http, so base_url would hand out http://
    # links on an https page. The public address, when configured, is the truth.
    base = config.PUBLIC_URL or str(request.base_url).rstrip("/")
    out = {}
    for st in store.stations():
        live = icecast.live_mount(states, st)
        mount = live[1] if live else None
        now = await asyncio.to_thread(web.now_playing, st, states)
        art = ""
        if now.get("album_id"):
            art = f"{base}/art/{now['album_id']}"
            if now.get("cover_tag"):
                art += f"?v={now['cover_tag']}"
        streams = web.stream_states(st, states)
        out[st.id] = {
            "name": st.name,
            "mount": st.mount,
            "streams": streams,
            "listeners": mount.listeners if mount else 0,
            "on_air": mount.title if mount else "",
            # The same fields the page shows, for clients that are not the page.
            "artist": now.get("artist", ""),
            "title": now.get("song", ""),
            "album": now.get("album", ""),
            "art": art,
            # Per-station URL that answers with the station's picture; the Spica mark
            # for everyone, since stations do not have their own yet.
            "logo": f"{base}/logo/{st.id}.png",
            "queued": queue.queued(st),
            "pool": web.pool_size(st),
            "last": LAST.get(st.id, {}),
            "presence": {
                **presence.relay_summary(st.id),
                "heartbeat_age": round(presence.heartbeat_age(st.id), 1),
                "listening": presence.listening(st.id, mount.listeners if mount else 0),
            },
        }
    return {"stations": out, "icecast": config.ICECAST_URL}


async def pick_for(st: config.Station, text: str | None = None) -> picker.Pick | None:
    """Ask the picker for one track on this station and hand it to the mixer."""
    if not (config.llm_settings()["key"] and config.llm_settings()["base"]):
        return None
    p = await asyncio.to_thread(picker.pick_next, store.library(), st, text, queue.recent_ids(st))
    if p is None:
        return None
    # A person asked for this, so it goes next even if something was already
    # waiting; the displaced track goes back to the top of the pool.
    await asyncio.to_thread(air.air_pick, st, p, displace=True, log=log.info)
    return p


@app.post("/request")
async def request_track(body: RequestBody) -> dict:
    """Free text -> one track, pushed to the named station (default: first)."""
    st = web.station_or_404(body.station or store.stations()[0].id)
    if not (config.llm_settings()["key"] and config.llm_settings()["base"]):
        raise HTTPException(503, "LLM not configured — cannot interpret a request")
    # Recorded BEFORE the pick, so this pick already sees it as "0 songs ago"
    # and the next few picks keep seeing it, counting down.
    req = await asyncio.to_thread(requests_mod.record, st, body.text, body.songs or requests_mod.DEFAULT_SONGS)
    p = await pick_for(st, body.text)
    if p is None:
        raise HTTPException(422, "no track matched that request")
    return {
        "station": st.id,
        "track": p.track.as_line(),
        "why": p.reason,
        "request": {"text": req["text"], "songs": req["songs"]},
    }


@app.get("/requests/{station_id}")
async def list_requests(station_id: str) -> dict:
    """Requests still remembered on a station, with age and remaining songs."""
    st = web.station_or_404(station_id)
    return {"station": st.id, "requests": await asyncio.to_thread(requests_mod.active, st)}


@app.delete("/requests/{station_id}")
async def clear_requests(station_id: str) -> dict:
    st = web.station_or_404(station_id)
    await asyncio.to_thread(requests_mod.clear, st)
    return {"ok": True, "station": st.id, "requests": []}


@app.post("/pick/{station_id}")
async def force_pick(station_id: str) -> dict:
    st = web.station_or_404(station_id)
    p = await asyncio.to_thread(picker.pick_next, store.library(), st, None, queue.recent_ids(st))
    if p is None:
        raise HTTPException(422, "pick failed (LLM off or no match)")
    await asyncio.to_thread(air.air_pick, st, p, displace=True, log=log.info)
    return {"station": st.id, "track": p.track.as_line(), "why": p.reason}


@app.post("/rebuild")
async def rebuild() -> dict:
    """Re-index Navidrome and rewrite every pool.

    The connection and the station rack come from `store`, the same ones the rest
    of the process uses.
    """

    def _run() -> dict:
        conn = store.conn()
        n = navidrome.rebuild_index(conn, log=log.info)
        counts = pool.write_pools(conn, store.stations(), log=log.info)
        return {"tracks": n, "pools": counts}

    return await asyncio.to_thread(_run)


@app.post("/liq")
async def render_liq() -> dict:
    """Regenerate the Liquidsoap config from stations.json (then restart `mixer`)."""
    def _run() -> str:
        password = os.environ.get("ICECAST_SOURCE_PASSWORD", "")
        if not password:
            raise HTTPException(500, "ICECAST_SOURCE_PASSWORD not set in the picker env")
        return str(liqgen.write(store.stations(), password))

    path = await asyncio.to_thread(_run)
    return {"ok": True, "path": path, "stations": [s.id for s in store.stations()]}


@app.post("/enrich")
async def enrich_ids(limit: int = 0) -> dict:
    """Fill artist MBIDs from Navidrome's own artist records, then MusicBrainz."""
    def _run() -> dict:
        conn = store.library().conn
        outstanding = track_queries.artists_missing_mbid(conn)
        if limit:
            outstanding = outstanding[:limit]
        # Navidrome knows the artist it indexed, so ask it before asking anyone
        # else: one call per artist, and no search that can land on a namesake.
        resolved = 0
        for row in outstanding:
            mbid = enrich_mod.navidrome_artist_mbid(row["artist_id"])
            if mbid:
                enrich_mod.remember_mbid(conn, row["artist"], mbid, "navidrome")
                resolved += 1
        todo = track_queries.artists_missing_mbid_with_album(conn)
        if limit:
            todo = todo[:limit]
        ok = 0
        for row in todo:
            mbid, _ = enrich_mod.mb_release_artist(row["album"])
            if mbid:
                enrich_mod.remember_mbid(conn, row["artist"], mbid, "musicbrainz-release")
                ok += 1
        return {"from_navidrome": resolved, "musicbrainz_resolved": ok, "requested": len(todo)}

    return await asyncio.to_thread(_run)


@app.get("/similar")
async def similar(artist: str, limit: int = 8) -> dict:
    res = await asyncio.to_thread(store.library().similar, artist, limit)
    return {"artist": artist, **res}


@app.get("/genres")
async def genres() -> dict:
    """Canonical vocabulary, the raw labels behind it, and what's still unmapped."""
    return await asyncio.to_thread(navidrome.genre_report, store.library().conn)


@app.get("/aliases")
async def aliases() -> dict:
    """The operator merge rules currently in force (empty until you add some)."""
    return {
        "file": str(genre_lib.alias_path()),
        "operator": await asyncio.to_thread(lambda: genre_lib.operator_aliases()),
        "builtin": genre_lib.BUILTIN,
    }


@app.get("/coverage")
async def coverage() -> dict:
    """What the picker knows: genre coverage, plus analysis and loudness when on."""
    def _run() -> dict:
        out = jobs.coverage(store.library().conn)
        extra = analysis.coverage_for(store.library().conn)
        if extra is not None:
            out["analysis"] = extra
        levels = loudness.coverage(store.library().conn)
        if levels is not None:
            out["loudness"] = levels
        return out

    return await asyncio.to_thread(_run)


@app.post("/jobs")
async def create_job(body: dict) -> dict:
    """Queue background work: {"kind": "genre_tags"}."""
    kind = str(body.get("kind") or "")
    try:
        job_id = await asyncio.to_thread(jobs.create, store.library().conn, kind)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "id": job_id, "job": await asyncio.to_thread(jobs.get, store.library().conn, job_id)}


@app.get("/jobs")
async def list_jobs(limit: int = 10) -> dict:
    return {"jobs": await asyncio.to_thread(jobs.recent, store.library().conn, limit)}


def main() -> None:
    import uvicorn

    # Bind all interfaces inside the container: the published port is already
    # restricted to 127.0.0.1 on the host, and a container bound to its own
    # loopback is unreachable through docker's port mapping.
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8030")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
