"""Playlists, station logos and cover art."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from .. import store
from .. import subsonic
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    Response,
)

from .shared import (
    PACKAGE,
    COVER_SIZE,
    ALBUM_ID,
    ART_DIR,
    stations,
)

router = APIRouter()


PLACEHOLDER_MD5 = "83d193cefca2810f217cffd533241508"

# How long "this album has no cover" is believed. Long enough to save a round trip on
# every page load, short enough that one bad minute cannot cost an album its artwork
# for good — which is what a marker with no expiry did.
MISSING_TTL_S = 3600


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
        PACKAGE / "static" / "logo-300.png",
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
        # A marker says "Navidrome had nothing for this album" — but it can also be
        # the ghost of one bad afternoon, and a marker never aged out, so an album
        # that was briefly unreachable lost its artwork for good. Ask again after an
        # hour; a real "no cover" simply writes the marker back below.
        try:
            stale = time.time() - missing.stat().st_mtime > MISSING_TTL_S
        except OSError:
            stale = True
        if not stale:
            raise HTTPException(404, "no cover", headers={"cache-control": "public, max-age=86400"})
        try:
            missing.unlink()
        except OSError:
            pass

    cover = store.cover_for(album_id)
    try:
        data = subsonic.cover_art(cover, size=px)
        if not data:
            data = subsonic.cover_art(cover, size=px)  # one retry: a blip is not a missing cover
    except subsonic.CoverUnavailable:
        # Not the same thing as "this album has no cover": writing the marker here
        # made a Navidrome hiccup a 404 that lasted until someone deleted a file.
        raise HTTPException(503, "cover art unavailable", headers={"retry-after": "30"})
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
