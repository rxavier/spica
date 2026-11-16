"""The HTTP call to the model, and the two things that make it survive.

One function per job: send a body, trim a tool result so it can be sent back, and
pull the first useful JSON object out of a reply that may be prose, fenced, or two
objects long.
"""

from __future__ import annotations

import copy
import json

import httpx

from .. import config

# Every tool answer is JSON and the model has to be able to read it: a blind
# character slice cuts objects in half, and the ids it was told to use become
# unreadable. Trimming happens on whole rows instead.
TOOL_RESULT_CHARS = 6000

# A set is one line plus up to ten ids, which is a few hundred tokens — but the cap
# is a ceiling, not a spend, and this model thinks before it writes. At 700 it spent
# the whole budget reasoning and answered with nothing, which read downstream as a
# failed pick. The ceiling is set where the answer always fits.
MAX_TOKENS = 4096

# Unbounded thinking is what filled that budget: with no knob set, one plan produced
# nine thousand characters of reasoning and no reply at all. `low` keeps the judgement
# and bounds the length.
REASONING_EFFORT = "low"

# The reply contract, in one place: the system prompt and both retries used to
# state it in three slightly different wordings.
SET_CONTRACT = (
    'Reply with ONLY a JSON object: {"theme": "<one line, at most 140 characters: '
    'what this set is and where it is going>", "ids": ["<id from a tool result>", ...]}'
)
ANSWER_NOW = (
    "No more lookups. " + SET_CONTRACT + " Use ids from the results you already have."
)

# The table path gives the ids in the brief and asks for no lookups at all.
SET_CONTRACT_TABLE = (
    'Reply with ONLY a JSON object: {"theme": "<one line, at most 140 characters: '
    'what this set is and where it is going>", "ids": ["<id from the table>", ...]}'
)


def chat(messages: list[dict], tools: list[dict], no_tools: bool = False,
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
        "max_tokens": MAX_TOKENS,
        "reasoning_effort": REASONING_EFFORT,
    }
    r = httpx.post(f"{llm['base']}/chat/completions", headers=headers, json=body, timeout=90.0)
    if r.status_code in (400, 422):
        # Two things not every "OpenAI-compatible" server implements: the forced first
        # lookup, and the reasoning knob. A pick is worth more than either guarantee, so
        # both are dropped and the call goes once more — without them the model may
        # think longer than the budget allows, which the retry below cannot fix.
        body.pop("reasoning_effort", None)
        if require_call:
            body["tool_choice"] = "auto"
        r = httpx.post(f"{llm['base']}/chat/completions", headers=headers, json=body, timeout=90.0)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


def fit(payload, limit: int = TOOL_RESULT_CHARS) -> str:
    """A tool result as JSON that always parses, and always fits.

    Tools page themselves to this budget, so most calls never reach the trimming below.
    When one does — a nested album list, a tool that ignores its limit — the largest list
    anywhere in the payload is cut, not the first one with a familiar name: `catalogue`
    used to answer with the same tracks twice, and the old order cut the flat list to a
    single row while the album block beside it still held all twenty, leaving the payload
    above the cap with a note about the wrong list.
    """
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) <= limit:
        return text

    data = copy.deepcopy(payload)
    trimmed = 0
    for _ in range(6):
        if len(json.dumps(data, ensure_ascii=False)) <= limit:
            break
        target = _largest_list(data)
        if target is None or not target[1]:
            break
        rows = target[1]
        original = list(rows)
        lo, hi, best = 0, len(original), 0
        while lo <= hi:
            mid = (lo + hi) // 2
            rows[:] = original[:mid]
            if len(json.dumps(data, ensure_ascii=False)) <= limit:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        rows[:] = original[:best]
        trimmed += len(original) - best
        if best == len(original):
            break

    if len(json.dumps(data, ensure_ascii=False)) > limit:
        return json.dumps(
            {"results": [], "note": "too much to show; ask for less"}, ensure_ascii=False
        )
    if trimmed and isinstance(data, dict) and not data.get("note"):
        data["note"] = f"trimmed {trimmed} row(s) to fit"
    return json.dumps(data, ensure_ascii=False)


def _largest_list(node, best=None):
    """The biggest list anywhere in the payload, with its length, for trimming."""
    if isinstance(node, list):
        if best is None or len(node) > best[0]:
            best = (len(node), node)
        for item in node:
            best = _largest_list(item, best)
    elif isinstance(node, dict):
        for value in node.values():
            best = _largest_list(value, best)
    return best


def json_object(text: str) -> dict | None:
    """The first JSON object in a reply that carries a track id.

    Handles a bare object, a fenced one, prose around it, and two objects in one
    reply — the old parser spanned from the first `{` to the last `}`, so any of
    those produced invalid JSON and cost a whole extra round trip.
    """
    decoder = json.JSONDecoder()
    at = text.find("{")
    while at != -1:
        try:
            value, _ = decoder.raw_decode(text, at)
        except ValueError:
            at = text.find("{", at + 1)
            continue
        if isinstance(value, dict) and (
            value.get("ids") or value.get("tracks") or value.get("track_id")
        ):
            return value
        at = text.find("{", at + 1)
    return None
