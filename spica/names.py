"""Identity helpers shared across the station.

Two normalisations, for two different questions — pick the one that answers yours:

  - `name_key` / `artist_key`: is this the same artist? Accents, curly quotes,
    typographic dashes and spacing are how a name was typed, not what it says.
  - `norm` / `title_key`: is this the same title Icecast announced? Icecast turns
    punctuation into "*", so anything outside letters and digits is noise.
  - `norm_loose` / `title_key_loose`: the same title match after folding extended
    Latin to ASCII, for titles Icecast could not carry at all.

And `seed_key`, which orders one pick's offers deterministically.
"""

from __future__ import annotations

import hashlib
import unicodedata

# Letters that are alphanumeric but not ASCII: Icecast turns punctuation into
# "*", and a title with a Polish ł then has nothing left to match on.
_FOLD = str.maketrans({
    "ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ð": "d",
    "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss", "þ": "th", "ı": "i",
})


def name_key(name: str) -> str:
    """A name reduced to what it says, not how it was typed.

    "L’Impératrice", "L'Impératrice" and "l’imperatrice" are one artist, so an ASCII
    apostrophe still reaches a library that stores the typographic one.
    """
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    for quote in "’‘`´":
        text = text.replace(quote, "'")
    # "blink‐182" uses a typographic hyphen; nobody types one of those.
    for dash in "‐‑‒–—−":
        text = text.replace(dash, "-")
    return " ".join(text.casefold().split())


def norm(text: str) -> str:
    """Compare titles the way Icecast mangles them: it turns "Jane’s" into
    "Jane*s", so anything outside letters and digits is noise."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def norm_loose(text: str) -> str:
    """norm() with the extended-Latin letters folded to ASCII."""
    return norm((text or "").translate(_FOLD))


def wildcard_match(mangled: str, candidate: str) -> bool:
    """Icecast replaces a character it cannot carry with "*", so a title comes
    back with a hole in it: "Fikołek" arrives as "Fiko*ek". Same length, every
    position equal except where the mangled copy holds a non-letter."""
    a, b = (mangled or "").casefold(), (candidate or "").casefold()
    if not a or len(a) != len(b):
        return False
    return all(x == y or not x.isalnum() for x, y in zip(a, b))


def seed_key(seed: int, key: str) -> bytes:
    """A stable sort key for `key` inside one pick's seed."""
    return hashlib.blake2b(f"{seed}|{key}".encode(), digest_size=8).digest()
