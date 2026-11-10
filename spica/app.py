"""HTTP surface + the loop that feeds listened-to stations.

One process: uvicorn serves /status and /request while a background task asks
the DJ for a next track only on stations that have a listener.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import bootstrap
from . import config, icecast, mixer, navidrome, picker, plays, pool, presence, queue, store, web
from . import requests as requests_mod

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("radio")

STATIONS = config.load_stations()
DB_PATH = config.STATE_DIR / "index.db"
CONN: sqlite3.Connection | None = None
LIB: picker.Library | None = None
LAST: dict[str, dict] = {}
QUIET_SINCE: dict[str, float] = {}
INDEX_STAMP: list[float] = [0.0]


def lib() -> picker.Library:
    assert LIB is not None
    return LIB


async def loop() -> None:
    while True:
        try:
            stamp = store.db_path().stat().st_mtime if store.db_path().exists() else 0.0
            if stamp and stamp != INDEX_STAMP[0]:
                if INDEX_STAMP[0]:
                    store.forget_tracks()  # the worker rebuilt the index under us
                    log.info("index changed on disk — reloaded the track lookup")
                INDEX_STAMP[0] = stamp
            states = await asyncio.to_thread(icecast.listen_states)
            for st in store.stations():
                plays.note(st, states.get(st.mount).title if states.get(st.mount) else "")
                _close_if_silent(st, states)
                if queue.queued(st):
                    continue
                n = icecast.listeners(states, st.mount)
                if not presence.listening(st.id, n):
                    LAST[st.id] = {**LAST.get(st.id, {}), "listeners": n,
                                   "why": presence.reason(st.id, n)}
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
                elif ahead > config.KEEP_AHEAD:
                    continue
                p = await asyncio.to_thread(
                    picker.pick_next, lib(), st, None, queue.recent_ids(st)
                )
                if p is None:
                    LAST[st.id] = {**LAST.get(st.id, {}), "listeners": n, "why": "pick failed — playlist continues"}
                    continue
                queue.push(st, p.track.uri)
                queue.record_pick(st, p.track.id, p.track.title, p.track.artist, p.why,
                                  presence=presence.reason(st.id, n))
                LAST[st.id] = {
                    "listeners": n,
                    "pushed": time.time(),
                    "queued": p.track.as_line(),
                    "why": p.why,
                    "at": time.strftime("%H:%M:%S"),
                }
                log.info("[%s] queued %s — %s (%s)", st.id, p.track.title, p.track.artist, p.why[:60])
        except Exception as exc:  # keep the loop alive whatever happens
            log.warning("loop error: %s", exc)
        await asyncio.sleep(config.POLL_S)


def _close_if_silent(st: config.Station, states: dict) -> None:
    """Stop encoding a station nobody is listening to."""
    state = states.get(st.mount)
    if state is None:
        QUIET_SINCE.pop(st.id, None)
        return
    if state.listeners > 0:
        QUIET_SINCE.pop(st.id, None)
        return
    since = QUIET_SINCE.setdefault(st.id, time.time())
    if time.time() - since >= config.IDLE_STOP_S:
        if mixer.stop(st):
            log.info("[%s] closed after %ds without listeners", st.id, config.IDLE_STOP_S)
        QUIET_SINCE.pop(st.id, None)


def reload_stations() -> list[config.Station]:
    """Re-read stations.json after the admin UI writes it."""
    global STATIONS
    STATIONS = config.load_stations()
    return STATIONS


class RequestBody(BaseModel):
    text: str
    station: str | None = None
    # How many tracks the request stays in force for. A vibe change is a
    # direction for a set, not for one song.
    songs: int | None = None


def _station(station_id: str) -> config.Station:
    for st in store.stations():
        if st.id == station_id:
            return st
    raise HTTPException(404, f"unknown station {station_id}")


def _pool_size(st: config.Station) -> int:
    try:
        return sum(1 for line in st.pool_file.read_text(encoding="utf-8").splitlines() if line.strip())
    except FileNotFoundError:
        return 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    global LIB
    bootstrap.ensure()
    LIB = picker.Library(store.conn())
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
async def status() -> dict:
    states = await asyncio.to_thread(icecast.listen_states)
    out = {}
    for st in store.stations():
        mount = states.get(st.mount)
        out[st.id] = {
            "name": st.name,
            "mount": st.mount,
            "listeners": mount.listeners if mount else 0,
            "on_air": mount.title if mount else "",
            "queued": queue.queued(st),
            "pool": _pool_size(st),
            "last": LAST.get(st.id, {}),
            "presence": {
                **presence.relay_summary(st.id),
                "heartbeat_age": round(presence.heartbeat_age(st.id), 1),
                "listening": presence.listening(st.id, mount.listeners if mount else 0),
            },
        }
    return {"stations": out, "icecast": config.ICECAST_URL}


async def pick_for(st: config.Station, text: str | None = None) -> picker.Pick | None:
    """Ask the DJ for one track on this station and hand it to the mixer."""
    if not (config.llm_settings()["key"] and config.llm_settings()["base"]):
        return None
    p = await asyncio.to_thread(picker.pick_next, lib(), st, text, queue.recent_ids(st))
    if p is None:
        return None
    queue.push(st, p.track.uri)
    queue.record_pick(st, p.track.id, p.track.title, p.track.artist, p.why,
                                  presence=presence.reason(st.id, n))
    LAST[st.id] = {"queued": p.track.as_line(), "why": p.why, "at": time.strftime("%H:%M:%S")}
    return p


@app.post("/request")
async def request_track(body: RequestBody) -> dict:
    """Free text -> one track, pushed to the named station (default: first)."""
    st = _station(body.station or store.stations()[0].id)
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
        "why": p.why,
        "request": {"text": req["text"], "songs": req["songs"]},
    }


@app.get("/requests/{station_id}")
async def list_requests(station_id: str) -> dict:
    """Requests still in force on a station, with age and remaining songs."""
    st = _station(station_id)
    return {"station": st.id, "requests": await asyncio.to_thread(requests_mod.active, st)}


@app.delete("/requests/{station_id}")
async def clear_requests(station_id: str) -> dict:
    st = _station(station_id)
    await asyncio.to_thread(requests_mod.clear, st)
    return {"ok": True, "station": st.id, "requests": []}


@app.post("/pick/{station_id}")
async def force_pick(station_id: str) -> dict:
    st = _station(station_id)
    p = await asyncio.to_thread(picker.pick_next, lib(), st, None, queue.recent_ids(st))
    if p is None:
        raise HTTPException(422, "pick failed (LLM off or no match)")
    queue.push(st, p.track.uri)
    queue.record_pick(st, p.track.id, p.track.title, p.track.artist, p.why,
                                  presence=presence.reason(st.id, n))
    return {"station": st.id, "track": p.track.as_line(), "why": p.why}


@app.post("/rebuild")
async def rebuild() -> dict:
    def _run() -> dict:
        n = navidrome.rebuild_index(CONN, log=log.info)
        counts = pool.write_pools(CONN, STATIONS, log=log.info)
        return {"tracks": n, "pools": counts}

    return await asyncio.to_thread(_run)


@app.post("/liq")
async def render_liq() -> dict:
    """Regenerate the Liquidsoap config from stations.json (then restart `mixer`)."""
    from . import liqgen

    def _run() -> str:
        password = os.environ.get("ICECAST_SOURCE_PASSWORD", "")
        if not password:
            raise HTTPException(500, "ICECAST_SOURCE_PASSWORD not set in the picker env")
        target = config.CONFIG_DIR / "spica.liq"
        target.write_text(liqgen.render(STATIONS, password), encoding="utf-8")
        return str(target)

    path = await asyncio.to_thread(_run)
    return {"ok": True, "path": path, "stations": [s.id for s in STATIONS]}


@app.post("/enrich")
async def enrich_ids(limit: int = 0) -> dict:
    """Fill artist MBIDs: Navidrome's columns first, MusicBrainz for the rest."""
    from . import enrich as en

    def _run() -> dict:
        conn = lib().conn
        seeded = 0
        for row in conn.execute(
            """SELECT artist, MAX(mbz_artist) AS mbid FROM tracks
               WHERE artist != '' AND mbz_artist != '' GROUP BY artist"""
        ):
            if not en.cached_mbid(conn, row["artist"]):
                en.remember_mbid(conn, row["artist"], row["mbid"], "navidrome")
                seeded += 1
        todo = conn.execute(
            """SELECT t.artist AS artist, MAX(t.mbz_album) AS album
               FROM tracks t LEFT JOIN artist_mbids a ON a.name = t.artist
               WHERE a.name IS NULL AND t.artist != '' AND t.mbz_album != ''
               GROUP BY t.artist"""
        ).fetchall()
        if limit:
            todo = todo[:limit]
        ok = 0
        for row in todo:
            mbid, _ = en.mb_release_artist(row["album"])
            if mbid:
                en.remember_mbid(conn, row["artist"], mbid, "musicbrainz-release")
                ok += 1
        return {"seeded_from_navidrome": seeded, "musicbrainz_resolved": ok, "requested": len(todo)}

    return await asyncio.to_thread(_run)


@app.get("/similar")
async def similar(artist: str, limit: int = 8) -> dict:
    res = await asyncio.to_thread(lib().similar, artist, limit)
    return {"artist": artist, **res}


@app.get("/genres")
async def genres() -> dict:
    """Canonical vocabulary, the raw labels behind it, and what's still unmapped."""
    return await asyncio.to_thread(navidrome.genre_report, lib().conn)


@app.get("/aliases")
async def aliases() -> dict:
    """The operator merge rules currently in force (empty until you add some)."""
    from . import genres as g

    return {
        "file": str(g._FILE),
        "operator": await asyncio.to_thread(lambda: g.operator_aliases()),
        "builtin": g.BUILTIN,
    }


@app.get("/coverage")
async def coverage() -> dict:
    """What the DJ knows: genre coverage, with what is still missing."""
    from . import jobs

    return await asyncio.to_thread(jobs.coverage, lib().conn)


@app.post("/jobs")
async def create_job(body: dict) -> dict:
    """Queue background work: {"kind": "genre_tags"}."""
    from . import jobs

    kind = str(body.get("kind") or "")
    try:
        job_id = await asyncio.to_thread(jobs.create, lib().conn, kind)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "id": job_id, "job": await asyncio.to_thread(jobs.get, lib().conn, job_id)}


@app.get("/jobs")
async def list_jobs(limit: int = 10) -> dict:
    from . import jobs

    return {"jobs": await asyncio.to_thread(jobs.recent, lib().conn, limit)}


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
