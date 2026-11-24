"""The set the picker is playing through: several tracks, in order, under one line.

A pick is a decision about one track. A set is a decision about several, made once and
then played out — asking the model "what next" once per track gets an answer that can
only look one song ahead. The plan lives
in the station's state so the worker, the request path and the admin all read the same
one, and it is thrown away the moment something changes the mood it was chosen for.
"""

from __future__ import annotations

import json
import time

from . import config
from .atomic import atomic_write


# A set is good for the night it was chosen for, not for tomorrow. If the picker is off
# for a while the queue drains, and coming back hours later to play out a mood from
# before that is worse than picking again.
TTL_SECONDS = 90 * 60


def load(station: config.Station) -> dict | None:
    try:
        data = json.loads(station.plan_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("ids"), list):
        return None
    return data


def _write(station: config.Station, set_rationale: str, ids: list[str], made: float,
           candidate_artists: list[str] | None = None) -> None:
    body = {"set_rationale": set_rationale, "ids": list(ids), "made": made}
    if candidate_artists:
        body["candidate_artists"] = list(candidate_artists)
    atomic_write(station.plan_file, json.dumps(body, ensure_ascii=False))


def save(station: config.Station, set_rationale: str, ids: list[str],
         candidate_artists: list[str] | None = None) -> None:
    _write(station, set_rationale, ids, time.time(), candidate_artists=candidate_artists)


def clear(station: config.Station) -> None:
    """Drop the rest of the set."""
    try:
        station.plan_file.unlink()
    except OSError:
        pass


def take(station: config.Station, blocked: set[str] | None = None) -> tuple[str, dict] | None:
    """The next id in the set, with the plan it belongs to.

    Ids that can no longer play — already aired from the pool, or pushed as a request
    by somebody else — are dropped here instead of being handed to the mixer, and the
    set continues with whatever is left.
    """
    data = load(station)
    if data is None:
        return None
    if time.time() - float(data.get("made") or 0) > TTL_SECONDS:
        clear(station)
        return None
    blocked = blocked or set()
    ids = list(data["ids"])
    while ids and ids[0] in blocked:
        ids.pop(0)
    if not ids:
        clear(station)
        return None
    first, rest = ids[0], ids[1:]
    if rest:
        _write(station, str(data.get("set_rationale") or ""), rest,
               float(data.get("made") or time.time()),
               candidate_artists=data.get("candidate_artists"))
    else:
        clear(station)
    return first, data
