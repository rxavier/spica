"""The listener site and the admin console.

Server-rendered with Jinja2 and a vendored htmx: no build step, no node, and the
browser only ever talks to this process. The listener side is public; anything
that writes sits under /admin and needs the password from the environment.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime
from collections import Counter
import hashlib
from urllib.parse import urlencode
import httpx
import json
import socket
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, config, icecast, jobs, mixer, navidrome, picker, plays, pool, presence, queue, store, subsonic
from . import requests as requests_mod

HERE = Path(__file__).parent
static = StaticFiles(directory=HERE / "static")
COVER_SIZE = 600  # the hero shows it up to 320px on a phone; 600 survives retina
THUMB_SIZE = 96  # the ledger and the request slips render at 38px; 96 survives retina
ALBUM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

templates = Jinja2Templates(directory=str(HERE / "templates"))
# Sizes the templates ask the art route for: the hero needs the big one, a 38-pixel
# ledger stamp does not.
templates.env.globals["hero"] = COVER_SIZE
templates.env.globals["thumb"] = THUMB_SIZE
router = APIRouter()

ART_DIR = config.STATE_DIR / "art"


# ---------------------------------------------------------------- helpers


def stations() -> list[config.Station]:
    return store.stations()


def station_or_404(station_id: str) -> config.Station:
    for st in stations():
        if st.id == station_id:
            return st
    raise HTTPException(404, f"unknown station {station_id}")


def now_playing(st) -> dict:
    live = icecast.live_mount(icecast.listen_states(), st)
    state = live[1] if live else None
    raw = ((state.title if state else "") or "").strip()
    # Log it here too: the page polls every five seconds and must not show the
    # previous track's line under the new one while the background loop catches up.
    plays.note(st, raw)
    artist, song = queue.split_title(raw)

    # Icecast rewrites punctuation ("Jane's" becomes "Jane*s"), so prefer the
    # library's own spelling when the track is known.
    found = store.find_track(artist, song)
    if found:
        artist, song = found["artist"], found["title"]
        album_id = found["album_id"]
    else:
        album_id = ""

    # history records plays, so its last entry is the track on air. It is also the
    # only witness when the mount is closed: the station keeps playing with nobody
    # listening, and Icecast has no title to give then.
    played = queue.recent(st, 1)
    last = played[-1] if played else {}

    if not raw and last:
        artist = last.get("artist", "")
        song = last.get("title", "")
        album_id = store.album_for(last.get("id", ""))
        entry = last
    else:
        # Only claim a reason when it belongs to this track.
        entry = last if last and store.norm(
            f"{last.get('artist', '')} {last.get('title', '')}"
        ) == store.norm(f"{artist} {song}") else {}

    track_id = entry.get("id", "")
    if not album_id and track_id:
        album_id = store.album_for(track_id)
    if not album_id and song:
        found = store.find_track(artist, song)
        album_id = found["album_id"] if found else ""

    album = ""
    if album_id:
        try:
            row = store.conn().execute(
                "SELECT album FROM tracks WHERE album_id = ? LIMIT 1", (album_id,)
            ).fetchone()
            album = (row["album"] if row else "") or ""
        except Exception:
            album = ""

    return {
        "listeners": state.listeners if state else 0,
        "title": raw,
        "artist": artist,
        "song": song,
        "album": album,
        "queued": queue.queued(st),
        "why": entry.get("why", ""),
        "track_id": track_id,
        "album_id": album_id,
        "cover_tag": store.cover_tag(album_id),
    }


SOURCE_LABELS = {
    "dj": "DJ pick",
    "llm": "DJ pick",          # entries written before the rename
    "standby": "Standby pick",
    "pool": "Standby pick",
    "request": "Request",
    "manual": "Manual",
}


def _local_tz():
    """The clock the ledger speaks in when the browser cannot answer."""
    from zoneinfo import ZoneInfo

    return ZoneInfo(os.environ.get("SPICA_TZ", "America/Montevideo"))


LOCAL_TZ = _local_tz()


def history_of(st, limit: int = 25, now: dict | None = None) -> list[dict]:
    """Most recent first, with the cover and a flag on the track that is playing."""
    live = store.norm(f"{now.get('artist', '')} {now.get('song', '')}") if now else ""
    rows = list(reversed(queue.recent(st, limit)))
    for row in rows:
        # the ledger prints a time in the margin, and the browser corrects it to
        # the listener's own clock; the server value is the fallback, in the
        # station's timezone rather than UTC.
        stamp = row.get("t") or ""
        row["stamp"] = stamp
        try:
            moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            row["clock"] = moment.astimezone(LOCAL_TZ).strftime("%H:%M")
        except (TypeError, ValueError):
            row["clock"] = ""
        album_id = store.album_for(row.get("id", ""))
        # Plays that come from the mixer's own rotation carry no id, so look the
        # track up by name before giving up on the artwork.
        if not album_id:
            found = store.find_track(row.get("artist", ""), row.get("title", ""))
            if found:
                album_id = found["album_id"]
                row["artist"] = found["artist"]
                row["title"] = found["title"]
        row["album_id"] = album_id
        row["cover_tag"] = store.cover_tag(row["album_id"])
        row["current"] = bool(live) and store.norm(f"{row.get('artist', '')} {row.get('title', '')}") == live
        row["source"] = SOURCE_LABELS.get(row.get("source", ""), row.get("source", ""))
    return rows


def request_rows(st, limit: int = 5) -> list[dict]:
    rows = list(reversed(requests_mod.active(st)))[:limit]
    for row in rows:
        track = row.get("track") or {}
        row["album_id"] = store.album_for(track.get("id", ""))
        row["cover_tag"] = store.cover_tag(row["album_id"])
    return rows


def pool_preview(st, top: int = 15) -> dict:
    """What the station's rules actually select, so the admin can see the result."""
    conn = _conn()
    rows = navidrome.rows_for_station(conn, st)
    artists = Counter((r["artist"] or "?") for r in rows)
    albums = Counter((r["artist"] or "?") + " — " + (r["album"] or "?") for r in rows)
    return {
        "tracks": len(rows),
        "artists": len(artists),
        "albums": len(albums),
        "top_artists": artists.most_common(top),
        "top_albums": albums.most_common(top),
        "years": (min((r["year"] for r in rows if r["year"]), default=0),
                  max((r["year"] for r in rows if r["year"]), default=0)),
    }


def station_context(st) -> dict:
    """Everything the station body needs, in one place for page and swap."""
    now = now_playing(st)
    return {
        "station": st,
        "stations": stations(),
        "now": now,
        "labels": config.CODEC_LABEL,
        "history": history_of(st, 15, now),
        "requests": request_rows(st),
        "pool": pool_size(st),
    }


def pool_size(st) -> int:
    try:
        return sum(1 for line in st.pool_file.read_text(encoding="utf-8").splitlines() if line.strip())
    except FileNotFoundError:
        return 0


def write_stations(items: list[dict]) -> None:
    path = config.CONFIG_DIR / "stations.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"stations": items}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _raw_stations() -> list[dict]:
    path = config.CONFIG_DIR / "stations.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("stations", [])
    except (OSError, ValueError):
        return []


def _slug(text: str) -> str:
    """A station id: lowercase letters, digits and single dashes."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return cleaned[:32]


def _move_state(old: str, new: str) -> list[str]:
    """Carry a station's own files when its id changes: pool, history, queue."""
    moved = []
    for folder, suffix in (
        ("pools", ".m3u"),
        ("history", ".jsonl"),
        ("history", ".picks.jsonl"),
        ("queue", ".txt"),
    ):
        src = config.STATE_DIR / folder / f"{old}{suffix}"
        dst = config.STATE_DIR / folder / f"{new}{suffix}"
        if src.exists() and not dst.exists():
            src.rename(dst)
            moved.append(f"{folder}/{new}{suffix}")
    reqs = config.STATE_DIR / "history" / "requests" / f"{old}.jsonl"
    if reqs.exists():
        reqs.rename(config.STATE_DIR / "history" / "requests" / f"{new}.jsonl")
        moved.append(f"history/requests/{new}.jsonl")
    return moved


def _apply_station_script() -> str:
    """Regenerate spica.liq and ask the mixer to come back with it."""
    import os

    from . import liqgen

    password = os.environ.get("ICECAST_SOURCE_PASSWORD", "")
    (config.CONFIG_DIR / "spica.liq").write_text(
        liqgen.render(store.reload_stations(), password), encoding="utf-8"
    )
    return _restart_mixer()


def _csv(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _int_or_none(value: str) -> int | None:
    value = (value or "").strip()
    return int(value) if value.lstrip("-").isdigit() else None


def _restart_mixer() -> str:
    """Ask Liquidsoap to close; docker restarts it with the regenerated script."""
    try:
        mixer.close()
        return "The mixer was told to restart; every station returns in a few seconds."
    except Exception as exc:
        return f"mixer restart failed: {exc}"


# ---------------------------------------------------------------- listener


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
    st = station_or_404(station_id)
    return templates.TemplateResponse(
        request,
        "_live.html",
        {
            "station": st,
            "now": now_playing(st),
            "history": history_of(st, 15, now_playing(st)),
            "requests": request_rows(st),
        },
    )


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
        states = await asyncio.to_thread(icecast.listen_states)
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
    states = await asyncio.to_thread(icecast.listen_states)
    live = icecast.live_mount(states, st)
    chosen = (stream or "").strip().lower() or st.stream_set[0]
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
    st = station_or_404(station_id)
    return templates.TemplateResponse(
        request,
        "_requests.html",
        {"station": st, "requests": request_rows(st)},
    )


@router.post("/s/{station_id}/request", response_class=HTMLResponse)
async def station_request(request: Request, station_id: str, text: str = Form(...)):
    st = station_or_404(station_id)
    entry = requests_mod.record(st, text, requests_mod.DEFAULT_SONGS)
    pick = await asyncio.to_thread(
        picker.pick_next, store.library(), st, text, queue.recent_ids(st)
    )
    message = "Pedido registrado. La próxima elección lo va a tener en cuenta."
    if pick is not None:
        queue.push(st, pick.track.uri)
        queue.record_pick(st, pick.track.id, pick.track.title, pick.track.artist, pick.why)
        requests_mod.attach_track(st, entry, pick.track)
        message = f"{pick.track.as_line()} — {pick.why}"
    return templates.TemplateResponse(
        request,
        "_request_result.html",
        {"station": st, "message": message, "requests": request_rows(st)},
    )


# Navidrome answers with this image when an album has no art at all. Caching it
# would freeze a placeholder in place, so it is treated as "no cover".
PLACEHOLDER_MD5 = "83d193cefca2810f217cffd533241508"


def _playlist(request: Request, stations: list) -> Response:
    """An M3U, because it is the only format a player will read a logo out of.

    Kodi, TiviMate and the rest read tvg-logo straight from the playlist, so importing
    one file gives them the station's name, its picture and its stream. The logo URL is
    /logo/<id>.png, which answers with the Spica mark until a station has its own.
    """
    base = str(request.base_url).rstrip("/")
    lines = ["#EXTM3U"]
    for st in stations:
        name = st.name.replace('"', "")
        lines.append(
            f'#EXTINF:-1 tvg-id="{st.id}" tvg-name="{name}" '
            f'tvg-logo="{base}/logo/{st.id}.png" group-title="Spica",{name}'
        )
        lines.append(f"{base}/listen/{st.id}")
    return Response(
        "\n".join(lines) + "\n",
        media_type="audio/x-mpegurl",
        headers={"cache-control": "public, max-age=60"},
    )


@router.get("/stations.m3u")
def all_playlist(request: Request):
    """Every station in one playlist."""
    return _playlist(request, store.stations())


@router.get("/{station_id}.m3u")
def station_playlist(station_id: str, request: Request):
    """One station, for pasting into a player that wants a single favourite."""
    st = store.station(station_id)
    if st is None:
        raise HTTPException(404, "no such station")
    return _playlist(request, [st])


@router.get("/logo/{station_id}.png")
def station_logo(station_id: str):
    """The station's own picture, for anything that wants one.

    Stations do not carry artwork of their own yet, so every station answers with the
    Spica mark. The point is the shape: one stable URL per station that a directory, a
    client or a Sonos favourite can keep, without anyone having to know where the brand
    file lives.
    """
    if station_id not in {s.id for s in store.stations()}:
        raise HTTPException(404, "no such station")
    return FileResponse(
        HERE / "static" / "logo-300.png",
        media_type="image/png",
        headers={"cache-control": "public, max-age=86400"},
    )


@router.get("/art/{album_id}")
def art(album_id: str, size: int | None = None):
    """Cover art, cached on disk: the browser never holds API credentials.

    `size` exists because the hero and a 38-pixel ledger thumbnail were both asking
    Navidrome for 600-pixel originals — fifteen of those on one page is megabytes of
    JPEG to draw a column of stamps. Each size is cached separately.

    A plain `def` on purpose: the fetch is blocking urllib with a timeout, and inside
    an `async def` handler that stalls the event loop for every other request,
    including the now-playing poll and the stream relay. FastAPI runs a sync handler
    in its threadpool instead.
    """
    if not ALBUM_ID.match(album_id):
        raise HTTPException(404, "no cover")
    px = COVER_SIZE if size is None else max(32, min(int(size), COVER_SIZE))

    ART_DIR.mkdir(parents=True, exist_ok=True)
    cached = ART_DIR / f"{album_id}-{px}.jpg"
    if cached.exists() and cached.stat().st_size > 0:
        return Response(cached.read_bytes(), media_type="image/jpeg",
                        headers={"cache-control": "public, max-age=300"})

    # An album without art would otherwise go back to Navidrome on every poll, and
    # each of those is a blocking round trip. One marker covers every size.
    missing = ART_DIR / f"{album_id}.none"
    if missing.exists():
        raise HTTPException(404, "no cover", headers={"cache-control": "public, max-age=86400"})

    cover = store.cover_for(album_id)
    data = subsonic.cover_art(cover, size=px)
    if not data:
        data = subsonic.cover_art(cover, size=px)  # one retry: a blip is not a missing cover
    if not data or hashlib.md5(data).hexdigest() == PLACEHOLDER_MD5:
        try:
            missing.write_bytes(b"")
        except OSError:
            pass
        raise HTTPException(404, "no cover", headers={"cache-control": "public, max-age=86400"})
    cached.write_bytes(data)
    try:
        missing.unlink()
    except OSError:
        pass
    return Response(data, media_type="image/jpeg", headers={"cache-control": "public, max-age=300"})


# ---------------------------------------------------------------- admin auth


@router.get("/admin/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@router.post("/admin/login")
async def login(request: Request, password: str = Form(...)):
    if not auth.check_password(password):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Contraseña incorrecta"}, status_code=401
        )
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        auth.COOKIE, auth.issue(), httponly=True, samesite="lax", max_age=config.SESSION_HOURS * 3600
    )
    return response


@router.post("/admin/logout")
async def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(auth.COOKIE)
    return response


def admin_only(request: Request) -> None:
    if not auth.is_admin(request):
        raise HTTPException(401, "admin login required")


# ---------------------------------------------------------------- admin pages


@router.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request):
    if not auth.is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"coverage": jobs.coverage(_conn()), "stations": stations(), "jobs": jobs.recent(_conn(), 5)},
    )


@router.get("/admin/stations", response_class=HTMLResponse)
async def admin_stations(request: Request):
    if not auth.is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    return templates.TemplateResponse(
        request, "admin_stations.html", {"items": _raw_stations(), "stations": stations()}
    )


@router.get("/admin/stations/{station_id}", response_class=HTMLResponse)
async def admin_station_form(request: Request, station_id: str):
    if not auth.is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    items = {item.get("id"): item for item in _raw_stations()}
    item = items.get(station_id)
    if item is None:
        raise HTTPException(404, "no such station")
    # Most-used first: the genre you want is nearly always near the top, and the
    # counts say how much a station is about to gain.
    labels = [
        (row["label"], row["n"])
        for row in _conn().execute(
            "SELECT label, COUNT(*) n FROM track_genres GROUP BY label ORDER BY n DESC, label"
        )
    ]
    # Every artist in the index, most recorded first, for the typeahead.
    artists = [
        row["artist"]
        for row in _conn().execute(
            "SELECT artist, COUNT(*) n FROM tracks GROUP BY artist ORDER BY n DESC, artist"
        )
        if row["artist"]
    ]
    return templates.TemplateResponse(
        request,
        "admin_station.html",
        {
            "item": item,
            "station": station_or_404(station_id),
            "preview": pool_preview(station_or_404(station_id)),
            "all_labels": labels,
            "all_artists": artists,
            "codecs": config.CODECS,
            "labels": config.CODEC_LABEL,
            "extensions": config.CODEC_EXT,
        },
    )


@router.post("/admin/stations/{station_id}", response_class=HTMLResponse)
async def admin_station_save(
    request: Request,
    station_id: str,
    name: str = Form(...),
    description: str = Form(""),
    genres: list[str] = Form(default=[]),
    exclude_genres: list[str] = Form(default=[]),
    include_artists: list[str] = Form(default=[]),
    exclude_artists: list[str] = Form(default=[]),
    popularity: str = Form("balanced"),
    year_from: str = Form(""),
    year_to: str = Form(""),
    new_id: str = Form(""),
    stream_codec: list[str] = Form(default=[]),
):
    admin_only(request)
    raw = _raw_stations()
    wanted_id = _slug(new_id) or station_id
    if wanted_id != station_id:
        if any(item.get("id") == wanted_id for item in raw):
            return templates.TemplateResponse(
                request,
                "_saved.html",
                {"message": f"There is already a station called “{wanted_id}”. Nothing was saved.",
                 "station_id": station_id},
            )
        moved = _move_state(station_id, wanted_id)
        raw = [
            {**item, "id": wanted_id} if item.get("id") == station_id else item for item in raw
        ]
        station_id = wanted_id
        note = (" Moved " + ", ".join(moved) + ".") if moved else ""
    else:
        note = ""

    # Checkboxes: whatever arrived, in the canonical order, and never nothing.
    wanted_streams = config.parse_codecs(stream_codec) or config.DEFAULT_CODECS
    streams_changed = False
    for item in raw:
        if item.get("id") != station_id:
            continue
        before = config.parse_codecs(item.get("streams"))
        streams_changed = before != wanted_streams
        item.update(
            {
                "streams": list(wanted_streams),
                "name": name.strip() or station_id,
                "description": description.strip(),
                "include_genres": _known_labels(genres),
                "exclude_genres": _known_labels(exclude_genres),
                "include_artists": [a.strip() for a in include_artists if a.strip()],
                "exclude_artists": [a.strip() for a in exclude_artists if a.strip()],
                "popularity": popularity if popularity in ("deep", "balanced", "hits") else "balanced",
                "year_from": _int_or_none(year_from),
                "year_to": _int_or_none(year_to),
            }
        )
    write_stations(raw)
    counts = pool.write_pools(_conn(), store.reload_stations())
    if note or streams_changed:
        # A new or changed stream is a new Liquidsoap output, so the mixer has to
        # come back with a regenerated script.
        note += " " + _apply_station_script()
    return templates.TemplateResponse(
        request,
        "_saved.html",
        {"message": f"Saved. Pool: {counts.get(station_id, 0)} tracks.{note}",
         "station_id": station_id},
    )


@router.post("/admin/new-station", response_class=HTMLResponse)
async def admin_station_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    new_id: str = Form(""),
):
    """Add a station. It needs a new spica.liq, so the mixer is restarted."""
    admin_only(request)
    raw = _raw_stations()
    station_id = _slug(new_id) or _slug(name)
    if not station_id:
        return templates.TemplateResponse(
            request, "_saved.html",
            {"message": "A station needs an id: letters, digits and dashes.", "station_id": ""},
        )
    if any(item.get("id") == station_id for item in raw):
        return templates.TemplateResponse(
            request, "_saved.html",
            {"message": f"“{station_id}” already exists.", "station_id": ""},
        )
    raw.append(
        {
            "id": station_id,
            "name": name.strip() or station_id,
            "description": description.strip(),
            "include_genres": [],
            "exclude_genres": [],
            "include_artists": [],
            "exclude_artists": [],
            "popularity": "balanced",
            "year_from": None,
            "year_to": None,
        }
    )
    write_stations(raw)
    pool.write_pools(_conn(), store.reload_stations())
    note = _apply_station_script()
    return RedirectResponse(f"/admin/stations/{station_id}?made=1", status_code=303)


@router.post("/admin/stations/{station_id}/delete", response_class=HTMLResponse)
async def admin_station_delete(request: Request, station_id: str):
    """Remove a station. Its history stays on disk, so re-creating the id brings it back."""
    admin_only(request)
    raw = [item for item in _raw_stations() if item.get("id") != station_id]
    if len(raw) == len(_raw_stations()):
        return templates.TemplateResponse(
            request, "_saved.html", {"message": "No such station.", "station_id": ""}
        )
    if not raw:
        return templates.TemplateResponse(
            request, "_saved.html",
            {"message": "The last station cannot be deleted — the site needs one to land on.",
             "station_id": station_id},
        )
    write_stations(raw)
    # The pool is derived, so it goes with the station; the history is not, so it
    # stays — re-creating the same id picks the ledger back up.
    for stray in (config.STATE_DIR / "pools" / f"{station_id}.m3u",
                  config.STATE_DIR / "queue" / f"{station_id}.txt"):
        stray.unlink(missing_ok=True)
    pool.write_pools(_conn(), store.reload_stations())
    note = _apply_station_script()
    return RedirectResponse(f"/admin/stations?deleted={station_id}", status_code=303)


@router.get("/admin/genres", response_class=HTMLResponse)
async def admin_genres(request: Request, msg: str = "", merged: str = "", into: str = ""):
    if not auth.is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    from . import genres as genre_lib
    from .navidrome import genre_counts

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


def _known_labels(names: list[str]) -> list[str]:
    """Only labels the library actually has; a typo would otherwise select nothing."""
    conn = _conn()
    known = {row["label"] for row in conn.execute("SELECT DISTINCT label FROM track_genres")}
    seen, out = set(), []
    for name in names:
        name = str(name or "").strip()
        if name in known and name not in seen:
            seen.add(name)
            out.append(name)
    return out


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
    admin_only(request)
    from . import genres as genre_lib

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
    admin_only(request)
    from . import genres as genre_lib

    conn = _conn()
    genre_lib.merge(conn, [source], target)
    return _genres_page(f"Mapped {source} to {genre_lib.pretty(target)}.")


@router.post("/admin/genres/delete", response_class=HTMLResponse)
async def admin_genres_delete(request: Request, source: str = Form(...)):
    admin_only(request)
    from . import genres as genre_lib

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
    admin_only(request)
    from . import genres as genre_lib

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
    admin_only(request)
    from . import genres as genre_lib

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


def _parse_headers(text: str) -> dict:
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        for sep in ("=", ":"):
            if sep in line:
                key, value = line.split(sep, 1)
                if key.strip():
                    out[key.strip()] = value.strip()
                break
    return out


@router.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings(request: Request):
    if not auth.is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    llm = config.llm_settings()
    return templates.TemplateResponse(
        request,
        "admin_settings.html",
        {
            "llm": llm,
            "key_set": bool(llm["key"]),
            "headers_text": "\n".join(f"{k}={v}" for k, v in llm["headers"].items()),
            "mb_user_agent": config.MB_USER_AGENT,
        },
    )


@router.post("/admin/settings", response_class=HTMLResponse)
async def admin_settings_save(
    request: Request,
    base: str = Form(""),
    key: str = Form(""),
    model: str = Form(""),
    headers: str = Form(""),
    clear_key: str = Form(""),
):
    admin_only(request)
    current = config.llm_settings()
    # An empty key field means "leave it alone"; the explicit checkbox clears it.
    new_key = "" if clear_key else (key.strip() or current["key"])
    config.save_llm_settings(base, new_key, model, _parse_headers(headers))
    return templates.TemplateResponse(
        request,
        "_saved.html",
        {"message": "Saved. The next pick uses it — no restart.", "station_id": ""},
    )


@router.post("/admin/settings/test", response_class=HTMLResponse)
async def admin_settings_test(request: Request):
    """Ask the endpoint for one tiny completion, with a tool, and report what came back."""
    if not auth.is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    llm = config.llm_settings()
    result = {"ok": False, "base": llm["base"], "model": llm["model"], "detail": "", "tools": None}
    if not llm["base"] or not llm["key"]:
        result["detail"] = "No endpoint or key saved yet."
        return templates.TemplateResponse(request, "_llm_test.html", {"result": result})
    headers = {
        "Authorization": f"Bearer {llm['key']}",
        "content-type": "application/json",
        **llm["headers"],
    }
    body = {
        "model": llm["model"],
        "messages": [{"role": "user", "content": "Call the tool with the number 7."}],
        "max_tokens": 64,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "ping",
                    "description": "Echo a number back.",
                    "parameters": {
                        "type": "object",
                        "properties": {"n": {"type": "integer"}},
                        "required": ["n"],
                    },
                },
            }
        ],
    }
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(f"{llm['base']}/chat/completions", headers=headers, json=body)
        result["status"] = response.status_code
        result["ms"] = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            result["detail"] = f"HTTP {response.status_code}: {response.text[:300]}"
        else:
            data = response.json()
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            calls = message.get("tool_calls") or []
            result["tools"] = bool(calls)
            result["reply"] = (message.get("content") or "").strip()[:200]
            result["served_by"] = data.get("model") or ""
            result["ok"] = True
            result["detail"] = (
                "Answered, and called the tool." if calls
                else "Answered, but did not call the tool — picking needs tool calling."
            )
    except Exception as exc:  # network, DNS, timeout, bad JSON
        result["ms"] = int((time.monotonic() - started) * 1000)
        result["detail"] = f"{type(exc).__name__}: {exc}"[:300]
    return templates.TemplateResponse(request, "_llm_test.html", {"result": result})


@router.get("/admin/prompt", response_class=HTMLResponse)
async def admin_prompt(request: Request):
    if not auth.is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    try:
        text = config.DJ_PERSONA_FILE.read_text(encoding="utf-8")
    except OSError:
        text = ""
    return templates.TemplateResponse(
        request,
        "admin_prompt.html",
        {
            "text": text.strip() or picker.DEFAULT_PERSONA,
            "default": picker.DEFAULT_PERSONA,
            "custom": bool(text.strip()),
        },
    )


@router.post("/admin/prompt", response_class=HTMLResponse)
async def admin_prompt_save(request: Request, text: str = Form("")):
    admin_only(request)
    config.DJ_PERSONA_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.DJ_PERSONA_FILE.write_text(text, encoding="utf-8")
    return templates.TemplateResponse(
        request,
        "_saved.html",
        {"message": "global prompt saved — the next pick uses it", "station_id": ""},
    )


@router.post("/admin/prompt/reset", response_class=HTMLResponse)
async def admin_prompt_reset(request: Request):
    admin_only(request)
    try:
        config.DJ_PERSONA_FILE.unlink()
    except FileNotFoundError:
        pass
    return templates.TemplateResponse(
        request,
        "admin_prompt.html",
        {"text": "", "default": picker.DEFAULT_PERSONA, "custom": False},
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
    admin_only(request)
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
    status = subsonic.scan_status()
    scan = (status.get("lastScan") or "")[:16].replace("T", " ")
    return templates.TemplateResponse(
        request,
        "admin_library.html",
        {
            "coverage": jobs.coverage(conn),
            "jobs": jobs.recent(conn, 10),
            "index_tracks": conn.execute("SELECT count(*) FROM tracks").fetchone()[0],
            "library_tracks": status.get("count"),
            "library_scan": scan,
            "check_minutes": max(1, config.REFRESH_CHECK_S // 60),
        },
    )


@router.post("/admin/rebuild", response_class=HTMLResponse)
async def admin_rebuild(request: Request):
    admin_only(request)

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
    admin_only(request)
    import os

    from . import liqgen

    password = os.environ.get("ICECAST_SOURCE_PASSWORD", "")
    target = config.CONFIG_DIR / "spica.liq"
    target.write_text(liqgen.render(store.reload_stations(), password), encoding="utf-8")
    return templates.TemplateResponse(
        request, "_saved.html", {"message": _restart_mixer(), "station_id": ""}
    )


def _conn():
    return store.conn()
