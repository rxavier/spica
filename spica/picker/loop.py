"""One pick: brief, look things up, answer, and make sure the answer is legal.

The loop is deliberately small — at most MAX_TOOL_ROUNDS lookups, one forced retry
when a reply cannot be used, and a last ask with the tools switched off — because
every round is money and the station needs a track either way.
"""

from __future__ import annotations

import json

from .. import config, icecast, mixer, queue
from .. import requests as requests_mod
from .library import Library
from .llm import ANSWER_NOW, REPLY_CONTRACT, chat, fit, json_object
from .prompt import system_prompt
from .schemas import TOOLS, Pick


MAX_TOOL_ROUNDS = 4

# How many of its own picks the brief repeats, with the reason it gave. Three is
# enough to keep a thread going and to avoid the same opening twice in a night; more
# would spend tokens on lines the listener has long forgotten.
RECENT_PICKS = 3


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

    # Without this the model has no memory of its own voice: every pick is a new
    # conversation, so "vary your phrasing" asked it not to repeat a construction it
    # had no record of having used, and a line could never pick up a thread from the
    # pick before it.
    said = [
        f"{e.get('title')} ({e.get('artist')}): {e.get('why')}"
        for e in reversed(queue.recent_picks(station, RECENT_PICKS))
        if e.get("why")
    ]
    if said:
        lines.append("Your last picks, newest first — " + "; ".join(said).rstrip(".") + ".")
    recent_lines = " ".join(lines)

    live = requests_mod.active(station)
    live_lines = "\n".join(
        f'- "{r["text"]}" — asked {r["songs_ago"]} song(s) ago'
        for r in live
    )

    messages: list[dict] = [
        {"role": "system", "content": system_prompt(station, lib.count(station))},
        {
            "role": "user",
            "content": (
                (f"Listener request: {request_text}\n" if request_text else "Pick the next track.\n")
                + (f"What listeners asked for:\n{live_lines}\n" if live_lines else "")
                + recent_lines
            ),
        },
    ]

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
            pick = parse_final(lib, msg.get("content") or "", avoid, station)
            if pick is not None:
                return pick
            # One more go: an unusable reply is usually prose wrapped round the
            # object, or a track we played minutes ago. Cheaper to ask again than to
            # hand the listener a failed pick.
            messages.append({"role": "assistant", "content": msg.get("content") or ""})
            messages.append(
                {
                    "role": "user",
                    "content": "That reply could not be used. " + REPLY_CONTRACT
                    + " Pick a track that is not in the recent list.",
                }
            )
            retry = chat(messages, TOOLS, no_tools=True)
            return parse_final(lib, retry.get("content") or "", avoid, station)
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
                    "content": fit(payload),
                }
            )

    # Out of rounds with nothing usable: one last ask, no tools at all, so the model
    # cannot keep exploring instead of answering.
    messages.append(
        {"role": "user", "content": ANSWER_NOW + " Pick a track that is not in the recent list."}
    )
    final = chat(messages, TOOLS, no_tools=True)
    return parse_final(lib, final.get("content") or "", avoid, station)


def parse_final(
    lib: Library, content: str, avoid: set[str], station: config.Station
) -> Pick | None:
    data = json_object(content)
    if data is None:
        return None
    track_id = str(data.get("track_id") or "")
    if not track_id or track_id in avoid:
        return None
    track = lib.by_id(track_id)
    if not track or not track.uri:
        return None
    # The station's rules are the invariant, not just a filter on the tools: a
    # guessed id, or one from a tool that forgot them, still has to be allowed to
    # play before it reaches the queue.
    if not lib.on_station(station, track_id):
        return None
    return Pick(track=track, why=str(data.get("why") or "")[:200])
