"""Candidate tracks for one set, grouped by the transition each direction makes.

With analysis on, the candidates are records rather than artists: the station's tracks are
split into continue / single-axis / contrast / closing / familiar directions, and the
model gets ids, so a set costs one call and no lookups. An id that is not in the
candidate list cannot air, no artist appears twice, and the station's rules are applied
before the list is built.
"""

from __future__ import annotations

import statistics
from collections import Counter

from .. import analysis, ledger, mixer, navidrome, queue
from ..names import seed_key
from ..queries import tracks as track_queries
from .memory import ABSENCE, artist_absence, blocked_artists, spread_decades

# A set is 3-10 tracks. Thirty-two records leave a real choice in every direction and
# still fit cheaply in one message (~3.5k characters, about 900 tokens). The split
# depends on the station's lean: a "hits" station gets a bigger familiar block, a
# "deep" station a smaller one.
TARGETS = (("continue", 7), ("single_axis", 6), ("contrast", 6), ("closing", 6), ("familiar", 7))
MIN_CANDIDATES = 8
PER_LABEL = 3  # one label cannot own the candidate list
KNOWN = 0.66   # within-artist percentile that counts as "the one everyone knows"
# A record offered this many sets ago has had its turn whether or not it played: the
# play ledger alone let the familiar block hand over the same records it kept refusing.
# The window itself — sixty picks or eight hours of air — lives in `queue`, because the
# picker's tool rows report it too.
SEEN_SETS = 4
# The familiar block is one record per artist, so it needs the artist's turn remembered
# rather than the record's: two sets is enough to stop the same name coming back, and
# longer starves it — there are only so many artists with a known record in one family.
FAMILIAR_SETS = 2
# How much "not been on in ages" can move a candidate up its direction. Similarity
# alone lets the station drift back to one sound: `-absence` sat after `-sim` in the
# sort key, so it only ever broke an exact tie.
ABSENCE_WEIGHT = 0.12
# Readiness for full candidates, per station. Below this they still run, but with
# an explicit "not measured yet" group so new music is not invisible.
READY_TRACKS = 0.90
READY_ARTISTS = 0.80
UNMEASURED = 4

_MAJOR = {"B": 1, "F#": 2, "Gb": 2, "Db": 3, "C#": 3, "Ab": 4, "G#": 4, "Eb": 5,
          "D#": 5, "Bb": 6, "A#": 6, "F": 7, "C": 8, "G": 9, "D": 10, "A": 11, "E": 12}
_MINOR = {"Ab": 1, "G#": 1, "Eb": 2, "D#": 2, "Bb": 3, "A#": 3, "F": 4, "C": 5,
          "G": 6, "D": 7, "A": 8, "E": 9, "B": 10, "F#": 11, "Gb": 11, "Db": 12, "C#": 12}


def _camelot(label: str):
    parts = (label or "").split()
    if len(parts) != 2:
        return None
    return (_MAJOR if parts[1] == "maj" else _MINOR).get(parts[0])


def _same_temperature(candidate: dict, energy: float, bpm: float, key) -> bool:
    """Energy is not enough: tempo and key are what stop a raw grunge song reading as
    a continuation of an anthemic indie one at the same loudness."""
    if abs(candidate["energy"] - energy) > 0.2:
        return False
    if bpm and candidate["bpm"] and abs(candidate["bpm"] - bpm) > 20:
        return False
    if key is not None:
        other = _camelot(candidate["key"])
        if other is not None:
            distance = abs(key - other)
            if min(distance, 12 - distance) > 2:
                return False
    return True


LABELS = {
    "continue": ("Continue the flow", "closest to what is on air, same temperature"),
    "single_axis": ("Move one axis", "one thing changes — tempo, weight or colour — and the rest holds"),
    "contrast": ("Change it up", "a different family of sound; a deliberate turn"),
    "closing": ("Land it", "calmer and slower than what is on air; a closer"),
    "familiar": ("The one everyone knows",
                 "the familiar song, in the same family as what is on air"),
    "unmeasured": ("Not measured yet", "no sonic data yet; offered so it is not invisible"),
}


def targets(station) -> tuple[tuple[str, int], ...]:
    """The same thirty-two slots, weighted by how the station leans.

    `popularity` on a station was only ever about the pool order; with candidates it
    decides how much is familiar. A hits station gets seven familiar
    records, a deep one four, balanced six.
    """
    known = 7 if station.deep_bias < 0 else (4 if station.deep_bias > 0 else 6)
    rest = 32 - known
    base, extra = divmod(rest, 4)
    names = ("continue", "single_axis", "contrast", "closing")
    return tuple(
        [(name, base + (1 if i < extra else 0)) for i, name in enumerate(names)]
        + [("familiar", known)]
    )


def build(lib, station) -> dict | None:
    """The candidate tracks for one set, or None when analysis is off or too thin."""
    if not analysis.enabled():
        return None
    rows = lib.station_rows(station)
    vectors = lib.track_vectors()
    features = lib.features()
    if not rows or not vectors or not features:
        return None

    measured = [row for row in rows
                if row["id"] in vectors and row["id"] in features]
    coverage = len(measured) / len(rows) if rows else 0.0
    all_artists = {navidrome.name_key(r["album_artist"] or r["artist"] or "")
                   for r in rows}
    known_artists = {navidrome.name_key(r["album_artist"] or r["artist"] or "")
                     for r in measured}
    artist_coverage = len(known_artists) / len(all_artists) if all_artists else 0.0
    partial = coverage < READY_TRACKS or artist_coverage < READY_ARTISTS

    entries = queue.recent(station, ABSENCE * 6)
    on_air_entry = entries[-1] if entries else {}
    blocked = _blocked_artists(lib, station, entries=entries)
    played = ledger.recently_out(station)
    try:
        queued = set(mixer.queued_ids(station))
    except Exception:  # liquidsoap down: candidates are still worth drawing
        queued = set()
    on_air = str(on_air_entry.get("id") or "")

    labels = lib.labels_by_track()
    try:
        ranks = lib.popularity_ranks(rows)
    except Exception:
        ranks = {}
    absence = artist_absence(lib, station, entries=entries)

    eligible = _eligible(rows, vectors, features, played, queued, on_air, blocked)
    candidates = _candidates(eligible, labels, ranks, absence)
    if len(candidates) < MIN_CANDIDATES:
        return None
    # A record the last few sets offered has had its turn, played or not — that is what
    # stops the familiar block from becoming a fixed cast. On a small station the rule
    # could empty the candidates, and fewer candidates are worse than a repeated one.
    seen = ledger.candidate_ids(station, SEEN_SETS)
    fresh = [c for c in candidates if c["id"] not in seen]
    if len(fresh) >= MIN_CANDIDATES:
        candidates = fresh

    anchor = _anchor(lib, station, features, vectors, on_air_entry)
    if anchor is None or anchor.get("energy") is None:
        # Without a real anchor the directions are guesses against the station median;
        # the artist candidates are better than that.
        return None
    for candidate in candidates:
        candidate["sim"] = float(candidate["vector"] @ anchor["vector"])

    groups = _groups(candidates, anchor, getattr(lib, "draw_seed", 0) or 0, station,
                     seen_artists=_seen_artists(
                         lib, ledger.candidate_ids(station, FAMILIAR_SETS)))
    ids = [track["id"] for group in groups for track in group["tracks"]]
    if len(ids) < MIN_CANDIDATES:
        return None
    artists = []
    for group in groups:
        for track in group["tracks"]:
            if track["artist"] not in artists:
                artists.append(track["artist"])

    if partial:
        unknown = _unmeasured(eligible, set(navidrome.name_key(a) for a in artists),
                              ranks, UNMEASURED)
        if unknown:
            groups.append({"name": "unmeasured", "label": LABELS["unmeasured"][0],
                           "note": LABELS["unmeasured"][1], "tracks": unknown})
            ids += [track["id"] for track in unknown]
            for track in unknown:
                if track["artist"] not in artists:
                    artists.append(track["artist"])

    return {
        "groups": groups,
        "ids": ids,
        "artists": artists,
        "anchor": {k: anchor.get(k) for k in ("energy", "bpm", "key", "family")},
        "coverage": round(coverage, 3),
        "partial": partial,
    }


def render(candidates: dict) -> str:
    """The candidate tracks as the prompt carries them: id first, then what the record is and does."""
    lines = [
        f"Candidate tracks for this set — {len(candidates['ids'])} records in {len(candidates['groups'])} "
        "directions. Every track comes from it, no artist twice, three to ten tracks, in "
        "the order they should air. The id leads each row; no lookups are needed."
    ]
    for group in candidates["groups"]:
        label, note = group["label"], group["note"]
        lines.append("")
        lines.append(f"{label} ({note}):")
        for track in group["tracks"]:
            if track.get("measured", True):
                parts = (
                    str(track["year"] or ""),
                    track["sound"],
                    analysis.tempo_label(track["bpm"], track["energy"]),
                    track["key"],
                    f"energy {track['energy']:.2f}" if track["energy"] is not None else "",
                    _popularity_tier(track["popularity"], track["popularity_known"]),
                )
            else:
                parts = (str(track["year"] or ""), "not measured",
                         _popularity_tier(track["popularity"], track["popularity_known"]))
            detail = " · ".join(part for part in parts if part)
            lines.append(
                f"- {track['id']} · {track['artist']} — {track['title']}"
                + (f" · {detail}" if detail else "")
            )
    return "\n".join(lines)


# ------------------------------------------------------------------ the directions

def _eligible(rows, vectors, features, played, queued, on_air, blocked):
    """Rows a set may draw from at all, split into measured/unmeasured by the caller.

    Played, queued, on-air and blocked artists are dropped once here, so a new
    exclusion applies to measured and unmeasured candidates together.
    """
    out = []
    for row in rows:
        track_id = row["id"]
        if track_id in played or track_id in queued or track_id == on_air:
            continue
        artist = (row["album_artist"] or row["artist"] or "").strip()
        key = navidrome.name_key(artist)
        if not key or key in blocked:
            continue
        out.append((row, artist, key, vectors.get(track_id), features.get(track_id)))
    return out


def _candidates(eligible, labels, ranks, absence) -> list[dict]:
    """Every measured record a set may be built from, with what it sounds like and how known."""
    out = []
    for row, artist, key, vector, feature in eligible:
        if vector is None or not feature:
            continue
        track_id = row["id"]
        year = int(row["year"] or 0)
        out.append({
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
            "popularity_known": track_id in ranks,
            "absence": int(absence.get(key, ABSENCE * 6)),
            "vector": vector,
        })
    return out


def _seen_artists(lib, candidate_ids: set[str]) -> set[str]:
    """The artists whose records the last sets already offered, by name key.

    Ids alone are not enough: an artist with several familiar records keeps a seat in
    the familiar block by serving a different one each time, so the same five names
    came back set after set while every individual record was rotated out.
    """
    artists = lib.artists_by_ids(candidate_ids)
    return {
        navidrome.name_key(name)
        for name in artists.values()
        if name
    }


def _absent(candidate: dict) -> float:
    """How long since this artist aired, 0 = just now, 1 = longer than the window."""
    return min(int(candidate["absence"]), ABSENCE) / ABSENCE


def _candidate_rank(candidate: dict, lean: float) -> float:
    """Where a candidate sits in its direction: what it sounds like, and when it aired.

    Similarity alone lets the station drift back to one corner of the library. A record that
    aired three songs ago beat one that had not been on all day by a thousandth of a
    cosine, because `-absence` came after `-sim` in the sort key as a tiebreak that never
    fired. Absence is a term of its own now: a bonus, capped, so it lifts a record that
    has not had its turn without overruling "this is the same temperature".
    """
    return (float(candidate["sim"])
            + ABSENCE_WEIGHT * _absent(candidate)
            + 0.10 * lean * float(candidate["popularity"]))


def _groups(candidates: list[dict], anchor: dict, seed: int, station,
            seen_artists: set[str] | None = None) -> list[dict]:
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
            # library whose tags never reached the index would cap the candidates at three.
            if candidate["label"] != "?" and label_counts[candidate["label"]] >= PER_LABEL:
                continue
            used_ids.add(candidate["id"])
            used_artists.add(candidate["artist_key"])
            label_counts[candidate["label"]] += 1
            out.append(candidate)
        return out

    energy = anchor.get("energy") or 0.5
    bpm = anchor.get("bpm") or 0.0
    key = _camelot(anchor.get("key") or "")
    family = anchor.get("family") or ""

    def same_band(c):
        return _same_temperature(c, energy, bpm, key)

    def one_axis(c):
        if not c["bpm"] or not bpm:
            return 0.10 <= abs(c["energy"] - energy) <= 0.30
        tempo_moves = 6 <= abs(c["bpm"] - bpm) <= 30
        energy_moves = 0.10 <= abs(c["energy"] - energy) <= 0.30
        return tempo_moves != energy_moves

    turn_cut = _percentile([x["sim"] for x in candidates], 25)

    def turns(c):
        if family:
            return c["family"] and c["family"] != family and abs(c["energy"] - energy) >= 0.12
        return c["sim"] <= turn_cut

    def lands(c):
        if not c["bpm"] or not bpm:
            return c["energy"] <= energy - 0.15
        return c["energy"] <= energy - 0.15 and c["bpm"] <= bpm + 5

    # How strongly to favour the familiar end of the catalogue, from the station's
    # own lean: +1 for hits, 0 balanced, -1 for deep.
    lean = -station.deep_bias
    by_sim = sorted(
        candidates,
        key=lambda c: (-_candidate_rank(c, lean), seed_key(seed, c["id"])),
    )

    plans = {
        # Continue may fall back to the nearest records: "closest to on air" is still
        # true of them when nothing sits in the same energy band.
        "continue": [c for c in by_sim if same_band(c)] or by_sim,
        "single_axis": [c for c in by_sim if one_axis(c)],
        "contrast": [c for c in by_sim if turns(c)],
        "closing": sorted(
            [c for c in candidates if lands(c)],
            key=lambda c: (c["energy"], -c["absence"], seed_key(seed, c["id"])),
        ),
        # The familiar option, from whoever has been off the air longest: "the one
        # everyone knows" sorted by popularity alone hands over the same single every
        # time, which is the opposite of what the station is for.
        "familiar": sorted(
            [c for c in candidates
             if c["popularity"] >= KNOWN and (not family or c["family"] == family)],
            key=lambda c: (-c["absence"], -c["popularity"], seed_key(seed, c["id"])),
        ),
    }

    # One record per artist means the familiar block has to be drawn first or the
    # directions before it can take every artist and leave it empty.
    wanted = list(targets(station))
    build_order = ([item for item in wanted if item[0] == "familiar"]
                   + [item for item in wanted if item[0] != "familiar"])
    groups = []
    for name, size in build_order:
        if not plans[name]:
            # Nothing honest in this direction: drop it rather than hand over
            # a block whose label is a lie. "Land it" on a track already at the floor
            # of the station's energy is the case that made this necessary.
            continue
        plan = plans[name]
        if name == "familiar" and seen_artists:
            # Familiar, and not the same familiar as last time: an artist offered a set
            # ago has had its turn, so its known records go behind the others. A shorter
            # block is the point; it only falls back when there is not even a choice left.
            unheard = [c for c in plan if c["artist_key"] not in seen_artists]
            plan = unheard if len(unheard) >= min(size, 2) else (
                unheard + [c for c in plan if c["artist_key"] in seen_artists])
        picked = take(plan, size)
        if picked:
            groups.append({"name": name, "label": LABELS[name][0], "note": LABELS[name][1],
                           "tracks": picked})

    _spread_decades(groups, plans, used_ids, used_artists, seed)

    # Display order is the narrative one: directions first, the familiar option last.
    order = {name: i for i, (name, _size) in enumerate(wanted)}
    groups.sort(key=lambda group: order.get(group["name"], 99))

    out = []
    for group in groups:
        if not group["tracks"]:
            continue
        group["tracks"].sort(key=lambda c: seed_key(seed, c["id"]))
        out.append({
            "name": group["name"],
            "label": group["label"],
            "note": group["note"],
            "tracks": [_public(c) for c in group["tracks"]],
        })
    return out


def _spread_decades(groups, plans, used_ids, used_artists, seed) -> None:
    """Two decades in the candidate list when the station can pay for it.

    The replacement comes from the *same direction's* candidates, so the swap keeps
    the label true: a different decade is not a reason to smuggle a dance record into
    "continue the flow".
    """
    decades = {c["decade"] for g in groups for c in g["tracks"] if c["decade"]}
    if len(decades) >= 2:
        return
    for group in sorted(groups, key=lambda g: -len(g["tracks"])):
        if not group["tracks"]:
            continue
        candidates = sorted(plans[group["name"]], key=lambda c: seed_key(seed, c["id"]))
        replaced = spread_decades(
            group["tracks"], candidates,
            key=lambda c: c["id"],
            decade=lambda c: c["decade"],
            decades=decades,
            accept=lambda c: not (c["id"] in used_ids or c["artist_key"] in used_artists),
        )
        if replaced is None:
            continue
        _index, previous, candidate = replaced
        used_ids.discard(previous["id"])
        used_ids.add(candidate["id"])
        used_artists.discard(previous["artist_key"])
        used_artists.add(candidate["artist_key"])
        return


def _popularity_tier(popularity: float, known: bool = True) -> str:
    """The track's standing within its artist's catalogue, always spelled out.

    ``unknown`` means no provider has a count; otherwise the tier is ``low``, ``mid``
    or ``high`` (the familiar end).
    """
    if not known:
        return "unknown"
    if popularity >= KNOWN:
        return "high"
    if popularity <= 0.33:
        return "low"
    return "mid"


def _unmeasured(eligible, used_artists, ranks, limit) -> list[dict]:
    """A few unanalysed records so a new import is not invisible until the worker runs.

    They carry what metadata exists and no sonic numbers: the group's label says why.
    """
    out = []
    seen = set(used_artists)
    ordered = sorted(eligible, key=lambda item: (-(int(item[0]["fetched_at"] or 0)), item[0]["id"]))
    for row, artist, key, vector, feature in ordered:
        track_id = row["id"]
        if vector is not None and feature:
            continue
        if key in seen:
            continue
        seen.add(key)
        year = int(row["year"] or 0)
        out.append({
            "id": track_id, "artist": artist, "title": row["title"] or "",
            "album": row["album"] or "", "year": year or None,
            "duration": row["duration"], "sound": "", "bpm": None, "key": "",
            "energy": None, "measured": False,
            "popularity": float(ranks.get(track_id, 0.5)),
            "popularity_known": track_id in ranks,
        })
        if len(out) >= limit:
            break
    return out


def _public(candidate: dict) -> dict:
    return {
        "id": candidate["id"],
        "measured": True,
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
        "popularity_known": candidate["popularity_known"],
    }


# ------------------------------------------------------------------- the context

def _blocked_artists(lib, station, *, entries: list[dict] | None = None) -> set[str]:
    """Artists that may not be offered: just played, queued, or in the last candidates.

    No sonic distance here — "close to what is on air" is one deliberate direction. The picker's own recent picks are left out, so
    the familiar block stays reachable for a set not anchored to an airing.
    """
    return blocked_artists(lib, station, recent=6, include_picks=False, entries=entries).keys


def _anchor(lib, station, features: dict, vectors: dict, entry: dict) -> dict | None:
    """What the set has to sit next to: the on-air track, or its artist's sound."""
    entry = entry or {}
    track_id = str(entry.get("id") or "")
    vector = vectors.get(track_id)
    feature = features.get(track_id) or {}
    if vector is None and track_id:
        row = lib.by_id(track_id)
        name = (row.album_artist or row.artist) if row is not None else ""
        if name:
            vector, artist_feature = _artist_analysis(lib, name, vectors, features)
            if vector is not None:
                feature = artist_feature or feature
    if vector is None:
        return None
    return {
        "vector": vector,
        "energy": float(feature["energy"]) if feature.get("energy") is not None else None,
        "bpm": float(feature["bpm"]) if feature.get("bpm") else None,
        "key": feature.get("key") or "",
        "family": feature.get("family") or "",
    }


def _artist_analysis(lib, name: str, vectors: dict, features: dict):
    """One artist's mean vector and median-ish feature, from a single id lookup."""
    key = navidrome.name_key(name)
    ids = track_queries.track_ids_for_artist(lib.conn, key)
    parts = [vectors[track_id] for track_id in ids if track_id in vectors]
    rows = [features[track_id] for track_id in ids if track_id in features]
    vector = _mean_vector(parts)
    if not rows:
        return vector, {}
    energies = [r["energy"] for r in rows if r.get("energy") is not None]
    bpms = [r["bpm"] for r in rows if r.get("bpm")]
    families = [r["family"] for r in rows if r.get("family")]
    return vector, {
        "energy": sum(energies) / len(energies) if energies else None,
        "bpm": statistics.median(bpms) if bpms else None,
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


def _percentile(values: list, pct: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, len(ordered) * pct // 100)])


