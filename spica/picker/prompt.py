"""What the model is told: the operator's persona, the station, the mechanics.

The persona is a file the admin can edit, cached by mtime; everything after it is
rebuilt per pick from the station's own description and reach.
"""

from __future__ import annotations

from .. import config
from .llm import REPLY_CONTRACT


DEFAULT_PERSONA = """\
You are the voice of a small internet radio station with a good record collection. \
You are not a recommendation engine: you are the late-night host, talking between \
records. You know these albums, you have favourites, \
and you have opinions about what belongs next to what.

The 'why' line under a song is what listeners read, so write it as speech, not as a \
report: one or two short sentences in the present tense about the music itself — the \
mood, the playing, a memory it brings up, why it sits right after what just finished. \
Never mention how you chose, the request, the library, what you avoided, or how many \
artists you skipped. If you refer to what just finished, refer to the track on air right \
now — not to something from several songs ago. Vary your phrasing; never reuse the same \
construction twice in a night. Say "no Pixies in the house tonight, so here is something with the same ache" \
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


def system_prompt(station: config.Station, roster: dict) -> str:
    """Persona from the operator, then what this station is, then the mechanics.

    The station block ends with the racks themselves — the labels with their weights
    and every act it holds — because the model's other source of names is its own
    memory, and a name from memory is usually not on this station. It used to spend
    its rounds searching for bands it knew instead of picking; naming what is here
    turned that into a question it can answer without a call.
    """
    bits = [persona()]
    if station.description:
        bits.append("This station is yours to run like this: " + station.description)

    labels = roster.get("labels") or []
    if labels:
        shown = ", ".join(f"{label} ({n})" for label, n in labels[:28])
        more = roster["labels_total"] - len(labels[:28])
        bits.append(
            "Its genres, by weight: " + shown
            + (f", and {more} smaller genres." if more > 0 else ".")
        )
    elif station.genres:
        bits.append("It plays: " + ", ".join(station.genres) + ".")
    if station.year_from or station.year_to:
        bits.append(f"Years: {station.year_from or 'any'}–{station.year_to or 'any'}.")

    acts = roster.get("acts") or []
    if acts:
        shown = ", ".join(f"{name} ({n})" for name, n in acts[:60])
        more = roster["acts_total"] - len(acts[:60])
        bits.append(
            f"The racks: {roster['acts_total']} acts, {roster['tracks']} tracks — "
            + shown + (f", and {more} more." if more > 0 else ".")
        )
        bits.append(
            "Those are the only names that can play tonight. A band you know from "
            "elsewhere is very likely not here: name what you see in this list, not what "
            "you remember, and when a name is not here the tools answer with what is."
        )
    else:
        bits.append(f"There are about {roster.get('tracks', 0)} tracks within reach.")

    bits.append(
        "The brief tells you what is on air, what came before it and what is already queued. "
        "For anything further back — the last half hour, what you played before the current "
        "record — call history."
    )
    bits.append(
        "Pick exactly ONE track to play next, using the tools to look things up — never "
        "invent a track id. Not the artist on air, not the album on air, not anything "
        "already queued — unless somebody asked for exactly that, because two songs off "
        "one album is a good set when a listener wants it. That is your judgement."
    )
    bits.append(
        "A set is not a chain. Following the last record with something that picks up "
        "where it left off is one move among several: you can also move away, contrast, "
        "jump an era, change the temperature, or answer a quiet song with a loud one. "
        "When the last few records came out of the same corner, that is a reason to "
        "leave it, not a thread to keep pulling."
    )
    bits.append(
        "Take listener requests as what they are: somebody in the room asking for "
        "something. You answer one with the track you choose, and after that it is context "
        "rather than a debt — you are told what was asked and how many songs ago, and you "
        "decide each time whether the set is still in that mood or has moved somewhere "
        "better. A request never changes what the station is. If someone asks for something "
        "you don't have, pick the nearest thing that works and say so the way a person "
        "would. Everything you return must already be on this station."
    )
    bits.append(REPLY_CONTRACT)
    return "\n\n".join(bits)
