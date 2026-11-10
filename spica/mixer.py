"""Liquidsoap's control server, used for two things: asking how much is already
queued, and closing the process so docker restarts it with a new script."""

from __future__ import annotations

import re
import socket
import urllib.parse

from . import config


def command(line: str, timeout: float = 3.0) -> str:
    """Send one command and return its reply without the END sentinel."""
    host, _, port = config.LIQ_TELNET.rpartition(":")
    with socket.create_connection((host, int(port)), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(line.encode() + b"\n")
        buf = b""
        while b"END\r\n" not in buf:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
    return buf.decode(errors="replace").replace("END\r\n", "").replace("\r", "").strip()


def pending(station) -> int | None:
    """How many requests the station's queue still holds, or None if unreachable.

    The queue includes whatever is playing right now, so 0 means the station is
    running on its own pool and the DJ is free to choose the next track.
    """
    try:
        reply = command(f"q_{station.id}.queue")
    except OSError:
        return None
    if not reply or reply.startswith("ERROR"):
        return None
    return len(reply.split())


def start(station) -> bool:
    """Open the station's Icecast source; returns False if the mixer is away."""
    try:
        reply = command(f"out_{station.id}.start")
    except OSError:
        return False
    return not reply.startswith("ERROR")


def stop(station) -> bool:
    """Close the source: the mount disappears and the encoder stops burning CPU."""
    try:
        reply = command(f"out_{station.id}.stop")
    except OSError:
        return False
    return not reply.startswith("ERROR")


def queued_ids(station) -> list[str]:
    """Library ids of the requests waiting, in the order they will play."""
    try:
        reply = command(f"q_{station.id}.queue")
    except OSError:
        return []
    out = []
    for rid in reply.split():
        if not rid.isdigit():
            continue
        try:
            meta = command(f"request.metadata {rid}")
        except OSError:
            continue
        match = re.search(r'initial_uri="([^"]+)"', meta)
        if not match:
            continue
        query = urllib.parse.urlparse(match.group(1)).query
        song = urllib.parse.parse_qs(query).get("id", [""])[0]
        if song:
            out.append(song)
    return out


def close() -> None:
    """Ask Liquidsoap to exit; docker's restart policy brings it back."""
    try:
        command("shutdown")
    except OSError:
        pass
