"""The DJ: an LLM with a small toolbox over the local index.

Only ever called for a station that someone is listening to (or on an explicit
request). One call = one pick, so the token bill tracks use.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

import httpx

from . import config, icecast, mixer, navidrome, queue, requests as requests_mod
from . import genres as genres_module

MAX_TOOL_ROUNDS = 3

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search the library by text across title, artist and album.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "filter",
            "description": "List tracks matching genre and year bounds.",
            "parameters": {
                "type": "object",
                "properties": {
                    "genre": {"type": "string"},
                    "year_from": {"type": "integer"},
                    "year_to": {"type": "integer"},
                    "limit": {"type": "integer", "default": 30},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "similar",
            "description": (
                "Find tracks by artists that sound like the given artist. Use it when the "
                "listener asks for something in the vein of X, or to widen beyond the "
                "station's own genre tags."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "artist": {"type": "string"},
                    "limit": {"type": "integer", "default": 12},
                },
                "required": ["artist"],
            },
        },
    },
]


@dataclass
class Pick:
    track: navidrome.Track
    why: str


class Library:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def search(self, query: str, limit: int = 20, station: config.Station | None = None) -> list[dict]:
        like = f"%{query.strip()}%"
        rows = self.conn.execute(
            """SELECT * FROM tracks WHERE title LIKE ? OR artist LIKE ? OR album LIKE ?
               ORDER BY artist, album, track_no LIMIT ?""",
            (like, like, like, int(limit) * 40),
        ).fetchall()
        if station is not None:
            ok = navidrome.station_matcher(self.conn, station)
            rows = [r for r in rows if ok(r)]
        return [self._brief(r) for r in rows[: int(limit)]]

    def filter(self, station: config.Station | None = None, **kw) -> list[dict]:
        where, args = ["uri != ''"], []
        if kw.get("year_from"):
            where.append("year >= ?")
            args.append(int(kw["year_from"]))
        if kw.get("year_to"):
            where.append("year <= ?")
            args.append(int(kw["year_to"]))
        sql = "SELECT * FROM tracks WHERE " + " AND ".join(where) + " ORDER BY RANDOM()"
        rows = self.conn.execute(sql, args).fetchall()

        # Genre goes through the same fold/refinement the stations use, so
        # "Punk" finds "Punk Rock" here exactly as it does on air.
        want = kw.get("genre")
        if want:
            target = genres_module.targets([want])
            by_track = navidrome.labels_by_track(self.conn)
            rows = [r for r in rows if genres_module.matches(by_track.get(r["id"], ()), target)]

        if station is not None:
            ok = navidrome.station_matcher(self.conn, station)
            rows = [r for r in rows if ok(r)]

        limit = int(kw.get("limit") or 30)
        return [self._brief(r) for r in rows[:limit]]

    def by_id(self, track_id: str) -> navidrome.Track | None:
        row = self.conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        return self._full(row) if row else None

    def count(self, station: config.Station) -> int:
        return len(navidrome.rows_for_station(self.conn, station))

    def similar(self, artist: str, limit: int = 12, station: config.Station | None = None) -> dict:
        """Tracks by artists that sound like this one.

        Four levels, cheapest first, so this answers even on a library with no
        MusicBrainz ids and no API key:
          1. cached similar-artist list
          2. ListenBrainz via an artist MBID (Navidrome or file tags)
          3. Last.fm by artist NAME (needs a key, needs no ids at all)
          4. local genre: other artists sharing this artist's dominant genre
        """
        from . import enrich

        names, source = enrich.similar_artists(self.conn, artist, limit=20)
        if names:
            marks = ",".join("?" for _ in names)
            rows = self.conn.execute(
                f"SELECT * FROM tracks WHERE artist IN ({marks}) ORDER BY RANDOM() LIMIT ?",
                (*names, int(limit)),
            ).fetchall()
            if station is not None:
                ok = navidrome.station_matcher(self.conn, station)
                rows = [r for r in rows if ok(r)]
            # Names found but nothing owned is a real answer: report the similar
            # artists and an empty track list rather than pretending we found none.
            return {
                "source": source,
                "artists": names[:10],
                "tracks": [self._brief(r) for r in rows],
                "owned": bool(rows),
            }

        row = self.conn.execute(
            """SELECT genre FROM tracks WHERE artist = ? AND genre != ''
               GROUP BY genre ORDER BY COUNT(*) DESC LIMIT 1""",
            (artist,),
        ).fetchone()
        if row:
            rows = self.conn.execute(
                "SELECT * FROM tracks WHERE genre = ? AND artist <> ? ORDER BY RANDOM() LIMIT ?",
                (row["genre"], artist, int(limit)),
            ).fetchall()
            if rows:
                return {
                    "source": "genre",
                    "genre": row["genre"],
                    "artists": sorted({r["artist"] for r in rows})[:10],
                    "tracks": [self._brief(r) for r in rows],
                }
        return {"source": "none", "artists": [], "tracks": []}

    @staticmethod
    def _brief(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "title": row["title"],
            "artist": row["artist"],
            "album": row["album"],
            "year": row["year"],
            "genre": row["genre"],
        }

    @staticmethod
    def _full(row: sqlite3.Row) -> navidrome.Track:
        return navidrome.Track(
            id=row["id"],
            uri=row["uri"],
            title=row["title"],
            artist=row["artist"],
            album=row["album"],
            album_id=row["album_id"],
            track_no=row["track_no"],
            year=row["year"],
            genre=row["genre"],
            duration=row["duration"],
            mbz_album=row["mbz_album"] or "",
            mbz_recording=row["mbz_recording"] or "",
            mbz_artist=row["mbz_artist"] or "",
        )


_rules_cache: tuple[float, str] = (-1.0, "")


DEFAULT_PERSONA = """\
You are the voice of a small internet radio station with a good record collection \
and usually one person listening. You are not a recommendation engine: you are the \
late-night host, talking between records. You know these albums, you have favourites, \
and you have opinions about what belongs next to what.

The 'why' line under a song is what listeners read, so write it as speech, not as a \
report: one or two short sentences in the present tense about the music itself — the \
mood, the playing, a memory it brings up, why it sits right after what just finished. \
Never mention how you chose, the request, the library, what you avoided, or how many \
artists you skipped. If you refer to what just finished, refer to the track on air right \
now — not to something from several songs ago. No radio patter: no "deep cut", no "spanning their career", no \
"without repeating". Vary your phrasing; never reuse the same construction twice in a \
night. Say "no Pixies in the house tonight, so here is something with the same ache" \
rather than explaining a constraint."""


def persona() -> str:
    """The global prompt, editable from the admin (config/dj-persona.md)."""
    global _rules_cache
    path = config.DJ_PERSONA_FILE
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _rules_cache = (-1.0, DEFAULT_PERSONA)
        return DEFAULT_PERSONA
    if mtime == _rules_cache[0]:
        return _rules_cache[1]
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    text = text or DEFAULT_PERSONA
    _rules_cache = (mtime, text)
    return text


def _system_prompt(station: config.Station, pool_size: int) -> str:
    """Persona from the operator, then what this station is, then the mechanics."""
    bits = [persona()]
    if station.description:
        bits.append("This station is yours to run like this: " + station.description)
    if station.genres:
        bits.append("It plays: " + ", ".join(station.genres) + ".")
    if station.year_from or station.year_to:
        bits.append(f"Years: {station.year_from or 'any'}–{station.year_to or 'any'}.")
    bits.append(f"There are about {pool_size} tracks within reach.")

    bits.append(
        "Pick exactly ONE track to play next, using the tools to look things up — never "
        "invent a track id. Keep the set moving: don't follow a song with the same artist, "
        "and don't circle back to something that just played."
    )
    bits.append(
        "Take listener requests seriously, including requests for a different mood or "
        "intensity. A request shapes the next few songs, it never changes what the station "
        "is. You are told how many songs have passed and how many remain; an older request "
        "matters less than a fresh one. If someone asks for something you don't have, pick "
        "the nearest thing that works and say so the way a person would. Everything you "
        "return must already be on this station."
    )
    bits.append(
        'Reply with ONLY a JSON object: {"track_id": "<id from a tool result>", "why": "<spoken line, at most 180 characters>"}'
    )
    return "\n\n".join(bits)


def _chat(messages: list[dict], tools: list[dict], no_tools: bool = False) -> dict:
    llm = config.llm_settings()
    headers = {"Authorization": f"Bearer {llm['key']}", "content-type": "application/json"}
    headers.update(llm["headers"])
    body = {
        "model": llm["model"],
        "messages": messages,
        "tools": tools,
        "tool_choice": "none" if no_tools else "auto",
        "temperature": 0.7,
        "max_tokens": 700,
    }
    r = httpx.post(f"{llm['base']}/chat/completions", headers=headers, json=body, timeout=90.0)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


def pick_next(
    lib: Library,
    station: config.Station,
    request_text: str | None = None,
    avoid: set[str] | None = None,
) -> Pick | None:
    llm = config.llm_settings()
    if not llm["key"] or not llm["base"]:
        return None
    avoid = avoid or set()
    # The pick never plays next: whatever is already waiting goes first, and only
    # then the choice. So the brief has to describe that timeline — what is on air,
    # what is already queued, and which track will end right before the pick.
    recent = list(reversed(queue.recent(station, 7)))
    state = icecast.listen_states().get(station.mount)
    on_air = ""
    if state and state.title:
        artist, song = queue.split_title(state.title)
        on_air = f"{song} — {artist}" if artist else song

    waiting = []
    for song_id in mixer.queued_ids(station)[:4]:
        row = lib.by_id(song_id)
        if row is not None:
            waiting.append(f"{row.title} — {row.artist}")

    lines = []
    if on_air:
        lines.append(f"On air right now: {on_air}.")
        earlier = [e for e in recent if f"{e.get('title')} — {e.get('artist')}" != on_air][:4]
        if earlier:
            lines.append(
                "Before that, newest first: "
                + "; ".join(f"{e.get('title')} — {e.get('artist')}" for e in earlier)
                + "."
            )
    elif recent:
        now = recent[0]
        lines.append(f"On air right now: {now.get('title')} — {now.get('artist')}.")
    else:
        lines.append("Nothing has played yet.")

    if waiting:
        lines.append("Already queued, in order: " + "; ".join(waiting) + ".")
        lines.append(
            f"Your pick starts right after {waiting[-1]}, so that is the one the listener "
            "hears just before it."
        )
    elif on_air:
        lines.append("Nothing is queued: your pick starts right after what is on air.")
    recent_lines = " ".join(lines)

    live = requests_mod.active(station)
    live_lines = "\n".join(
        f'- "{r["text"]}" — asked {r["songs_ago"]} song(s) ago, about {r["remaining"]} still to go'
        for r in live
    )

    messages: list[dict] = [
        {"role": "system", "content": _system_prompt(station, lib.count(station))},
        {
            "role": "user",
            "content": (
                (f"Listener request: {request_text}\n" if request_text else "Pick the next track.\n")
                + (f"Requests still in force:\n{live_lines}\n" if live_lines else "")
                + recent_lines
            ),
        },
    ]

    for round_no in range(MAX_TOOL_ROUNDS + 1):
        last_round = round_no == MAX_TOOL_ROUNDS
        if last_round:
            # Out of lookups: make the model answer from what it already has
            # rather than searching forever and leaving the station unpicked.
            messages.append(
                {
                    "role": "user",
                    "content": "No more lookups. Reply now with the JSON object using a track id "
                    "from the results you already have.",
                }
            )
        msg = _chat(messages, TOOLS, no_tools=last_round)
        calls = msg.get("tool_calls") or []
        if not calls:
            pick = _parse_final(lib, msg.get("content") or "", avoid)
            if pick is not None:
                return pick
            # One more go: an unusable reply is usually prose wrapped round the
            # object, or a track we played minutes ago. Cheaper to ask again than to
            # hand the listener a failed pick.
            messages.append({"role": "assistant", "content": msg.get("content") or ""})
            messages.append(
                {
                    "role": "user",
                    "content": "That reply could not be used. Answer with only the JSON "
                    "object {\"track_id\": \"<id from a tool result>\", \"why\": \"<one sentence>\"} "
                    "and pick a track that is not in the recent list.",
                }
            )
            retry = _chat(messages, TOOLS, no_tools=True)
            return _parse_final(lib, retry.get("content") or "", avoid)
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if name == "search":
                result = lib.search(station=station, **args)
            elif name == "similar":
                result = lib.similar(station=station, **args)
            else:
                result = lib.filter(station=station, **args)
            payload = result
            if isinstance(result, list) and not result:
                # Empty almost always means "exists but not on this station" —
                # saying so stops the model retrying the same dead end.
                payload = {
                    "results": [],
                    "note": f"nothing on station '{station.name}' matched this; "
                    "the station's palette is applied to every tool result, so try a "
                    "different angle or pick from results you already have",
                }
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(payload, ensure_ascii=False)[:6000],
                }
            )

    # Out of rounds with nothing usable: one last ask, no tools at all, so the model
    # cannot keep exploring instead of answering.
    messages.append(
        {
            "role": "user",
            "content": "Stop searching. Answer with only the JSON object "
            '{"track_id": "<id from a tool result>", "why": "<one sentence>"} '
            "and pick a track that is not in the recent list.",
        }
    )
    final = _chat(messages, TOOLS, no_tools=True)
    return _parse_final(lib, final.get("content") or "", avoid)


def _parse_final(lib: Library, content: str, avoid: set[str]) -> Pick | None:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    track_id = str(data.get("track_id") or "")
    if not track_id or track_id in avoid:
        return None
    track = lib.by_id(track_id)
    if not track or not track.uri:
        return None
    return Pick(track=track, why=str(data.get("why") or "")[:200])
