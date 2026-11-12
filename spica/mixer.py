"""Liquidsoap's control server, used for two things: asking how much is already
queued, and closing the process so docker restarts it with a new script."""

from __future__ import annotations

import re
import socket

from . import config, subsonic


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

    An empty queue replies with nothing between the command and the END sentinel,
    which is a real answer — 0 means the station is running on its own pool and the
    DJ is free to choose. None is reserved for a control server that is not there,
    because the caller treats the two differently: one picks now, the other waits.
    """
    try:
        reply = command(f"q_{station.id}.queue")
    except OSError:
        return None
    if reply.startswith("ERROR"):
        return None
    return len(reply.split())


def _switches(station, codec, verb: str) -> bool:
    codecs = (codec,) if codec is not None else station.stream_set
    ok = True
    for one in codecs:
        try:
            reply = command(f"out_{station.id}_{one}.{verb}")
        except OSError:
            return False
        ok = ok and not reply.startswith("ERROR")
    return ok


def start(station, codec=None) -> bool:
    """Open one stream's Icecast source, or all of them.

    Each stream is a separate encoder, so opening only the one somebody asked for
    is the difference between one core-percent and three.
    """
    return _switches(station, codec, "start")


def stop(station, codec=None) -> bool:
    """Close a stream: its mount disappears and its encoder stops burning CPU."""
    return _switches(station, codec, "stop")


def _pending_rids(station) -> list[str] | None:
    """Request ids the station's queue holds, in order. None if it did not answer."""
    try:
        reply = command(f"q_{station.id}.queue")
    except OSError:
        return None
    if reply.startswith("ERROR"):
        return None
    return [rid for rid in reply.split() if rid.isdigit()]


def pending_uris(station) -> list[str] | None:
    """The URIs waiting in the station's queue, in the order they will play.

    None means the control server did not answer, which is not the same as an empty
    queue: one means "nothing is waiting", the other means "I cannot tell".
    """
    rids = _pending_rids(station)
    if rids is None:
        return None
    out = []
    for rid in rids:
        try:
            meta = command(f"request.metadata {rid}")
        except OSError:
            return None
        match = re.search(r'initial_uri="([^"]+)"', meta)
        if match:
            out.append(match.group(1))
    return out


def queued_ids(station) -> list[str]:
    """Library ids of the requests waiting, in the order they will play."""
    return [song for song in (subsonic.song_id(uri) for uri in pending_uris(station) or []) if song]


def set_queue(station, uris: list[str]) -> bool:
    """Replace what is waiting with `uris`, in that order.

    Liquidsoap's request.queue can only push onto the end and remove by id, so
    changing the order means emptying it and refilling it. False when the control
    server did not answer, which leaves the old queue alone rather than half-written.
    """
    rids = _pending_rids(station)
    if rids is None:
        return False
    try:
        for rid in rids:
            command(f"q_{station.id}.remove {rid}")
        for uri in uris:
            command(f"q_{station.id}.push {uri}")
    except OSError:
        return False
    return True


_NOW_OFFSET: dict[str, int] = {}


def _offset_file(station):
    return station.now_file.with_suffix(".offset")


def _load_offset(station, size: int) -> int:
    """Where we stopped last time. A fresh start begins at the end of the file.

    The offset has to outlive the process: the picker restarts, and replaying the
    file from zero would re-record everything the station played while the picker
    was down as fresh plays — and crowd the DJ's avoid list with them. Seeding a
    first run to the current size loses nothing worth having; a couple of tracks
    from before the cursor existed are not evidence about now.
    """
    try:
        saved = int(_offset_file(station).read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return size
    if saved < 0 or saved > size:  # the file went backwards under us
        return 0
    return saved


def _save_offset(station, offset: int) -> None:
    try:
        _offset_file(station).write_text(f"{offset}\n", encoding="utf-8")
    except OSError:
        pass


def now_tail(station) -> str:
    """The last track the mixer stamped, without consuming it.

    Icecast carries a title only on the MP3 mount; Ogg streams (Opus, FLAC) carry
    their metadata inside the stream, so a FLAC-only station has nothing to read from
    the mount and used to fall back to the play ledger — which the picker writes on
    its own 20-second tick. The mixer writes this file the moment a track starts, so
    reading it is the freshest answer that works for every codec.
    """
    path = station.now_file
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            start = max(0, fh.tell() - 4096)
            fh.seek(start)
            chunk = fh.read()
    except OSError:
        return ""
    lines = [line.decode("utf-8", "replace").strip() for line in chunk.split(b"\n")]
    for line in reversed(lines):
        if line:
            return line
    return ""


def now_lines(station) -> list[str]:
    """Tracks the mixer has started since the last call, oldest first.

    The ledger cannot come from Icecast once the clock outlives the mount: a closed
    mount has no title. The mixer appends "artist - title" to a file as each track
    starts, and this reads whatever is new.

    Everything here is byte-oriented. The lines carry accents ("Los Wálters"), and
    splitting decoded text then re-encoding it to recover an offset drifts as soon as
    a chunk boundary lands inside a character.
    """
    path = station.now_file
    try:
        size = path.stat().st_size
    except OSError:
        return []

    offset = _NOW_OFFSET.get(station.id)
    if offset is None:
        offset = _load_offset(station, size)
        _NOW_OFFSET[station.id] = offset
        if not _offset_file(station).exists():
            _save_offset(station, offset)  # remember the decision across restarts
    if offset > size:  # truncated or rewritten under us
        offset = 0
        _NOW_OFFSET[station.id] = offset

    out: list[str] = []
    if size > offset:
        try:
            with path.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read(size - offset)
        except OSError:
            return []
        cut = chunk.rfind(b"\n")
        if cut >= 0:  # anything after the last newline is still being written
            for raw in chunk[:cut].split(b"\n"):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    out.append(line)
            offset += cut + 1
            _NOW_OFFSET[station.id] = offset
            _save_offset(station, offset)

    # Rotate only once everything has been consumed, so a full file cannot hide
    # unread plays. A line appended between the read above and this truncation is
    # lost; that is one track, and the alternative is a file that grows forever.
    if size > 512_000 and offset >= size:
        try:
            path.write_bytes(b"")
            _NOW_OFFSET[station.id] = 0
            _save_offset(station, 0)
        except OSError:
            pass
    return out


def close() -> None:
    """Ask Liquidsoap to exit; docker's restart policy brings it back."""
    try:
        command("shutdown")
    except OSError:
        pass
