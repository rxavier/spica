"""The table: the records a set may come from, grouped by direction.

The artist shelf existed because nothing here could tell two tracks by the same act
apart. Audio analysis changes that: with a vector, energy, tempo and style per track,
the offer can be made of records and sorted by what they *do* — this one continues
what is on air, this one moves one axis, this one turns away, this one lands.

The table is handed over with its ids, so the set costs one model call and no
lookups. That is why the checks in the loop matter more here than in the tool path:
an id that is not on the table cannot air, no artist appears twice, and the station's
rules were already applied when the rows were drawn. Thirty-two records for a set of
three to ten is deliberate: more choice per direction than a set can spend.

What makes it not a menu is the grouping and the numbers: directions, not a running
order, more records than a set needs, and the previous table's artists excluded so it
can never become the same thirty-two records every twenty minutes.
"""

from __future__ import annotations

import hashlib
from collections import Counter

from .. import analysis, mixer, navidrome, queue

# A set is 3-10 tracks. Thirty-two records leave a real choice in every direction and
# still fit cheaply in one message (~3.5k characters, about 900 tokens).
TARGETS = (("continue", 8), ("shift", 8), ("contrast", 8), ("land", 8))
MIN_TABLE = 8
PER_LABEL = 3  # one label cannot own the table
ABSENCE = 40   # plays that count as "off the air"

LABELS = {
    "continue": ("Continue the flow", "closest to what is on air, same temperature"),
    "shift": ("Move one axis", "one thing changes — tempo, weight or colour — and the rest holds"),
    "contrast": ("Change it up", "a different family of sound; a deliberate turn"),
    "land": ("Land it", "calmer and slower than what is on air; a closer"),
}


def build(lib, station) -> dict | None:
    """The table for one set, or None when analysis is off or too thin to draw one."""
    if not analysis.enabled():
        return None
    rows = lib._station_rows(station)
    vectors = lib.track_vectors()
    features = lib._features()
    if not rows or not vectors or not features:
        return None

    blocked = _blocked_artists(lib, station)
    played = queue.recent_ids(station, 40)
    try:
        queued = set(mixer.queued_ids(station))
    except Exception:  # liquidsoap down: the table is still worth drawing
        queued = set()
    on_air = str((queue.recent(station, 1) or [{}])[-1].get("id") or "")

    labels = navidrome.labels_by_track(lib.conn)
    try:
        ranks = lib.popularity_ranks(rows)
    except Exception:
        ranks = {}
    absence = _absence(lib, station)

    candidates = []
    for row in rows:
        track_id = row["id"]
        if track_id in played or track_id in queued or track_id == on_air:
            continue
        vector = vectors.get(track_id)
        feature = features.get(track_id)
        if vector is None or not feature:
            continue
        artist = (row["album_artist"] or row["artist"] or "").strip()
        key = navidrome.name_key(artist)
        if not key or key in blocked:
            continue
        year = int(row["year"] or 0)
        candidates.append({
            "id": track_id,
            "artist": artist,
            "artist_key": key,
            "title": row["title"] or "",
            "album": row["album"] or "",
            "year": year or None,
            "duration": row["duration"],
            "label": (labels.get(track_id) or ["?"])[0],
            "decade": year // 10 * 10 if year else 0,
            "energy": float(feature["energy"]) if feature.get("energy") is not None else 0.5,
            "bpm": float(feature["bpm"]) if feature.get("bpm") else None,
            "key": feature.get("key") or "",
            "sound": feature.get("sound") or "",
            "family": feature.get("family") or "",
            "popularity": float(ranks.get(track_id, 0.5)),
            "absence": int(absence.get(key, ABSENCE * 6)),
            "vector": vector,
        })
    if len(candidates) < MIN_TABLE:
        return None

    anchor = _anchor(lib, station, features, vectors)
    if anchor is None:
        anchor = {
            "vector": _mean_vector([c["vector"] for c in candidates]),
            "energy": _median([c["energy"] for c in candidates]),
            "bpm": _median([c["bpm"] for c in candidates if c["bpm"]]),
            "family": "",
        }
    for candidate in candidates:
        candidate["sim"] = float(candidate["vector"] @ anchor["vector"])

    groups = _groups(candidates, anchor, getattr(lib, "draw_seed", 0) or 0)
    ids = [track["id"] for group in groups for track in group["tracks"]]
    if len(ids) < MIN_TABLE:
        return None
    artists = []
    for group in groups:
        for track in group["tracks"]:
            if track["artist"] not in artists:
                artists.append(track["artist"])
    return {
        "groups": groups,
        "ids": ids,
        "artists": artists,
        "anchor": {k: anchor.get(k) for k in ("energy", "bpm", "family")},
    }


def render(table: dict) -> str:
    """The table as the brief carries it: id first, then what the record is and does."""
    lines = [
        f"The table for this set — {len(table['ids'])} records in {len(table['groups'])} "
        "directions. Every track comes from it, no artist twice, three to ten tracks, in "
        "the order they should air. The id leads each row; no lookups are needed."
    ]
    for group in table["groups"]:
        label, note = group["label"], group["note"]
        lines.append("")
        lines.append(f"{label} ({note}):")
        for track in group["tracks"]:
            detail = " · ".join(part for part in (
                str(track["year"] or ""),
                track["sound"],
                f"{round(track['bpm'])} bpm" if track["bpm"] else "",
                track["key"],
                f"energy {track['energy']:.2f}" if track["energy"] is not None else "",
            ) if part)
            lines.append(
                f"- {track['id']} · {track['artist']} — {track['title']}"
                + (f" · {detail}" if detail else "")
            )
    return "\n".join(lines)


# ------------------------------------------------------------------ the directions

def _groups(candidates: list[dict], anchor: dict, seed: int) -> list[dict]:
    used_ids: set[str] = set()
    used_artists: set[str] = set()
    label_counts: Counter = Counter()

    def take(ordered: list[dict], n: int) -> list[dict]:
        out = []
        for candidate in ordered:
            if len(out) >= n:
                break
            if candidate["id"] in used_ids or candidate["artist_key"] in used_artists:
                continue
            # An unlabelled track is not a label with three slots: without this, a
            # library whose tags never reached the index would cap the table at three.
            if candidate["label"] != "?" and label_counts[candidate["label"]] >= PER_LABEL:
                continue
            used_ids.add(candidate["id"])
            used_artists.add(candidate["artist_key"])
            label_counts[candidate["label"]] += 1
            out.append(candidate)
        return out

    energy = anchor.get("energy") or 0.5
    bpm = anchor.get("bpm") or 0.0
    family = anchor.get("family") or ""

    def same_band(c):
        return abs(c["energy"] - energy) <= 0.2

    def one_axis(c):
        if not c["bpm"] or not bpm:
            return 0.10 <= abs(c["energy"] - energy) <= 0.30
        tempo_moves = 6 <= abs(c["bpm"] - bpm) <= 30
        energy_moves = 0.10 <= abs(c["energy"] - energy) <= 0.30
        return tempo_moves != energy_moves

    def turns(c):
        if family:
            return c["family"] and c["family"] != family and abs(c["energy"] - energy) >= 0.12
        return c["sim"] <= _percentile([x["sim"] for x in candidates], 25)

    def lands(c):
        if not c["bpm"] or not bpm:
            return c["energy"] <= energy - 0.15
        return c["energy"] <= energy - 0.15 and c["bpm"] <= bpm + 5

    by_sim = sorted(candidates, key=lambda c: (-c["sim"], -c["absence"]))
    absent_first = sorted(candidates, key=lambda c: (-c["absence"], -c["sim"]))

    plans = {
        "continue": [c for c in by_sim if same_band(c)] or by_sim,
        "shift": [c for c in by_sim if one_axis(c)] or by_sim,
        "contrast": [c for c in by_sim if turns(c)] or list(reversed(by_sim)),
        "land": sorted(
            [c for c in candidates if lands(c)],
            key=lambda c: (c["energy"], -c["absence"]),
        ) or by_sim,
    }

    groups = []
    for name, size in TARGETS:
        picked = take(plans[name], size)
        groups.append({"name": name, "label": LABELS[name][0], "note": LABELS[name][1],
                       "tracks": picked})

    # A thin direction is topped up from whoever has been off the air longest, so the
    # rotation memory survives a station where one corner dominates the vectors.
    for group in groups:
        if len(group["tracks"]) < 2:
            group["tracks"] += take(absent_first, 3 - len(group["tracks"]))

    _spread_decades(groups, candidates, used_ids, used_artists, label_counts, seed)

    out = []
    for group in groups:
        if not group["tracks"]:
            continue
        group["tracks"].sort(key=lambda c: _seed_key(seed, c["id"]))
        out.append({
            "name": group["name"],
            "label": group["label"],
            "note": group["note"],
            "tracks": [_public(c) for c in group["tracks"]],
        })
    return out


def _spread_decades(groups, candidates, used_ids, used_artists, label_counts, seed) -> None:
    """Two decades on the table when the station can pay for it."""
    decades = {c["decade"] for g in groups for c in g["tracks"] if c["decade"]}
    if len(decades) >= 2:
        return
    for candidate in sorted(candidates, key=lambda c: _seed_key(seed, c["id"])):
        if candidate["id"] in used_ids or not candidate["decade"] or candidate["decade"] in decades:
            continue
        # Swap it in for the last pick of the longest group, so no direction empties.
        target = max(groups, key=lambda g: len(g["tracks"]))
        if not target["tracks"]:
            continue
        dropped = target["tracks"][-1]
        target["tracks"][-1] = candidate
        used_ids.discard(dropped["id"])
        used_ids.add(candidate["id"])
        return


def _public(candidate: dict) -> dict:
    return {
        "id": candidate["id"],
        "artist": candidate["artist"],
        "title": candidate["title"],
        "album": candidate["album"],
        "year": candidate["year"],
        "duration": candidate["duration"],
        "sound": candidate["sound"],
        "bpm": candidate["bpm"],
        "key": candidate["key"],
        "energy": candidate["energy"],
        "popularity": round(candidate["popularity"], 2),
    }


# ------------------------------------------------------------------- the context

def _blocked_artists(lib, station) -> set[str]:
    """Artists that may not be offered: just played, queued, or on the last table.

    No sonic distance here — the point of the table is that "close to what is on air"
    is one of the directions on purpose.
    """
    names: list[str] = []

    def name_of(track_id: str, fallback: str = "") -> str:
        row = lib.by_id(track_id) if track_id else None
        if row is not None:
            return row.album_artist or row.artist or ""
        return fallback

    for entry in queue.recent(station, 6):
        name = name_of(str(entry.get("id") or ""), str(entry.get("artist") or ""))
        if name:
            names.append(name)
    names.extend(queue.last_shelf(station))
    try:
        for track_id in mixer.queued_ids(station):
            name = name_of(track_id)
            if name:
                names.append(name)
    except Exception:
        pass
    return {navidrome.name_key(name) for name in names if name}


def _absence(lib, station, look: int = ABSENCE * 6) -> dict[str, int]:
    """For each artist, how many songs ago it last aired (large when it never did)."""
    since: dict[str, int] = {}
    for ago, entry in enumerate(reversed(queue.recent(station, look))):
        row = lib.by_id(str(entry.get("id") or ""))
        name = (row.album_artist or row.artist) if row is not None else str(entry.get("artist") or "")
        key = navidrome.name_key(name)
        if key and key not in since:
            since[key] = ago
    return since


def _anchor(lib, station, features: dict, vectors: dict) -> dict | None:
    """What the set has to sit next to: the on-air track, or its artist's sound."""
    entry = (queue.recent(station, 1) or [{}])[-1]
    track_id = str(entry.get("id") or "")
    vector = vectors.get(track_id)
    feature = features.get(track_id) or {}
    if vector is None and track_id:
        row = lib.by_id(track_id)
        name = (row.album_artist or row.artist) if row is not None else ""
        if name:
            vector = _artist_vector(lib, name, vectors)
            if vector is not None:
                feature = _artist_feature(lib, name, features) or feature
    if vector is None:
        return None
    return {
        "vector": vector,
        "energy": float(feature["energy"]) if feature.get("energy") is not None else None,
        "bpm": float(feature["bpm"]) if feature.get("bpm") else None,
        "family": feature.get("family") or "",
    }


def _artist_vector(lib, name: str, vectors: dict):
    key = navidrome.name_key(name)
    parts = [
        vectors[row["id"]] for row in
        lib.conn.execute("SELECT id FROM tracks WHERE album_artist_key = ?", (key,))
        if row["id"] in vectors
    ]
    return _mean_vector(parts) if parts else None


def _artist_feature(lib, name: str, features: dict) -> dict:
    """Median-ish feature of an artist's analysed tracks, for an unanalysed on-air one."""
    key = navidrome.name_key(name)
    rows = [
        features[row["id"]] for row in
        lib.conn.execute("SELECT id FROM tracks WHERE album_artist_key = ?", (key,))
        if row["id"] in features
    ]
    energies = [r["energy"] for r in rows if r.get("energy") is not None]
    bpms = [r["bpm"] for r in rows if r.get("bpm")]
    families = [r["family"] for r in rows if r.get("family")]
    if not rows:
        return {}
    return {
        "energy": sum(energies) / len(energies) if energies else None,
        "bpm": _median(bpms),
        "family": families[0] if families else "",
    }


def _mean_vector(vectors: list):
    if not vectors:
        return None
    total = vectors[0].copy()
    for vector in vectors[1:]:
        total += vector
    norm = float((total @ total) ** 0.5)
    return total / norm if norm else total


def _median(values: list) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[len(ordered) // 2])


def _percentile(values: list, pct: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, len(ordered) * pct // 100)])


def _seed_key(seed: int, track_id: str) -> bytes:
    return hashlib.blake2b(f"{seed}|{track_id}".encode(), digest_size=8).digest()
