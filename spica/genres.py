"""Genre labels: fold, obvious merges, operator overrides, and matching.

Labels come from whatever the library carries — Navidrome's tag map, the files,
or a fallback provider — and they are messy in predictable ways. Three layers
normalise them, cheapest first:

  1. ``fold``     case, padding and separators away, so ``Post-Rock`` and
                  ``post  rock`` are one key.
  2. ``BUILTIN``  a deliberately SHORT list of obvious equivalences: spelling,
                  abbreviation and translation noise. Never genre judgement —
                  ``math rock`` is not folded into Post-Rock, and
                  ``progressive metal`` is left alone, because a human has to
                  decide those.
  3. operator     ``state/genre-aliases.json``, the same shape SUB/WAVE writes
                  (``{"aliases": [{"from": <folded key>, "to": <label>}]}``).
                  It WINS over BUILTIN, so an operator can always overrule a
                  builtin guess. Empty or missing is normal.

Matching is one-directional: a track label may be more specific than the station
asked for, never broader.

    station "Punk"      ← track "Punk Rock"        ✓ refines
    station "Punk"      ← track "Pop Punk"         ✓ refines
    station "Pop Punk"  ← track "Pop"              ✗ broader than asked
    station "Rap"       ← track "Trap"             ✗ not a word boundary
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import config

# ---------------------------------------------------------------- layer 1: fold

_SPACE = re.compile(r"[^\w&]+", re.UNICODE)


def fold(label: object) -> str:
    """Comparison key: case, punctuation and padding fold; `&` reads as `and`.

    Letters in any script survive. The old pattern was `[^0-9a-z&]`, which turned
    every non-Latin letter into a space: "Рок", "ロック" and "روك" all folded to
    "" and unify_spellings then treated them as one label, keeping one spelling and
    deleting the others.
    """
    s = str(label or "").strip().lower().replace("&", " and ").replace("+", " and ")
    return _SPACE.sub(" ", s).replace("_", " ").strip()


def _words(label: object) -> list[str]:
    return [w for w in fold(label).split(" ") if w]


# ------------------------------------------------------------- layer 2: builtin
#
# Keyed by the folded label. Only variants a listener would never distinguish:
# nothing here changes what a genre MEANS. Keep it small and obviously right.

BUILTIN: dict[str, str] = {
    # progressive
    "prog": "Progressive Rock",
    "prog rock": "Progressive Rock",
    "progressive": "Progressive Rock",
    "rock progresivo": "Progressive Rock",
    "rock progressivo": "Progressive Rock",
    "progressieve rock": "Progressive Rock",
    "neo prog": "Progressive Rock",
    "neoprog": "Progressive Rock",
    # alternative
    "alt": "Alternative Rock",
    "alt rock": "Alternative Rock",
    "alternative": "Alternative Rock",
    "rock alternative rock": "Alternative Rock",
    # psychedelic
    "psych": "Psychedelic Rock",
    "psychedelic": "Psychedelic Rock",
    # separators / spelling only
    "post rock": "Post Rock",
    "postrock": "Post Rock",
    "art rock": "Art Rock",
    "artrock": "Art Rock",
    "synth pop": "Synth Pop",
    "synthpop": "Synth Pop",
    "hip hop": "Hip Hop",
    "hiphop": "Hip Hop",
    "drum n bass": "Drum and Bass",
    "drum and bass": "Drum and Bass",
    "dnb": "Drum and Bass",
    "r and b": "R&B",
    "rnb": "R&B",
    "rhythm and blues": "R&B",
}


# Last.fm artist tags are mostly real genres but carry a few that are not genres
# at all — a country, a decade, a listening habit. Only used for labels we
# IMPORT from a provider; a tag the library itself carries is left alone.
NON_GENRE = {
    "seen live", "favorites", "favourites", "favorite", "favourite", "love",
    "awesome", "good", "best", "all", "misc", "unknown", "albums i own", "want to see live",
    "british", "american", "usa", "uk", "england", "britain", "english", "scottish",
    "welsh", "irish", "swedish", "norwegian", "finnish", "danish", "dutch", "german",
    "french", "italian", "spanish", "portuguese", "greek", "polish", "russian",
    "belgian", "swiss", "austrian", "icelandic", "canadian", "australian",
    "new zealand", "japanese", "korean", "chinese", "argentine", "argentinian",
    "chile", "uruguay", "uruguayan", "spain", "mexico", "mexican", "brazil",
    "brazilian", "argentina", "peruvian", "colombian", "cuban",
    # country and region names, in English and in the language people tag in
    "france", "francia", "italy", "italia", "germany", "deutschland", "sweden",
    "sverige", "japan", "japon", "norway", "denmark", "finland", "holland",
    "netherlands", "belgium", "switzerland", "austria", "poland", "russia",
    "greece", "turkey", "israel", "india", "china", "korea", "canada",
    "australia", "new zealand", "ireland", "scotland", "wales", "portugal",
    "spain", "espana", "mexico", "peru", "colombia", "venezuela", "ecuador",
    "bolivia", "paraguay", "cuba", "puerto rico", "usa", "united states",
    "united kingdom", "uk", "england", "britain", "great britain", "europe",
    "latin america", "south america",
    "60s", "70s", "80s", "90s", "00s", "1960s", "1970s", "1980s", "1990s", "2000s",
    "female vocalists", "male vocalists", "instrumental", "instrumental rock",
    # performative, editorial and mood words that providers tag as genres. They are
    # true of the recording, not of a genre: a track can be instrumental and still be
    # post-rock, and "atmospheric" describes a mix, not a shelf.
    "instrumental music", "vocals", "vocal", "acapella", "a cappella", "karaoke",
    "cover", "covers", "live", "live recording", "studio", "bootleg", "demo",
    "remix", "remixes", "single", "ep", "album", "compilation", "greatest hits",
    "soundtrack", "score", "ost", "mixtape", "playlist", "radio edit",
    "atmospheric", "melancholic", "melancholy", "energetic", "beautiful", "epic",
    "chill", "relaxing", "dreamy", "dark", "happy", "sad",
    "aggressive", "calm", "mellow", "uplifting", "emotional", "moody", "spacey",
    "under 2000 listeners", "top 100", "favourite", "spotify", "youtube", "mp3",
}


def is_genre_like(label: object) -> bool:
    """Reject provider tags that are not genres before importing them."""
    return fold(label) not in NON_GENRE

# ---------------------------------------------------------------- layer 3: file

_FILE = config.STATE_DIR / "genre-aliases.json"
_cache: tuple[float, dict[str, str]] = (-1.0, {})


def operator_aliases(path: Path | None = None) -> dict[str, str]:
    """Load the operator's rules, re-reading only when the file changes."""
    global _cache
    p = path or _FILE
    try:
        mtime = p.stat().st_mtime
    except OSError:
        _cache = (-1.0, {})
        return {}
    if mtime == _cache[0]:
        return _cache[1]
    out: dict[str, str] = {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        for rule in raw.get("aliases", raw) if isinstance(raw, dict) else raw:
            src = rule.get("from") if isinstance(rule, dict) else None
            dst = rule.get("to") if isinstance(rule, dict) else None
            if src and dst:
                # `from` is stored as a fold key; accept a raw label too.
                out[fold(src)] = str(dst).strip()
    except Exception:
        out = {}
    _cache = (mtime, out)
    return out


# ------------------------------------------------------------------- resolution


def canonical(label: object) -> str:
    """Operator rule, then builtin, then the label spelled as the library has it."""
    text = str(label or "").strip()
    if not text:
        return ""
    key = fold(text)
    user = operator_aliases()
    if key in user:
        return user[key]
    if key in BUILTIN:
        return BUILTIN[key]
    squashed = key.replace(" ", "")
    if squashed in BUILTIN:
        return BUILTIN[squashed]
    return text


def labels_of(raw: object) -> list[str]:
    """Split one tag value into canonical labels.

    Tags are frequently multi-valued inside a single string
    ("New Wave, Post-Disco, Nu Disco"), so commas, semicolons, a bracketed pipe and a
    spaced slash separate. A bare hyphen or slash never does — "Post-Rock" and
    "R&B/Soul" stay one label.
    """
    out: list[str] = []
    for part in re.split(r"[;,]|\s\|\s|\s/\s", str(raw or "")):
        label = canonical(part)
        if label and label.lower() not in {x.lower() for x in out}:
            out.append(label)
    return out


def targets(station_genres) -> list[list[str]]:
    """Station genre words -> canonical word lists, ready for `covers`."""
    return [_words(canonical(g)) for g in (station_genres or ()) if fold(g)]


def covers(label: object, target_words: list[str]) -> bool:
    """True when the label is the target or refines it (contiguous word slice)."""
    if not target_words:
        return False
    words = _words(label)
    n = len(target_words)
    if n > len(words):
        return False
    return any(words[i : i + n] == target_words for i in range(len(words) - n + 1))


def matches(labels, target_sets: list[list[str]]) -> bool:
    """Any label covers any target — OR within a station's genre list."""
    return any(covers(label, t) for label in labels for t in target_sets)


def explain(label: object) -> dict:
    """Why a raw label resolves the way it does — for /genres and the future UI."""
    key = fold(label)
    user = operator_aliases()
    if key in user:
        return {"raw": str(label), "key": key, "canonical": user[key], "source": "operator"}
    if key in BUILTIN:
        return {"raw": str(label), "key": key, "canonical": BUILTIN[key], "source": "builtin"}
    if key.replace(" ", "") in BUILTIN:
        return {
            "raw": str(label),
            "key": key,
            "canonical": BUILTIN[key.replace(" ", "")],
            "source": "builtin",
        }
    return {"raw": str(label), "key": key, "canonical": str(label).strip(), "source": "library"}


_ACRONYMS = {"r&b", "idm", "edm", "uk", "us", "usa", "dj", "mc", "d&b", "dnb", "synthpop", "ebm"}


def pretty(label: str) -> str:
    """One house style for the vocabulary: "rock" and "Progressive Rock" agree."""
    out = []
    for word in str(label or "").split():
        if word.isupper() and 2 <= len(word) <= 4:
            out.append(word)  # already an acronym
        elif word.lower() in _ACRONYMS:
            out.append(word.upper())
        else:
            out.append(" ".join(p[:1].upper() + p[1:] for p in re.split(r"[-\u2010-\u2015_]", word) if p))
    return " ".join(out)


def unify_spellings(conn) -> int:
    """Collapse case and spacing variants to the spelling the library uses most.

    "Progressive Rock" and "Progressive rock" are the same genre to a listener and to
    the matcher, but they split the vocabulary in two and make the counts lie.
    """
    rows = conn.execute("SELECT label, count(*) c FROM track_genres GROUP BY label").fetchall()
    if not rows:
        return 0
    best: dict[str, tuple[str, int]] = {}
    for row in rows:
        label, count = row["label"], row["c"]
        key = fold(label)
        if not key:  # punctuation only: no spelling to unify it with
            continue
        current = best.get(key)
        if current is None or count > current[1] or (count == current[1] and label < current[0]):
            best[key] = (label, count)
    merged = 0
    # No transaction of its own: this runs inside the rebuild's, and the per-label
    # commit it used to do released a half-finished index.
    for row in rows:
        label = row["label"]
        winner = best.get(fold(label))
        if winner is None:
            continue
        target = pretty(winner[0])
        if label == target:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO track_genres (track_id, label, source)"
            " SELECT track_id, ?, source FROM track_genres WHERE label = ?",
            (target, label),
        )
        conn.execute("DELETE FROM track_genres WHERE label = ?", (label,))
        merged += 1
    return merged


def _add_alias(source: str, target: str) -> None:
    """Remember the rule, so a rebuild does not undo the merge."""
    path = _FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {"aliases": []}
    key = fold(source)
    aliases = [a for a in raw.get("aliases", []) if a.get("from") != key]
    aliases.append({"from": key, "to": target})
    raw["aliases"] = aliases
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def merge(conn, labels, into) -> int:
    """Fold any number of labels (raw tags or canonical names) into one name.

    The rows move now and the rule is recorded, so the next rebuild lands in the
    same place instead of resurrecting the old spelling.
    """
    target = pretty(str(into or "").strip())
    if not target:
        return 0
    moved = 0
    for label in labels:
        label = str(label or "").strip()
        if not label:
            continue
        canon = canonical(label)
        if fold(canon) == fold(target):
            continue  # already there: nothing to move, nothing worth remembering
        _add_alias(label, target)
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO track_genres (track_id, label, source)"
                " SELECT track_id, ?, source FROM track_genres WHERE label = ?",
                (target, canon),
            )
            conn.execute("DELETE FROM track_genres WHERE label = ?", (canon,))
        moved += 1
    return moved
