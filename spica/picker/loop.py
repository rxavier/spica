"""One pick: brief, look things up, answer, and make sure the answer is legal.

The loop is deliberately small — at most MAX_TOOL_ROUNDS lookups, one forced retry
when a reply cannot be used, and a last ask with the tools switched off — because
every round is money and the station needs a track either way.
"""

from __future__ import annotations

import json
import secrets

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
RECENT_PLAYS = 5


def _count(result) -> int:
    """How many rows a tool returned — not how many keys its envelope has.

    `len()` on a dict counts keys, so a `catalogue` reply with five fields was logged
    as "5 rows" whatever it held, and the ledger under-reported every paged tool.
    """
    if isinstance(result, list):
        return len(result)
    if isinstance(result, dict):
        for key in ("tracks", "results", "items", "similar_here"):
            value = result.get(key)
            if isinstance(value, list):
                return len(value)
        if result.get("error") or result.get("note"):
            return 0
    return 0


def pick_next(
    lib: Library,
    station: config.Station,
    request_text: str | None = None,
    avoid: set[str] | None = None,
) -> Pick | None:
    llm = config.llm_settings()
    if not llm["key"] or not llm["base"]:
        return None
    # Nothing that is already airing or queued can air again — that is the one rule
    # about repeats the code enforces. Artist and album spacing is the DJ's call.
    avoid = (avoid or set()) | already_airing(lib, station)
    # The pick never plays next: whatever is already waiting goes first, and only
    # then the choice. So the brief has to describe that timeline — what is on air,
    # what is already queued, and which track will end right before the pick.
    # One seed per pick: `filter`'s order must hold still while the DJ is choosing,
    # or `offset` would page a different list every call. The next pick reshuffles.
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

    def trail(entry: dict) -> str:
        """One line of the timeline: what played, and who put it there.

        The DJ only gets a reason when the reason was its own. A track the mixer played
        from the standby pool while the DJ was offline is labelled as such — it is what
        the room heard, not a choice to build on, and presenting it as one is how the
        brief ends up disagreeing with the station.
        """
        line = f"{entry.get('title')} — {entry.get('artist')}"
        if entry.get("source") == "dj" and entry.get("why"):
            return line + f": {entry['why']}"
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
        suffix = ""
        if live_entry and live_entry.get("source") != "dj":
            suffix = " (standby — the station started this while you were offline)"
        lines.append(f"On air right now: {on_air}{suffix}.")
    else:
        lines.append("Nothing has played yet.")

    earlier = [e for e in recent if f"{e.get('title')} — {e.get('artist')}" != on_air][:4]
    if earlier:
        block = [
            "Before that, newest first — the reason when you chose it, standby when the "
            "station did:"
        ]
        for index, entry in enumerate(earlier, 1):
            block.append(f"{index}. {trail(entry)}")
        lines.append("\n".join(block))

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
    # What actually went out, not what it meant to play. A DJ notices he has been in
    # one corner by hearing it, and the model has no ears — so the run of labels is
    # stated, and stated as a reason to leave rather than a thread to continue.
    labels_seen, acts_seen = lib.recent_labels(station, RECENT_PLAYS)
    if labels_seen:
        corner = " and ".join(labels_seen[:2])
        heard = ", ".join(dict.fromkeys(a for a in acts_seen if a))
        lines.append(f"The last {len(acts_seen)} songs went out as {corner} — {heard}.")
        # Only nag when the run really is tight. Five songs from five acts is not a
        # corner, and saying so every pick would just teach it to ignore the line.
        if len(set(acts_seen)) <= 3:
            lines.append(
                f"That is {len(set(acts_seen))} acts in a row, one corner of a "
                f"{lib.count(station)}-track station: leave the room tonight — another "
                "label, another era, another temperature."
            )

    # One thought per line. Run together as a paragraph, the on-air line, the queue and
    # the list of past picks read as one wall of text.
    crate = lib.crate(
        station,
        avoid_acts=set(acts_seen) | ({on_air_artist} if on_air_artist else set()),
    )
    if crate:
        block = [
            "Three albums picked uniformly at random from the tracks this station can play. "
            "Each comes from a different act, and acts in the recent history above are "
            "excluded. Every track line is: track id, length, popularity (0 = that act's least "
            "played track, 1 = its most played), title — five tracks per album, with a count of "
            "the rest. Pick any id listed here, call flip for another three albums, or use "
            "search, filter or similar if these three do not cover what you want."
        ]
        for index, album in enumerate(crate, 1):
            head = f"{index}. {album['album']} — {album['artist']}"
            if album["year"]:
                head += f" ({album['year']})"
            if album["genre"]:
                head += f" · {album['genre']}"
            block.append(head)
            # Length and how well known, per track: the two things that decide whether
            # a five-minute single or a ninety-second sketch belongs next.
            for track in album["tracks"]:
                block.append(
                    f"   {track['id']}  {track['duration']:>5}  pop {track['popularity']:.2f}  {track['title']}"
                )
            if album["total"] > len(album["tracks"]):
                block.append(f"   (+{album['total'] - len(album['tracks'])} more)")
        lines.append("\n".join(block))

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
                (f"Listener request: {request_text}\n" if request_text else "Pick the next track.\n")
                + (f"What listeners asked for:\n{live_lines}\n" if live_lines else "")
                + recent_lines
            ),
        },
    ]

    brief = messages[1]["content"] if len(messages) > 1 else ""
    called: list[dict] = []

    def finish(content: str) -> Pick | None:
        pick = parse_final(lib, content, avoid, station)
        if pick is not None:
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
                    "content": "That reply could not be used. " + REPLY_CONTRACT
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
        {"role": "user", "content": ANSWER_NOW + " Pick a track that is not in the recent list."}
    )
    final = chat(messages, TOOLS, no_tools=True)
    return finish(final.get("content") or "")


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
