"""The HTTP call to the model, and the two things that make it survive.

One function per job: send a body, trim a tool result so it can be sent back, and
pull the first useful JSON object out of a reply that may be prose, fenced, or two
objects long.
"""

from __future__ import annotations

import json

import httpx

from .. import config

# Every tool answer is JSON and the model has to be able to read it: a blind
# character slice cuts objects in half, and the ids it was told to use become
# unreadable. Trimming happens on whole rows instead.
TOOL_RESULT_CHARS = 6000

# The reply contract, in one place: the system prompt and both retries used to
# state it in three slightly different wordings.
REPLY_CONTRACT = (
    'Reply with ONLY a JSON object: {"track_id": "<id from a tool result>", '
    '"why": "<spoken line, at most 180 characters>"}'
)
ANSWER_NOW = "No more lookups. " + REPLY_CONTRACT + " Use an id from the results you already have."


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
        "max_tokens": 700,
    }
    r = httpx.post(f"{llm['base']}/chat/completions", headers=headers, json=body, timeout=90.0)
    if require_call and r.status_code in (400, 422):
        # Not every "OpenAI-compatible" server accepts tool_choice: required.
        body["tool_choice"] = "auto"
        r = httpx.post(f"{llm['base']}/chat/completions", headers=headers, json=body, timeout=90.0)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


def fit(payload, limit: int = TOOL_RESULT_CHARS) -> str:
    """A tool result as JSON that always parses, and always fits.

    `catalogue` on a big artist, `filter` at its maximum limit and `search` at 200
    all encode to more than the budget, and the old blind slice left the model with
    half an object — including half the ids it is required to answer with. So the
    list is trimmed by whole rows, largest prefix that fits, and says so.
    """
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) <= limit:
        return text

    key = None
    rows = None
    if isinstance(payload, dict):
        for candidate in ("results", "tracks"):
            if isinstance(payload.get(candidate), list):
                key, rows = candidate, payload[candidate]
                break
    elif isinstance(payload, list):
        rows = payload
    if not rows:
        return json.dumps(
            {"results": [], "note": "too much to show; narrow the lookup"}, ensure_ascii=False
        )

    def build(count: int):
        if key is None:
            return rows[:count]
        return {**payload, key: rows[:count], "note": f"showing {count} of {len(rows)}"}

    lo, hi, best = 0, len(rows), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if len(json.dumps(build(mid), ensure_ascii=False)) <= limit:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return json.dumps(build(best), ensure_ascii=False)


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
        if isinstance(value, dict) and value.get("track_id"):
            return value
        at = text.find("{", at + 1)
    return None
