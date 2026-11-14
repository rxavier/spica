"""One pick: brief, look things up, answer, and make sure the answer is legal.

The loop is deliberately small — at most MAX_TOOL_ROUNDS lookups, one forced retry
when a reply cannot be used, and a last ask with the tools switched off — because
every round is money and the station needs a track either way.
"""

from __future__ import annotations

import json
import secrets

from .. import config, icecast, mixer, plan, queue
from .. import requests as requests_mod
from .library import Library
from .llm import ANSWER_NOW, SET_CONTRACT, chat, fit, json_object
from .prompt import system_prompt
from .schemas import TOOLS, Pick


MAX_TOOL_ROUNDS = 4

# How many of its own picks the brief repeats, with the reason it gave. Three is
# enough to keep a thread going and to avoid the same opening twice in a night; more
# would spend tokens on lines the listener has long forgotten.
RECENT_PLAYS = 5


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


# How many artists the brief offers from outside the recent plays. Three was invisible
# against sixty names on the racks; ten reads as a menu, and one of them is required.
FRESH_ARTISTS = 10


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
    # Nothing that is already airing or queued can air again — that is the one rule
    # about repeats the code enforces. Artist and album spacing is the DJ's call.
    avoid = (avoid or set()) | already_airing(lib, station)
    # A request is a new decision: the rest of the set was chosen for the mood the
    # listener just asked to leave, so it is dropped rather than played behind them.
    if request_text:
        plan.clear(station, "listener request")
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
    return plan_pick(lib, station, request_text, avoid)


def plan_pick(
    lib: Library,
    station: config.Station,
    request_text: str | None,
    avoid: set[str],
) -> Pick | None:
    """Ask the model for the next set, store it, and hand back its first track."""
    # One seed per set: `filter`'s order must hold still while the DJ is choosing, or
    # `offset` would page a different list every call. The next set reshuffles.
    lib.draw_seed = secrets.randbits(32)

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

    lines = []
    if on_air:
        # The song on air is part of the same record as the ones before it: if the DJ
        # chose it, its reason belongs here too, and if the station played it while the
        # DJ was offline, that is the fact worth stating.
        if live_entry.get("source") == "dj" and live_entry.get("why"):
            lines.append(f"On air right now: {on_air}: {live_entry['why']}")
        elif live_entry and live_entry.get("source") != "dj":
            lines.append(
                f"On air right now: {on_air} (standby — the station started this while "
                "you were offline)."
            )
        else:
            lines.append(f"On air right now: {on_air}.")
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

    # Without this the model has no memory of its own voice: every pick is a new
    # conversation, so "vary your phrasing" asked it not to repeat a construction it
    # had no record of having used, and a line could never pick up a thread from the
    # pick before it.
    # What actually went out, not what it meant to play. A DJ notices he has been in
    # one corner by hearing it, and the model has no ears — so the run of labels is
    # stated, and stated as a reason to leave rather than a thread to continue.
    labels_seen, artists_seen = lib.recent_labels(station, RECENT_PLAYS)
    if labels_seen:
        corner = " and ".join(labels_seen[:2])
        heard = ", ".join(dict.fromkeys(a for a in artists_seen if a))
        lines.append(f"The last {len(artists_seen)} songs went out as {corner} — {heard}.")
        # Only nag when the run really is tight. Five songs from five artists is not a
        # corner, and saying so every pick would just teach it to ignore the line.
        if len(set(artists_seen)) <= 3:
            lines.append(
                f"That is {len(set(artists_seen))} artists in a row, one corner of a "
                f"{lib.count(station)}-track station: leave the room tonight — another "
                "label, another era, another temperature."
            )

    # The racks above are a fixed list sorted by weight, so the same handful of big
    # artists is the first thing the model reads every time and the first thing it
    # reaches for. These three are the counterweight: random, and left out of the
    # recent plays, so the set has somewhere to go that is not the top of the list.
    fresh = lib.fresh_artists(station, k=FRESH_ARTISTS)
    if fresh:
        lines.append(
            f"{len(fresh)} artists that haven't played recently: {', '.join(fresh)}. "
            "At least one track in this set should be by one of them."
        )

    # The other side of the same coin: one artist can quietly become the station. Said
    # as a count, because the model has no ears and cannot hear that it is looping.
    heavy = lib.heavy_artist(station)
    if heavy:
        lines.append(
            f"{heavy[0]} has played {heavy[1]} times in the last hour — prefer avoiding it."
        )

    # One thought per line. Run together as a paragraph, the on-air line, the queue and
    # the list of past picks read as one wall of text.
    recent_lines = "\n".join(lines)

    live = requests_mod.active(station)
    live_lines = "\n".join(
        f'- "{r["text"]}" — asked {r["songs_ago"]} song(s) ago'
        for r in live
    )

    messages: list[dict] = [
        {"role": "system", "content": system_prompt(station, lib.roster(station))},
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

    def finish(content: str) -> Pick | None:
        """Turn a reply into a stored set.

        The ten drawn artists are named in the brief and required by the prompt, and
        that is where it stays: the model decides what the set is, the code only checks
        that what it returns can actually play.
        """
        theme, ids = parse_set(lib, content, avoid, station)
        if not ids:
            return None
        # The set is stored before its first track is handed over: the next call finds
        # it there and plays the second track without asking the model anything.
        plan.save(station, theme, ids[1:])
        track = lib.by_id(ids[0])
        if track is None or not track.uri:
            return None
        pick = Pick(track=track, why=theme, theme=theme)
        pick.brief, pick.tools = brief, tuple(called)
        pick.reply = (content or "")[:500]
        return pick

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
                    + " Pick a track that is not in the recent list and is not already "
                    "playing or queued.",
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

    # Out of rounds with nothing usable: one last ask, no tools at all, so the model
    # cannot keep exploring instead of answering.
    messages.append(
        {
            "role": "user",
            "content": ANSWER_NOW + " Keep the set to tracks that are not in the recent list.",
        }
    )
    final = chat(messages, TOOLS, no_tools=True)
    return finish(final.get("content") or "")


def parse_set(
    lib: Library, content: str, avoid: set[str], station: config.Station
) -> tuple[str, list[str]]:
    """The set the model chose: one line, and the ids that can actually play.

    Only facts are checked here — the track exists, it has a file, it is on this
    station, it is not already airing or queued, and it appears once. How many tracks a
    set holds, which artists it uses and where it goes are the DJ's business and the
    operator's prompt; none of it is enforced in code, because the prompt is theirs and
    an installation may want it to say something else entirely.
    """
    data = json_object(content)
    if data is None:
        return "", []
    theme = str(data.get("theme") or "").strip()[:200]
    raw = data.get("ids") or data.get("tracks") or []
    ids: list[str] = []
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
