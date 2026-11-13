"""What the model is offered and what it must answer with.

The tool schemas, the reply contract, and the one dataclass a pick becomes. Kept
apart from the code that runs them so a description change is a one-file diff and
the names in here can be checked against the handlers.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import navidrome


@dataclass
class Pick:
    track: navidrome.Track
    why: str
    # What the DJ was working with, kept for the admin: the brief it read, the tools
    # it called, and the reply it produced. Empty when the caller does not record it.
    brief: str = ""
    tools: tuple = ()
    reply: str = ""


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
            "name": "flip",
            "description": (
                "Three more records from the racks, never the ones already in front of "
                "you. The crate in the brief is a pile you can take from; this turns the "
                "pile over. Use it when nothing among the records you were handed fits "
                "the moment, and especially before settling for a track you have already "
                "seen twice tonight."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": (
                "Free-text lookup across titles, artists and albums. Every word has to land "
                "somewhere, so 'Ride Vapour Trail' finds the track. Best when you have a name in "
                "mind. When the name plays nowhere here it does not come back empty: it answers "
                "with `similar_here` — acts this station does play that sound like the one you "
                "named, each with a track id — so a name you know from outside is a route into "
                "the library rather than a dead end. Use catalogue for order and completeness, "
                "filter for a kind of thing rather than a name."
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
                "A draw from everything this station can play. Call it with no arguments "
                "at all to see the station itself — that is the widest look you get, and the "
                "right first move when nothing in particular is in mind. Narrow it by genre or "
                "years when you want a corner of it; genre matching is deliberately loose, so "
                "'Punk' also brings 'Punk Rock'. It answers with how many tracks matched and "
                "how many it returned: a slice that returns far fewer than it matched is a "
                "corner thin on artists, not a big room. The order holds still while you are "
                "choosing, so `offset` really does page the same slice — page 2 follows page 1. "
                "Never more than three tracks by one artist per page."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "genre": {"type": "string"},
                    "year_from": {"type": "integer"},
                    "year_to": {"type": "integer"},
                    "limit": {"type": "integer", "default": 30},
                    "offset": {"type": "integer", "default": 0},
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

]
