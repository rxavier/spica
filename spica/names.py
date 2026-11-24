"""Identity helpers shared across the picker.

A seed orders one pick's offers deterministically: the DJ can page `filter` without
the list changing under it, and the next pick reshuffles. The hash has to be the
same everywhere or two callers disagree about what "this pick's order" means.
"""

from __future__ import annotations

import hashlib


def seed_key(seed: int, key: str) -> bytes:
    """A stable sort key for `key` inside one pick's seed."""
    return hashlib.blake2b(f"{seed}|{key}".encode(), digest_size=8).digest()
