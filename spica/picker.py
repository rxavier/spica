"""The DJ: an LLM with a small toolbox over the local index.

Only ever called for a station that someone is listening to (or on an explicit
request). One call = one pick, so the token bill tracks use.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass

import httpx

from . import config, icecast, mixer, navidrome, queue, requests as requests_mod
from . import genres as genres_module

MAX_TOOL_ROUNDS = 4

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "history",
            "description": (
                "The station's own recent past, newest first: what played, how many songs ago, "
                "and whether it is on air. Call it before picking when the current track suggests "
                "an artist you may have just played, to read the arc of the last half hour, or when "
                "a request says 'like what you just put on'. Do not use it to find a track to play "
                "— every id here has already played."
            ),
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 20}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": (
                "Free-text lookup across titles, artists and albums. Best when you have a name in "
                "mind: a song, a band, half an album title. Everything it returns is playable on "
                "this station. Use catalogue when you want order or completeness, filter when you "
                "want a kind of thing rather than a name."
            ),
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
            "description": (
                "A random sample of what this station can play, narrowed by genre label and year "
                "bounds. Genre matching is deliberately loose: 'Punk' also brings 'Punk Rock'. "
                "Results are not ranked — call it twice for a different draw. Use it to explore, "
                "not to find one specific track."
            ),
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
            "name": "catalogue",
            "description": (
                "What the station owns by one artist, or one album's tracklist in running order "
                "(artist + album, or album alone — if several artists share the name, the one with "
                "the most tracks here wins). Call it to build a set "
                "from a single record, to see how much of a band there is to work with, or when a "
                "request names a band rather than a song. Tracks this station cannot play are "
                "omitted, so an empty list means it holds none of it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "artist": {"type": "string"},
                    "album": {"type": "string"},
                    "limit": {"type": "integer", "default": 25},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "similar",
            "description": (
                "Artists and tracks that sit near a given artist: cached similarity, then "
                "ListenBrainz, then Last.fm, then a shared local genre. Use it when someone asks "
                "for 'something in the vein of X', or to step outside the station's genres. It can "
                "return artists you own nothing by — owned: false means the answer is names, not "
                "playable tracks."
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
    {
        "type": "function",
        "function": {
            "name": "station_map",
            "description": (
                "The station's palette as numbers: how many tracks it can play, which genres "
                "dominate and how much of each, the year spread, and the artists with the most to "
                "choose from. Call it when a request may not be possible here, or to aim a set — "
                "'mostly the seventies' is only true if the years say so. Returns no track ids."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


@dataclass
class Pick:
    track: navidrome.Track
    why: str


def _clamp(value, default: int, high: int) -> int:
    """A limit the model chose, kept inside sensible bounds.

    limit=0 would slice to nothing (or, with recent()'s [-n:], to everything) and a
    huge one would flood a context the tool has to fit in 6000 characters anyway.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, high))


class Library:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def search(self, query: str, limit: int = 20, station: config.Station | None = None) -> list[dict]:
        limit = _clamp(limit, 20, 200)
        like = f"%{query.strip()}%"
        rows = self.conn.execute(
            """SELECT * FROM tracks WHERE title LIKE ? OR artist LIKE ? OR album LIKE ?
               ORDER BY artist, album, track_no LIMIT ?""",
            (like, like, like, limit * 40),
        ).fetchall()
        if station is not None:
            ok = navidrome.station_matcher(self.conn, station)
            rows = [r for r in rows if ok(r)]
        return [self._brief(r) for r in rows[:limit]]

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

        limit = _clamp(kw.get("limit"), 30, 200)
        return [self._brief(r) for r in rows[:limit]]

    def by_id(self, track_id: str) -> navidrome.Track | None:
        row = self.conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        return self._full(row) if row else None

    def count(self, station: config.Station) -> int:
        return len(navidrome.rows_for_station(self.conn, station))

    def similar(self, artist: str, limit: int = 12, station: config.Station | None = None) -> dict:
        limit = _clamp(limit, 12, 50)
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

    def history(self, station: config.Station, limit: int = 20, **_) -> list[dict]:
        """The station's recent past, newest first, with how long ago each played."""
        limit = _clamp(limit, 20, 100)
        plays = list(reversed(queue.recent(station, limit)))
        out: list[dict] = []
        for ago, entry in enumerate(plays):
            track_id = entry.get("id") or ""
            track = self.by_id(track_id) if track_id else None
            out.append(
                {
                    "id": track_id,
                    "title": entry.get("title") or (track.title if track else ""),
                    "artist": entry.get("artist") or (track.artist if track else ""),
                    "album": track.album if track else "",
                    "year": track.year if track else None,
                    "songs_ago": ago,
                    "on_air": ago == 0,
                }
            )
        return out

    def catalogue(
        self,
        artist: str | None = None,
        album: str | None = None,
        limit: int = 25,
        station: config.Station | None = None,
        **_,
    ) -> dict:
        """One artist's holdings, or one album in running order.

        An album name alone is enough when only one artist owns it; if several do,
        the one with most tracks on this station wins and the reply says so.
        """
        limit = _clamp(limit, 25, 100)
        where, args = ["uri != ''"], []
        if artist:
            where.append("artist = ? COLLATE NOCASE")
            args.append(artist.strip())
        if album:
            where.append("album LIKE ?")
            args.append(f"%{album.strip()}%")
        if not artist and not album:
            return {"error": "give an artist, an album, or both"}

        rows = self.conn.execute(
            "SELECT * FROM tracks WHERE " + " AND ".join(where)
            + " ORDER BY artist, album, track_no", args
        ).fetchall()
        if station is not None:
            ok = navidrome.station_matcher(self.conn, station)
            rows = [r for r in rows if ok(r)]
        if not rows:
            return {
                "artist": artist or "",
                "album": album or "",
                "tracks": [],
                "note": "this station holds nothing under that name",
            }

        # An album name shared by several artists: keep the one with the most tracks.
        if album and not artist:
            counts: dict[str, int] = {}
            for row in rows:
                counts[row["artist"]] = counts.get(row["artist"], 0) + 1
            artist = max(counts, key=counts.get)
            rows = [r for r in rows if r["artist"] == artist]

        played = {e["id"] for e in queue.recent(station, 40) if e.get("id")} if station else set()
        queued = set(mixer.queued_ids(station)) if station else set()
        albums: dict[str, dict] = {}
        tracks: list[dict] = []
        for row in rows[:limit]:
            item = self._brief(row)
            item["track_no"] = row["track_no"]
            item["played"] = row["id"] in played
            item["queued"] = row["id"] in queued
            tracks.append(item)
            key = row["album"] or ""
            albums.setdefault(key, {"album": key, "year": row["year"], "tracks": []})
            albums[key]["tracks"].append(item)
        return {
            "artist": rows[0]["artist"],
            "album": album or "",
            "albums": list(albums.values()),
            "tracks": tracks,
            "played_recently": any(t["played"] for t in tracks),
        }

    def station_map(self, station: config.Station, **_) -> dict:
        """What this station is made of, in numbers the model can reason with."""
        rows = navidrome.rows_for_station(self.conn, station)
        labels = navidrome.labels_by_track(self.conn)
        genre_counts: Counter = Counter()
        artist_counts: Counter = Counter()
        decades: Counter = Counter()
        for row in rows:
            for label in labels.get(row["id"], ()):
                genre_counts[label] += 1
            artist_counts[row["artist"] or "?"] += 1
            year = row["year"] or 0
            if year >= 1900:
                decades[(year // 10) * 10] += 1
        # Bogus years in file tags (1006, 1800s reissues) would otherwise make the
        # station look like it spans a millennium; count them, don't rank them.
        years = [r["year"] for r in rows if r["year"] and r["year"] >= 1900]
        odd = sum(1 for r in rows if r["year"] and r["year"] < 1900)
        undated = sum(1 for r in rows if not r["year"])
        return {
            "station": station.name,
            "tracks": len(rows),
            "genres": [
                {"label": label, "tracks": n} for label, n in genre_counts.most_common(8)
            ],
            "years": {
                "from": min(years) if years else None,
                "to": max(years) if years else None,
                "decades": {
                    f"{d}s": n for d, n in sorted(decades.items()) if d >= 1900
                },
                "undated": undated,
                "ignored_bad_years": odd,
            },
            "artists": [{"name": name, "tracks": n} for name, n in artist_counts.most_common(10)],
        }

    def run_tool(self, name: str, args: dict, station: config.Station):
        """Execute one tool call, turning bad arguments into something the model can read."""
        try:
            if name == "history":
                return self.history(station=station, **args)
            if name == "catalogue":
                return self.catalogue(station=station, **args)
            if name == "station_map":
                return self.station_map(station=station, **args)
            if name == "similar":
                return self.similar(station=station, **args)
            if name == "search":
                return self.search(station=station, **args)
            if name == "filter":
                return self.filter(station=station, **args)
            return {"error": f"no such tool: {name}"}
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc}"}
        except Exception as exc:  # a tool must never take the pick down with it
            return {"error": f"{name} failed: {exc}"}

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
        "The brief tells you what is on air, what came before it and what is already queued. "
        "For anything further back — the last half hour, what you played before the current "
        "record — call history."
    )
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


def _chat(messages: list[dict], tools: list[dict], no_tools: bool = False,
          require_call: bool = False) -> dict:
    llm = config.llm_settings()
    headers = {"Authorization": f"Bearer {llm['key']}", "content-type": "application/json"}
    headers.update(llm["headers"])
    body = {
        "model": llm["model"],
        "messages": messages,
        "tools": tools,
        "tool_choice": "none" if no_tools else ("required" if require_call else "auto"),
        "temperature": 0.7,
        "max_tokens": 700,
    }
    r = httpx.post(f"{llm['base']}/chat/completions", headers=headers, json=body, timeout=90.0)
    if require_call and r.status_code in (400, 422):
        # Not every "OpenAI-compatible" server accepts tool_choice: required.
        body["tool_choice"] = "auto"
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
    live = icecast.live_mount(icecast.listen_states(), station)
    state = live[1] if live else None
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
        # A track id can only come from a tool result, so the first round is made to
        # look something up rather than hoping the model has an id to hand.
        msg = _chat(messages, TOOLS, no_tools=last_round, require_call=round_no == 0)
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
            result = lib.run_tool(name, args, station)
            payload = result
            if isinstance(result, list) and not result and name == "history":
                payload = {
                    "results": [],
                    "note": "nothing has played on this station yet; look something up instead",
                }
            elif isinstance(result, list) and not result:
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
