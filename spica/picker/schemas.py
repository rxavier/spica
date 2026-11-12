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
                "A draw from everything this station can play. Call it with no arguments "
                "at all to see the station itself — that is the widest look you get, and the "
                "right first move when nothing in particular is in mind. Narrow it by genre "
                "label or years when you want a corner of it; genre matching is deliberately "
                "loose, so 'Punk' also brings 'Punk Rock'. It answers with how many tracks "
                "matched and how many it returned, so you can tell a 600-track slice from a "
                "6-track one. Page a long slice with offset. Never more than three tracks by "
                "one artist per page."
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
    {
        "type": "function",
        "function": {
            "name": "browse",
            "description": (
                "The station's own shape, paged: every artist with how many tracks each, or "
                "every genre label, or every decade. station_map answers with the top few of "
                "each and no ids; this answers with all of them and still no ids, so you can "
                "see who is in the room before choosing — and see that an artist you were "
                "about to repeat is one of eighty, not one of three. Page with next_offset."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dimension": {
                        "type": "string",
                        "enum": ["artists", "labels", "decades"],
                        "description": "Which list to walk. Artists by default.",
                    },
                    "limit": {"type": "integer", "default": 40},
                    "offset": {"type": "integer", "default": 0},
                },
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
