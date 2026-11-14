"""The DJ: an LLM with a small toolbox over the local index.

Only ever called for a station that someone is listening to (or on an explicit
request). One call = one pick, so the token bill tracks use.

  schemas  the tool descriptions and the shape of an answer
  llm      the HTTP call, result trimming, reply parsing
  prompt   what the model is told
  library  the six read-only queries the tools run
  loop     one pick: brief, tools, answer, validate
"""

from __future__ import annotations

from .library import Library
from .llm import ANSWER_NOW, SET_CONTRACT, TOOL_RESULT_CHARS
from .loop import MAX_TOOL_ROUNDS, pick_next
from .prompt import DEFAULT_PERSONA, persona
from .schemas import TOOLS, Pick

__all__ = [
    "ANSWER_NOW",
    "DEFAULT_PERSONA",
    "Library",
    "MAX_TOOL_ROUNDS",
    "Pick",
    "SET_CONTRACT",
    "TOOLS",
    "TOOL_RESULT_CHARS",
    "persona",
    "pick_next",
]
