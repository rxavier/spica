"""One pick: brief, look things up, answer, and make sure the answer is legal.

The loop is deliberately small — at most MAX_TOOL_ROUNDS lookups, one forced retry
when a reply cannot be used, and a last ask with the tools switched off — because
every round is money and the station needs a track either way.
"""

from __future__ import annotations

import json
import secrets
import time

from .. import analysis, config, icecast, ledger, mixer, navidrome, plan, queue
from .. import requests as requests_mod
from . import shelf as shelf_module
from . import table as table_module
from .library import Library
from .llm import ANSWER_NOW, SET_CONTRACT, SET_CONTRACT_TABLE, chat, fit, json_object
from .prompt import system_prompt
from .schemas import TOOLS, Pick


MAX_TOOL_ROUNDS = 4

# How much of the on-air track must be left before a new set is planned. The brief
# anchors the set to what is on air; if the track ends while the model is thinking, the
# set starts after the standby pool's next draw instead, chosen for a record nobody
# heard. One model call is usually under a minute.
MIN_LEAD_S = 60


def _sound_suffix(lib, track_id: str) -> str:
    """The on-air track's numbers, so the table's directions are comparable.

    Without them the model sees "continue the flow" as a label it has to trust; with
    them it can see how far a row actually moved (and how far its own set did).
    """
    if not track_id:
        return ""
    try:
        feature = lib._features().get(track_id) or {}
    except Exception:
        return ""
    bits = []
    if feature.get("bpm"):
        bits.append(analysis.tempo_label(feature["bpm"], feature.get("energy")))
    if feature.get("key"):
        bits.append(feature["key"])
    if feature.get("energy") is not None:
        bits.append(f"energy {feature['energy']:.2f}")
    if feature.get("sound"):
        bits.append(feature["sound"])
    return (" · " + " · ".join(bits)) if bits else ""


def _enough_runway(lib, station) -> bool:
    """Whether the on-air record has enough left to plan a set against it."""
    plays = queue.recent(station, 1)
    if not plays:
        return True
    entry = plays[-1]
    track = lib.by_id(str(entry.get("id") or ""))
    stamp = str(entry.get("t") or "")
    duration = int(getattr(track, "duration", 0) or 0)
    if track is None or not duration or not stamp:
        return True
    started = ledger.parse_stamp(stamp)
    if started is None:
        return True
    return duration - (time.time() - started) >= MIN_LEAD_S


def _count(result) -> int:
    """How many rows a tool returned — not how many keys its envelope has.

    `len()` on a dict counts keys, so a `catalogue` reply with five fields was logged
    as "5 rows" whatever it held, and the ledger under-reported every paged tool.
    """
    if isinstance(result, list):
        return len(result)
    if isinstance(result, dict):
        for key in ("tracks", "results", "items", "similar_here", "albums"):
            value = result.get(key)
            if isinstance(value, list):
                return len(value)
        if result.get("error") or result.get("note"):
            return 0
    return 0


# What the shelf is: the artists this set may come from, drawn in shelf.py. The brief
# no longer names what played — the names that aired are already off the shelf, and
# saying so twice is what made the model treat the list as a cast list.


def pick_next(
    lib: Library,
    station: config.Station,
    request_text: str | None = None,
    avoid: set[str] | None = None,
) -> Pick | None:
    """One track for the queue: the next of the current set, or the first of a new one.

    The model is asked for a set, not a track, and the set is then played out one track
    at a time. The decision — what this stretch of the night is — gets made once, with
    several records in front of it, instead of six times with one. Answering "what
    follows this song" over and over is what made it circle one corner: the only cheap
    answer to that question is something near what is already playing.

    A listener request is a new decision. The rest of the set was chosen for the mood
    they just asked to leave, so it is dropped and the next set is picked with the
    request in the brief.
    """
    mode = config.dj_mode()
    if mode == "off":
        # No model is called. The station keeps its own rotation and the ledger
        # still records what aired.
        return None
    # Nothing that is already airing or queued can air again — that is the one rule
    # about repeats the code enforces. Artist and album spacing is the DJ's call.
    avoid = (avoid or set()) | already_airing(lib, station)
    # A request is a new decision: the rest of the set was chosen for the mood the
    # listener just asked to leave, so it is dropped rather than played behind them.
    if request_text:
        plan.clear(station)
    else:
        # The set is served before anything below looks at the model. A set is already
        # decided: losing the key, the network or the endpoint mid-set must not stop the
        # night, only stop it planning a new one.
        while True:
            step = plan.take(station, avoid)
            if step is None:
                break
            track_id, held = step
            track = lib.by_id(track_id)
            # The set outlives the decision that made it: a track can have aired from
            # the pool, or left the library, by the time its turn comes. Skip that one
            # and keep the set rather than throwing the rest away for a single bad id.
            if track is not None and track.uri and lib.on_station(station, track_id):
                theme = str(held.get("theme") or "")
                return Pick(track=track, why=theme, theme=theme)
            avoid.add(track_id)

    llm = config.llm_settings()
    if not llm["key"] or not llm["base"]:
        return None
    return plan_pick(lib, station, request_text, avoid, use_table=(mode == "track"))


def _timeline_lines(lib, station) -> list[str]:
    """The station's story so far: on air, the plays before it, and what is queued."""
    recent = list(reversed(queue.recent(station, 7)))
    live = icecast.live_mount(icecast.listen_states(), station)
    state = live[1] if live else None
    on_air = ""
    on_air_artist = ""
    if state and state.title:
        artist, song = queue.split_title(state.title)
        on_air = f"{song} — {artist}" if artist else song
        on_air_artist = artist

    waiting = []
    for song_id in mixer.queued_ids(station)[:4]:
        row = lib.by_id(song_id)
        if row is not None:
            waiting.append(f"{row.title} — {row.artist}")

    def trail(entry: dict, repeat: bool = False) -> str:
        """One line of the timeline: what played, and who put it there.

        The DJ only gets a reason when the reason was its own. A track the mixer played
        from the standby pool while the DJ was offline is labelled as such — it is what
        the room heard, not a choice to build on, and presenting it as one is how the
        brief ends up disagreeing with the station.

        `repeat` drops the reason for a track that belongs to the set the line above it
        already explained: a set of six would otherwise print one theme six times, and
        the timeline is the part of the brief the model reads as its own voice.
        """
        line = f"{entry.get('title')} — {entry.get('artist')}"
        if entry.get("source") == "dj" and entry.get("why"):
            return line + (" (same set)" if repeat else f": {entry['why']}")
        return line + " (standby — the station played it while you were offline)"

    # The station's own title only exists on the MP3 mount; Ogg and FLAC carry theirs
    # inside the stream, so `on_air` falls back to the newest play. The timeline below
    # has to run either way — when it lived inside the live-title branch, an opus-only
    # station never got the list of what played before.
    if not on_air and recent:
        on_air = f"{recent[0].get('title')} — {recent[0].get('artist')}"
    live_entry = next(
        (e for e in recent if f"{e.get('title')} — {e.get('artist')}" == on_air), {}
    )

    # What the anchor sounds like, once, so the table's numbers can be read against
    # something. Raw `on_air` stays for the comparisons below.
    on_air_id = str(live_entry.get("id") or (recent[0].get("id") if recent else "") or "")
    display = on_air + _sound_suffix(lib, on_air_id)

    lines = []
    if on_air:
        # The song on air is part of the same record as the ones before it: if the DJ
        # chose it, its reason belongs here too, and if the station played it while the
        # DJ was offline, that is the fact worth stating.
        if live_entry.get("source") == "dj" and live_entry.get("why"):
            lines.append(f"On air right now: {display}: {live_entry['why']}")
        elif live_entry and live_entry.get("source") != "dj":
            lines.append(
                f"On air right now: {display} (standby — the station started this while "
                "you were offline)."
            )
        else:
            lines.append(f"On air right now: {display}.")
    else:
        lines.append("Nothing has played yet.")

    earlier = [e for e in recent if f"{e.get('title')} — {e.get('artist')}" != on_air][:4]
    if earlier:
        block = [
            "Before that, newest first — the reason when you chose it, standby when the "
            "station did:"
        ]
        previous_why = (
            str(live_entry.get("why") or "") if live_entry.get("source") == "dj" else ""
        )
        for index, entry in enumerate(earlier, 1):
            why = str(entry.get("why") or "")
            block.append(f"{index}. {trail(entry, repeat=bool(why) and why == previous_why)}")
            previous_why = why
        lines.append("\n".join(block))

    if waiting:
        lines.append("Already queued, in order: " + "; ".join(waiting) + ".")
        lines.append(
            f"Your set starts right after {waiting[-1]}, so that is the one the listener "
            "hears just before it."
        )
    elif on_air:
        lines.append("Nothing is queued: your set starts right after what is on air.")
    return lines


def _offer_lines(lib, station, request_text: str | None,
                 use_table: bool) -> tuple[list[str], dict | None, list[str]]:
    """What the DJ may choose from: a table of records, or the artist shelf."""
    lines: list[str] = []
    # What the DJ may choose from tonight. The artists that aired, the ones already
    # queued and the ones that sound most like the three tracks either side of this set
    # are not on it — so the brief never has to say what played, and the list cannot be
    # read as a cast list of "names to get through".
    # With analysis on, the offer is records, not artists: four directions, ids in
    # front, one call and no lookups. Without it, the artist shelf is unchanged.
    table = None
    shelf_names: list[str] = []
    if not request_text and use_table:
        table = table_module.build(lib, station)
    if table:
        lines.append(table_module.render(table))
        shelf_names = list(table["artists"])  # rotation memory, as before
    elif not request_text:
        drawn = shelf_module.build(lib, station)
        shelf_names = [item["artist"] for item in drawn]
    if not table and shelf_names:
        # When analysis ran, the shelf arrives with what it sounds like: the names
        # alone ask the model to recall artists it may not know, and for a local
        # collection that recall is exactly what is missing.
        sound = lib.sound_line(shelf_names) if callable(getattr(lib, "sound_line", None)) else ""
        if sound:
            lines.append(sound)
        lines.append(
            "On the shelf for this set, and nothing else: " + ", ".join(shelf_names) + "."
        )
        lines.append(
            "Every track comes from one of those artists, none of them twice, and the "
            "set still has to move — two eras, two genres, one thing changing between "
            "each track. Which of them, in what order, is yours."
        )
    return lines, table, shelf_names


def _finish(lib: Library, station: config.Station, avoid: set[str], content: str, *,
            allowed: set[str] | None, allowed_ids: set[str] | None,
            table: dict | None, shelf_names: list[str], brief: str,
            called: list[dict]) -> Pick | None:
    """Turn a reply into a stored set.

    The shelf is the one thing about a set that is checked rather than asked for:
    every artist has to be on it, and none of them twice. Everything else — how
    many tracks, which records, the order, the line — is the DJ's.
    """
    theme, ids = parse_set(lib, content, avoid, station, allowed=allowed,
                           allowed_ids=allowed_ids)
    if not ids:
        return None
    # The set is stored before its first track is handed over: the next call finds
    # it there and plays the second track without asking the model anything.
    plan.save(station, theme, ids[1:], shelf=shelf_names)
    track = lib.by_id(ids[0])
    if track is None or not track.uri:
        return None
    pick = Pick(track=track, why=theme, theme=theme)
    pick.path = ("shelf" if table is None
                 else ("table-partial" if table.get("partial") else "table"))
    pick.coverage = table.get("coverage") if table else None
    # What the set was offered, played or not: the next few sets leave those records
    # alone, so the same familiar block cannot come back every fifteen minutes.
    pick.offered = tuple(table["ids"]) if table else ()
    pick.brief, pick.tools = brief, tuple(called)
    pick.reply = (content or "")[:500]
    pick.shelf = tuple(shelf_names)
    return pick


def _table_attempt(messages: list[dict], finish) -> Pick | None:
    """One call, no tools: the table already carries every id that may air."""
    msg = chat(messages, [], no_tools=True)
    pick = finish(msg.get("content") or "")
    if pick is None:
        messages.append({"role": "assistant", "content": msg.get("content") or ""})
        messages.append({
            "role": "user",
            "content": "That reply could not be used. " + SET_CONTRACT_TABLE
            + " Every id must be one of the table's, no artist twice, and nothing "
            "already playing or queued.",
        })
        pick = finish(chat(messages, [], no_tools=True).get("content") or "")
    return pick


def _tool_rounds(lib: Library, station: config.Station, messages: list[dict],
                 finish, called: list[dict]) -> Pick | None:
    """Run the lookup rounds until the model answers with a usable set."""
    for round_no in range(MAX_TOOL_ROUNDS + 1):
        last_round = round_no == MAX_TOOL_ROUNDS
        if last_round:
            # Out of lookups: make the model answer from what it already has
            # rather than searching forever and leaving the station unpicked.
            messages.append({"role": "user", "content": ANSWER_NOW})
        # A track id can only come from a tool result, so the first round is made to
        # look something up rather than hoping the model has an id to hand.
        msg = chat(messages, TOOLS, no_tools=last_round, require_call=round_no == 0)
        calls = msg.get("tool_calls") or []
        if not calls:
            pick = finish(msg.get("content") or "")
            if pick is not None:
                return pick
            # One more go: an unusable reply is usually prose wrapped round the
            # object, or a track we played minutes ago. Cheaper to ask again than to
            # hand the listener a failed pick.
            messages.append({"role": "assistant", "content": msg.get("content") or ""})
            messages.append(
                {
                    "role": "user",
                    "content": "That reply could not be used. " + SET_CONTRACT
                    + " Every artist must be one of the names on the shelf, none of "
                    "them twice, and no track already playing or queued.",
                }
            )
            retry = chat(messages, TOOLS, no_tools=True)
            return finish(retry.get("content") or "")
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = lib.run_tool(name, args, station)
            called.append({"name": name, "args": args, "result": _count(result)})
            payload = result
            if isinstance(result, list) and not result and name == "history":
                payload = {
                    "results": [],
                    "note": "nothing has played on this station yet; look something up instead",
                }
            elif isinstance(result, dict) and result.get("note") and not result.get("results"):
                # search says which kind of miss it was and offers what is close;
                # that is more useful than the generic note below.
                payload = result
            elif (isinstance(result, list) and not result) or (
                isinstance(result, dict) and "results" in result and not result["results"]
            ):
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
                    "content": fit(payload),
                }
            )

    return None


def plan_pick(
    lib: Library,
    station: config.Station,
    request_text: str | None,
    avoid: set[str],
    use_table: bool = True,
) -> Pick | None:
    """Ask the model for the next set, store it, and hand back its first track."""
    # A request is answered now; a set can wait for the next record to settle. Planning
    # against a track that ends mid-call is how a set ends up starting after the pool's
    # next draw, anchored to a record the listener never heard.
    if request_text is None and not _enough_runway(lib, station):
        return None
    # One seed per set: `filter`'s order must hold still while the DJ is choosing, or
    # `offset` would page a different list every call. The next set reshuffles.
    lib.draw_seed = secrets.randbits(32)

    lines = _timeline_lines(lib, station)
    offer_lines, table, shelf_names = _offer_lines(lib, station, request_text, use_table)
    lines += offer_lines

    # One thought per line. Run together as a paragraph, the on-air line, the queue and
    # the list of past picks read as one wall of text.
    recent_lines = "\n".join(lines)

    active_requests = requests_mod.active(station)
    live_lines = "\n".join(
        f'- "{r["text"]}" — asked {r["songs_ago"]} song(s) ago'
        for r in active_requests
    )

    messages: list[dict] = [
        {"role": "system", "content": system_prompt(station, lib.roster(station), table=bool(table))},
        {
            "role": "user",
            "content": (
                (f"Listener request: {request_text}\n" if request_text else "Choose the next set.\n")
                + (f"What listeners asked for:\n{live_lines}\n" if live_lines else "")
                + recent_lines
            ),
        },
    ]

    brief = messages[1]["content"] if len(messages) > 1 else ""
    called: list[dict] = []
    allowed = None if table else ({navidrome.name_key(name) for name in shelf_names} or None)
    allowed_ids = set(table["ids"]) if table else None

    def finish(content: str) -> Pick | None:
        return _finish(
            lib, station, avoid, content,
            allowed=allowed, allowed_ids=allowed_ids,
            table=table, shelf_names=shelf_names, brief=brief, called=called,
        )

    if table:
        return _table_attempt(messages, finish)
    return _tool_rounds(lib, station, messages, finish, called)

def parse_set(
    lib: Library, content: str, avoid: set[str], station: config.Station,
    allowed: set[str] | None = None, allowed_ids: set[str] | None = None,
) -> tuple[str, list[str]]:
    """The set the model chose: one line, and the ids that can actually play.

    Facts only — the track exists, it has a file, it is on this station, it is not
    already airing or queued, its artist is on tonight's shelf, and no artist appears
    twice. How many tracks the set holds, which of the shelf's artists it uses and where
    it goes are the DJ's business; the shelf is the one boundary the station draws,
    because an artist that has just played cannot be the answer to what comes next.
    """
    data = json_object(content)
    if data is None:
        return "", []
    theme = str(data.get("theme") or "").strip()[:200]
    raw = data.get("ids") or data.get("tracks") or []
    ids: list[str] = []
    used: set[str] = set()
    for item in raw:
        track_id = item if isinstance(item, str) else str((item or {}).get("id") or "")
        if not track_id or track_id in avoid or track_id in ids:
            continue
        track = lib.by_id(track_id)
        if track is None or not track.uri:
            continue
        # The station's rules are the invariant, not just a filter on the tools: an
        # id from a tool that forgot them still has to be allowed to play.
        if not lib.on_station(station, track_id):
            continue
        who = navidrome.name_key(getattr(track, "album_artist", "") or track.artist)
        # The table path restricts by id; the shelf path restricts by artist. Both
        # keep "one artist a set". A request passes neither and is exempt: two songs
        # off the album somebody asked for is a good set, not a repeat.
        if allowed_ids is not None and track_id not in allowed_ids:
            continue
        if allowed is not None and who not in allowed:
            continue
        if (allowed is not None or allowed_ids is not None) and who in used:
            continue
        used.add(who)
        ids.append(track_id)
    return theme, ids


def already_airing(lib: Library, station: config.Station) -> set[str]:
    """Ids that cannot air again right now: the track on air, and what is queued.

    Only ids. Whether the set wants another song by the same artist, or a second
    one off the same album, is a judgement the DJ is better placed to make than a
    set comparison — a listener asking for two songs off one album is a good request,
    not a mistake, and a rule enforced here would refuse it.
    """
    ids = {entry.get("id") for entry in queue.recent(station, 1) if entry.get("id")}
    ids |= set(mixer.queued_ids(station))
    return ids
