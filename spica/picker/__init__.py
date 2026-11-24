"""The picker: an LLM with a small toolbox over the local index.

Only ever called for a station that someone is listening to (or on an explicit
request). One call = one pick, so the token bill tracks use.

  schemas  the tool descriptions and the shape of an answer
  llm      the HTTP call, result trimming, reply parsing
  prompt   what the model is told
  library  the six read-only queries the tools run
  loop     one pick: prompt context, tools, answer, validate
"""

from __future__ import annotations

from .library import Library
from .loop import pick_next
from .prompt import DEFAULT_PERSONA
from .schemas import Pick

__all__ = ["DEFAULT_PERSONA", "Library", "Pick", "pick_next"]
