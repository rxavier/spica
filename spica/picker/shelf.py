"""The shelf: the artists the DJ may choose from for one set.

A list of names is a stronger instrument than a rule. The model treats what it is
handed as its material — given ten names, three to five of every six tracks come off
them — so handing it a *representative* slice of the station makes the set a smaller
version of the station instead of a smaller version of the model's taste.

Everything decided here is a fact: what played, what is queued, what sounds like what,
how much of the station each label is, which decade an artist belongs to, how well
played their records are. None of it is a judgement about the music; the choice of
which of these ten to play, and in what order, is still the DJ's.
"""

from __future__ import annotations

import bisect
import hashlib
import statistics
from collections import Counter, defaultdict

from .. import analysis, enrich, mixer, navidrome, queue

# What keeps an artist off the shelf: the last few plays, the tracks already waiting,
# and the artists that sound most like the three tracks either side of this set.
BEHIND = 5
QUEUED = 5
SIMILAR_K = 3
SIMILAR_FLOOR = 0.15

# Ten names is a shelf, not a menu: a six-track set drawn from ten leaves a real
# choice and still makes the offer mean something. Two more are added from whoever has
# been off the air longest, so a record that has not had its turn in hours is not
# excluded by a stratification that never thinks about time.
SHELF = 10
ABSENT = 2
ABSENCE = 40      # plays that count as "off the air"
PER_LABEL = 3     # one label cannot own the shelf
MIN_SHELF = 3     # fewer than this is not a shelf, it is a wall: no restriction at all


def build(lib, station) -> list[dict]:
    """The shelf for one set: artist names, with the reason each one is there."""
    rows = lib._station_rows(station)
    if not rows:
        return []
    profiles = _profiles(lib, rows)
    blocked = _blocked(lib, station, profiles)
    pool = [p for key, p in profiles.items() if key not in blocked]
    if not pool:
        return []

    base = _stratified(pool, SHELF, lib.draw_seed)
    taken = {p["key"] for p in base}
    extra = _longest_absent(lib, station, [p for p in pool if p["key"] not in taken], ABSENT)
    chosen = base + extra

    # Present it unordered. A list in a meaningful order gets read top-down and turned
    # into a cast; a handful of names has to be sorted out by the DJ.
    chosen.sort(key=lambda p: _seed_key(lib.draw_seed, p["key"]))
    if len(chosen) < MIN_SHELF:
        # A small library after a night of plays can leave two or three names. Handing
        # those over as the only thing that can air would stop the station rather than
        # vary it, so the restriction is dropped and the night continues.
        return []
    return [{"artist": p["name"], "why": p.get("why", "shelf")} for p in chosen]


# ------------------------------------------------------------------ the ingredients


def _seed_key(seed: int, key: str) -> bytes:
    return hashlib.blake2b(f"{seed}|{key}".encode(), digest_size=8).digest()


def _profiles(lib, rows: list) -> dict[str, dict]:
    """What each artist on the station is made of: weight, label, era, standing."""
    labels = navidrome.labels_by_track(lib.conn)
    ranks = lib.popularity_ranks(rows)
    standing = _standing(lib.conn)
    raw: dict[str, dict] = {}
    for row in rows:
        name = row["album_artist"] or row["artist"] or ""
        if not name:
            continue
        key = navidrome.name_key(name)
        item = raw.setdefault(
            key,
            {"key": key, "name": name, "tracks": 0, "labels": Counter(), "years": [], "pop": []},
        )
        item["tracks"] += 1
        for label in labels.get(row["id"], ()):
            item["labels"][label] += 1
        if row["year"]:
            item["years"].append(int(row["year"]))
        if row["id"] in ranks:
            item["pop"].append(ranks[row["id"]])

    for key, item in raw.items():
        item["label"] = item["labels"].most_common(1)[0][0] if item["labels"] else "?"
        item["decade"] = int(statistics.median(item["years"])) // 10 * 10 if item["years"] else 0
        item["tier"] = _tier(standing.get(key))
    return raw


def _standing(conn) -> dict[str, float]:
    """How big each artist is, 0 = the smallest name on the shelf, 1 = the biggest.

    The rank the tools use is within-artist — "which of their songs do people know" —
    and averaging that over an artist gives 0.5 for everybody, which is no use for
    deciding whether a shelf has both ends of the roster. This one is the artist's own
    median play count, ranked against the other artists from the same provider (Last.fm
    and ListenBrainz counts are not on the same scale).
    """
    stats = {
        r["track_id"]: (float(r["playcount"] or 0), r["source"] or "")
        for r in conn.execute("SELECT track_id, playcount, source FROM track_popularity")
    }
    by_source: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in conn.execute("SELECT id, album_artist FROM tracks WHERE album_artist != ''"):
        hit = stats.get(row["id"])
        if hit:
            by_source[hit[1]][navidrome.name_key(row["album_artist"])].append(hit[0])
    out: dict[str, float] = {}
    for per_artist in by_source.values():
        medians = {k: statistics.median(v) for k, v in per_artist.items() if v}
        ordered = sorted(medians.values())
        if not ordered:
            continue
        span = max(1, len(ordered) - 1)
        for key, value in medians.items():
            out[key] = bisect.bisect_left(ordered, value) / span
    return out


def _tier(percentile: float | None) -> int:
    """Which third of the roster an artist sits in: 0 = deep, 1 = middle, 2 = big."""
    if percentile is None:
        return 1
    if percentile < 1 / 3:
        return 0
    if percentile < 2 / 3:
        return 1
    return 2


def _blocked(lib, station, profiles: dict[str, dict]) -> set[str]:
    """Artists that may not be offered: just played, queued, or too like what is next."""
    blocked: set[str] = set()
    anchors: list[str] = []

    def artist_of(track_id: str, fallback: str = "") -> str:
        row = lib.by_id(track_id) if track_id else None
        if row is not None:
            return row.album_artist or row.artist or ""
        return fallback

    plays = queue.recent(station, BEHIND + 1)
    for entry in plays[-(BEHIND + 1):]:
        name = artist_of(str(entry.get("id") or ""), str(entry.get("artist") or ""))
        if name:
            blocked.add(navidrome.name_key(name))
            anchors.append(name)

    # The ledger is airings, and on a quiet station those are the standby pool: an artist
    # the DJ chose an hour ago would otherwise be free to come straight back.
    for name in queue.recent_pick_artists(station, BEHIND + 3):
        blocked.add(navidrome.name_key(name))

    # The artists offered a set ago have had their turn at being offered. Without this
    # the same twelve names come back whenever the plays in between happen to leave them
    # clear, and the shelf becomes a fixed menu again, only smaller.
    for name in queue.last_shelf(station):
        blocked.add(navidrome.name_key(name))

    queued: list[str] = []
    try:
        waiting = mixer.queued_ids(station)[:QUEUED]
    except Exception:  # liquidsoap down: the shelf is still worth building
        waiting = []
    for track_id in waiting:
        name = artist_of(track_id)
        if name:
            blocked.add(navidrome.name_key(name))
            queued.append(name)

    # prev, now, next — the three records this set has to sit between.
    newest = list(reversed(anchors))
    around = [newest[0]] if newest else []          # what is on air
    around += newest[1:2]                            # the one before it
    around += queued[:1]                             # the one that plays next
    index = _neighbours(lib.conn, profiles, [a for a in around if a])
    for anchor in around:
        key = navidrome.name_key(anchor)
        nearest = sorted(index.get(key, {}).items(), key=lambda kv: -kv[1])[:SIMILAR_K]
        for other, match in nearest:
            if match >= SIMILAR_FLOOR:
                blocked.add(other)

    # The same "sounds too like what is next" rule, from the audio vectors when the
    # optional analysis ran. Worth having beside Last.fm rather than instead of it:
    # the provider can only name artists this library does not own, so for more than
    # half the roster its three nearest neighbours are unplayable and the rule does
    # nothing. Audio neighbours are drawn from the library by construction.
    store = lib.analysis_store() if callable(getattr(lib, "analysis_store", None)) else None
    if store is not None:
        try:
            blocked |= analysis.audio_neighbours(
                lib.conn, store, set(profiles), [a for a in around if a], k=SIMILAR_K
            )
        except Exception:  # analysis must never cost a pick
            pass
    return blocked


def _neighbours(conn, profiles: dict[str, dict], anchors: list[str]) -> dict[str, dict[str, float]]:
    """Similarity between artists this station owns, both directions, best score wins.

    Last.fm's lists are one-way — L'Impératrice names Nu Genea, Nu Genea names none of
    ours — so an edge counts whichever artist it was recorded from. Anchors with
    nothing cached are asked once, which keeps a shelf working on a fresh index.
    """
    owned = {p["name"] for p in profiles.values()}
    for anchor in set(anchors):
        if anchor in owned and not enrich.cached_scores(conn, anchor):
            enrich.similarity_for(conn, anchor)
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for row in conn.execute("SELECT name, similar, match FROM artist_similarity"):
        a, b = navidrome.name_key(row["name"]), navidrome.name_key(row["similar"])
        if a == b or a not in profiles or b not in profiles:
            continue
        match = float(row["match"] or 0.0)
        out[a][b] = max(out[a].get(b, 0.0), match)
        out[b][a] = max(out[b].get(a, 0.0), match)
    return out


# --------------------------------------------------------------------- the draw


def _quotas(weights: Counter, slots: int, cap: int) -> dict[str, int]:
    """Slots per label, proportional to its weight, none above the cap (D'Hondt).

    Returns fewer than `slots` when the caps add up to less than the shelf wants: a pool
    with two labels and a cap of three cannot pay for ten names, and the caller tops the
    list up with the cap lifted rather than handing over a short shelf.
    """
    quota = {label: 0 for label in weights}
    for _ in range(slots):
        open_labels = [l for l in weights if quota[l] < cap]
        if not open_labels:
            break
        best = max(open_labels, key=lambda l: (weights[l] / (quota[l] + 1), -quota[l]))
        quota[best] += 1
    return quota


def _stratified(pool: list[dict], size: int, seed: int) -> list[dict]:
    """A small version of the station: label weight, both ends of the roster, two eras."""
    if len(pool) <= size:
        return [dict(p, why="shelf") for p in pool]

    order = sorted(pool, key=lambda p: _seed_key(seed, p["key"]))
    chosen: list[dict] = []
    taken: set[str] = set()

    def take(item: dict) -> None:
        taken.add(item["key"])
        chosen.append(dict(item, why="shelf"))

    # One artist from each third of the roster first, on a different label where the
    # shelf allows it: a list of only the big names is a playlist, and one of only deep
    # cuts is a different station.
    used_labels: set[str] = set()
    for tier in (2, 1, 0):
        options = [p for p in order if p["tier"] == tier and p["key"] not in taken]
        if not options:
            continue
        item = next((p for p in options if p["label"] not in used_labels), options[0])
        take(item)
        used_labels.add(item["label"])
        if len(chosen) >= size:
            return chosen

    # Then by label weight, in proportion to what the station actually holds.
    left = Counter(p["label"] for p in pool if p["key"] not in taken)
    for label, count in _quotas(left, size - len(chosen), PER_LABEL).items():
        have = sum(1 for p in chosen if p["label"] == label)
        need = max(0, min(have + count, PER_LABEL) - have)
        for item in order:
            if need <= 0 or len(chosen) >= size:
                break
            if item["label"] != label or item["key"] in taken:
                continue
            take(item)
            need -= 1
        if len(chosen) >= size:
            break

    # A thin shelf can leave the quotas unfilled; the cap goes before the list does.
    # Ten names with one label carrying four beats six names in a neat three-three.
    for respect_cap in (True, False):
        for item in order:
            if len(chosen) >= size:
                break
            if item["key"] in taken:
                continue
            if respect_cap and sum(1 for p in chosen if p["label"] == item["label"]) >= PER_LABEL:
                continue
            take(item)

    _spread_decades(order, chosen)
    return chosen[:size]


def _spread_decades(order: list[dict], chosen: list[dict]) -> None:
    """Two decades on the shelf when the pool can pay for it."""
    if len({p["decade"] for p in chosen if p["decade"]}) >= 2:
        return
    taken = {p["key"] for p in chosen}
    seen = {p["decade"] for p in chosen}
    for item in order:
        if not item["decade"] or item["key"] in taken or item["decade"] in seen:
            continue
        # Replace the newest pick whose label keeps another representative behind it,
        # so the era swap does not empty a quota.
        for i in range(len(chosen) - 1, -1, -1):
            label = chosen[i]["label"]
            if sum(1 for c in chosen if c["label"] == label) > 1:
                chosen[i] = dict(item, why="shelf")
                return


def _longest_absent(lib, station, pool: list[dict], k: int) -> list[dict]:
    """Whoever has been off the air longest, among those clean of the last forty plays.

    A threshold alone is not enough: on a sixty-odd-artist station most artists have
    not played in the last forty songs, so a random draw from that set reads as two more
    names off the pile. Ranking by how long ago they last aired re-ranks itself every
    set — play one and it leaves the top.
    """
    plays = queue.recent(station, ABSENCE * 6)
    since: dict[str, int] = {}
    for ago, entry in enumerate(reversed(plays)):
        row = lib.by_id(str(entry.get("id") or ""))
        name = (row.album_artist or row.artist) if row is not None else str(entry.get("artist") or "")
        key = navidrome.name_key(name)
        if key and key not in since:
            since[key] = ago

    never = ABSENCE * 6
    scored = []
    for item in pool:
        ago = since.get(item["key"])
        if ago is not None and ago < ABSENCE:
            continue
        scored.append((ago if ago is not None else never, item))
    scored.sort(key=lambda pair: (-pair[0], _seed_key(lib.draw_seed, pair[1]["key"])))
    return [dict(item, why="absent") for _, item in scored[:k]]
