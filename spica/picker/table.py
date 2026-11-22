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
# still fit cheaply in one message (~3.5k characters, about 900 tokens). The split
# depends on the station's lean: a "hits" station gets a bigger familiar block, a
# "deep" station a smaller one.
TARGETS = (("continue", 7), ("shift", 6), ("contrast", 6), ("land", 6), ("known", 7))
MIN_TABLE = 8
PER_LABEL = 3  # one label cannot own the table
ABSENCE = 40   # plays that count as "off the air"
KNOWN = 0.66   # within-artist percentile that counts as "the one everyone knows"
# A record *offered* this many sets ago has had its turn whether or not it played: the
# play ledger alone let the familiar block hand over the same records it kept refusing.
# The window itself — sixty picks or eight hours of air — lives in `queue`, because the
# DJ's tool rows report it too.
OFFER_SETS = 4
# The familiar block is one record per artist, so it needs the artist's turn remembered
# rather than the record's: two sets is enough to stop the same name coming back, and
# longer starves it — there are only so many artists with a known record in one family.
KNOWN_SETS = 2
# How much "not been on in ages" can move a candidate up its direction. Similarity
# alone is what made the station orbit one sound: `-absence` sat after `-sim` in the
# sort key, so it only ever broke an exact tie.
ABSENCE_WEIGHT = 0.12
# Readiness for the full table, per station. Below this the table still runs, but with
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
    "shift": ("Move one axis", "one thing changes — tempo, weight or colour — and the rest holds"),
    "contrast": ("Change it up", "a different family of sound; a deliberate turn"),
    "land": ("Land it", "calmer and slower than what is on air; a closer"),
    "known": ("The one everyone knows",
              "the familiar song, in the same family as what is on air"),
    "unmeasured": ("Not measured yet", "no sonic data yet; offered so it is not invisible"),
}


def targets(station) -> tuple[tuple[str, int], ...]:
    """The same thirty-two slots, weighted by how the station leans.

    `popularity` on a station was only ever about the pool order; with the table it
    decides how much of the offer is familiar. A hits station gets seven known
    records, a deep one four, balanced six.
    """
    known = 7 if station.deep_bias < 0 else (4 if station.deep_bias > 0 else 6)
    rest = 32 - known
    base, extra = divmod(rest, 4)
    names = ("continue", "shift", "contrast", "land")
    return tuple(
        [(name, base + (1 if i < extra else 0)) for i, name in enumerate(names)]
        + [("known", known)]
    )


def build(lib, station) -> dict | None:
    """The table for one set, or None when analysis is off or too thin to draw one."""
    if not analysis.enabled():
        return None
    rows = lib._station_rows(station)
    vectors = lib.track_vectors()
    features = lib._features()
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

    blocked = _blocked_artists(lib, station)
    played = queue.recently_out(station)
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

    candidates = _candidates(rows, vectors, features, played, queued, on_air,
                             blocked, labels, ranks, absence)
    if len(candidates) < MIN_TABLE:
        return None
    # A record the last few sets offered has had its turn, played or not — that is what
    # stops the familiar block from becoming a fixed cast. On a small station the rule
    # could empty the table, and a smaller offer is worse than a repeated one.
    offered = queue.offered_ids(station, OFFER_SETS)
    fresh = [c for c in candidates if c["id"] not in offered]
    if len(fresh) >= MIN_TABLE:
        candidates = fresh

    anchor = _anchor(lib, station, features, vectors)
    if anchor is None or anchor.get("energy") is None:
        # Without a real anchor the directions are guesses against the station median;
        # the artist shelf is a better offer than that.
        return None
    for candidate in candidates:
        candidate["sim"] = float(candidate["vector"] @ anchor["vector"])

    groups = _groups(candidates, anchor, getattr(lib, "draw_seed", 0) or 0, station,
                     offered_artists=_offered_artists(
                         lib, queue.offered_ids(station, KNOWN_SETS)))
    ids = [track["id"] for group in groups for track in group["tracks"]]
    if len(ids) < MIN_TABLE:
        return None
    artists = []
    for group in groups:
        for track in group["tracks"]:
            if track["artist"] not in artists:
                artists.append(track["artist"])

    if partial:
        unknown = _unmeasured(rows, vectors, features, played, queued, on_air,
                              blocked, set(navidrome.name_key(a) for a in artists),
                              ranks, labels, UNMEASURED)
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
            if track.get("measured", True):
                parts = (
                    str(track["year"] or ""),
                    track["sound"],
                    analysis.tempo_label(track["bpm"], track["energy"]),
                    track["key"],
                    f"energy {track['energy']:.2f}" if track["energy"] is not None else "",
                    _prominence(track["popularity"], track["popularity_known"]),
                )
            else:
                parts = (str(track["year"] or ""), "not measured",
                         _prominence(track["popularity"], track["popularity_known"]))
            detail = " · ".join(part for part in parts if part)
            lines.append(
                f"- {track['id']} · {track['artist']} — {track['title']}"
                + (f" · {detail}" if detail else "")
            )
    return "\n".join(lines)


# ------------------------------------------------------------------ the directions

def _candidates(rows, vectors, features, played, queued, on_air, blocked,
                labels, ranks, absence) -> list[dict]:
    """Every record a set may be built from, with what it sounds like and how known."""
    out = []
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


def _offered_artists(lib, offered: set[str]) -> set[str]:
    """The artists whose records the last sets already offered, by name key.

    Ids alone are not enough: an artist with several known records keeps a seat in the
    familiar block by serving a different one each time, which is why the same five names
    came back table after table while every individual record was rotated out.
    """
    out: set[str] = set()
    for track_id in offered:
        track = lib.by_id(track_id)
        name = (getattr(track, "album_artist", "") or getattr(track, "artist", "")) if track else ""
        if name:
            out.add(navidrome.name_key(name))
    return out


def _absent(candidate: dict) -> float:
    """How long since this artist aired, 0 = just now, 1 = longer than the window."""
    return min(int(candidate["absence"]), ABSENCE) / ABSENCE


def _offer_rank(candidate: dict, lean: float) -> float:
    """Where a candidate sits in its direction: what it sounds like, and when it aired.

    Similarity alone is why the station orbited one corner of the library. A record that
    aired three songs ago beat one that had not been on all day by a thousandth of a
    cosine, because `-absence` came after `-sim` in the sort key as a tiebreak that never
    fired. Absence is a term of its own now: a bonus, capped, so it lifts a record that
    has not had its turn without overruling "this is the same temperature".
    """
    return (float(candidate["sim"])
            + ABSENCE_WEIGHT * _absent(candidate)
            + 0.10 * lean * float(candidate["popularity"]))


def _groups(candidates: list[dict], anchor: dict, seed: int, station,
            offered_artists: set[str] | None = None) -> list[dict]:
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

    def turns(c):
        if family:
            return c["family"] and c["family"] != family and abs(c["energy"] - energy) >= 0.12
        return c["sim"] <= _percentile([x["sim"] for x in candidates], 25)

    def lands(c):
        if not c["bpm"] or not bpm:
            return c["energy"] <= energy - 0.15
        return c["energy"] <= energy - 0.15 and c["bpm"] <= bpm + 5

    # How strongly to favour the familiar end of the catalogue, from the station's
    # own lean: +1 for hits, 0 balanced, -1 for deep.
    lean = -station.deep_bias
    by_sim = sorted(
        candidates,
        key=lambda c: (-_offer_rank(c, lean), _seed_key(seed, c["id"])),
    )

    plans = {
        # Continue may fall back to the nearest records: "closest to on air" is still
        # true of them when nothing sits in the same energy band.
        "continue": [c for c in by_sim if same_band(c)] or by_sim,
        "shift": [c for c in by_sim if one_axis(c)],
        "contrast": [c for c in by_sim if turns(c)],
        "land": sorted(
            [c for c in candidates if lands(c)],
            key=lambda c: (c["energy"], -c["absence"], _seed_key(seed, c["id"])),
        ),
        # The familiar option, from whoever has been off the air longest: "the one
        # everyone knows" sorted by popularity alone hands over the same single every
        # time, which is the opposite of what the station is for.
        "known": sorted(
            [c for c in candidates
             if c["popularity"] >= KNOWN and (not family or c["family"] == family)],
            key=lambda c: (-c["absence"], -c["popularity"], _seed_key(seed, c["id"])),
        ),
    }

    # One record per artist means the familiar block has to be drawn first or the
    # directions before it can take every artist and leave it empty.
    wanted = list(targets(station))
    build_order = ([item for item in wanted if item[0] == "known"]
                   + [item for item in wanted if item[0] != "known"])
    groups = []
    for name, size in build_order:
        if not plans[name]:
            # Nothing honest to offer in this direction: drop it rather than hand over
            # a block whose label is a lie. "Land it" on a track already at the floor
            # of the station's energy is the case that made this necessary.
            continue
        plan = plans[name]
        if name == "known" and offered_artists:
            # Familiar, and not the same familiar as last time: an artist offered a set
            # ago has had its turn, so its known records go behind the others. A shorter
            # block is the point; it only falls back when there is not even a choice left.
            unheard = [c for c in plan if c["artist_key"] not in offered_artists]
            plan = unheard if len(unheard) >= min(size, 2) else (
                unheard + [c for c in plan if c["artist_key"] in offered_artists])
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
        group["tracks"].sort(key=lambda c: _seed_key(seed, c["id"]))
        out.append({
            "name": group["name"],
            "label": group["label"],
            "note": group["note"],
            "tracks": [_public(c) for c in group["tracks"]],
        })
    return out


def _spread_decades(groups, plans, used_ids, used_artists, seed) -> None:
    """Two decades on the table when the station can pay for it.

    The replacement comes from the *same direction's* candidates, so the swap keeps
    the label true: a different decade is not a reason to smuggle a dance record into
    "continue the flow".
    """
    decades = {c["decade"] for g in groups for c in g["tracks"] if c["decade"]}
    if len(decades) >= 2:
        return
    for group in sorted(groups, key=lambda g: -len(g["tracks"])):
        for candidate in sorted(plans[group["name"]], key=lambda c: _seed_key(seed, c["id"])):
            if (candidate["id"] in used_ids or candidate["artist_key"] in used_artists
                    or not candidate["decade"] or candidate["decade"] in decades):
                continue
            if not group["tracks"]:
                continue
            dropped = group["tracks"][-1]
            group["tracks"][-1] = candidate
            used_ids.discard(dropped["id"])
            used_ids.add(candidate["id"])
            used_artists.discard(dropped["artist_key"])
            used_artists.add(candidate["artist_key"])
            return


def _prominence(popularity: float, known: bool = True) -> str:
    """How known the track is within its artist's catalogue — always, explicitly.

    An empty tag was ambiguous: the model could read it as mid-catalogue, unknown, or
    "not applicable", and it had no legend either way. Every row now says which of the
    four it is.
    """
    if not known:
        return "unknown"
    if popularity >= KNOWN:
        return "known"
    if popularity <= 0.33:
        return "deep cut"
    return "mid"


def _unmeasured(rows, vectors, features, played, queued, on_air, blocked,
                used_artists, ranks, labels, limit) -> list[dict]:
    """A few unanalysed records so a new import is not invisible until the worker runs.

    They carry what metadata exists and no sonic numbers: the group's label says why.
    """
    out = []
    seen = set(used_artists)
    ordered = sorted(rows, key=lambda r: (-(int(r["fetched_at"] or 0)), r["id"]))
    for row in ordered:
        track_id = row["id"]
        if track_id in played or track_id in queued or track_id == on_air:
            continue
        if track_id in vectors and track_id in features:
            continue
        artist = (row["album_artist"] or row["artist"] or "").strip()
        key = navidrome.name_key(artist)
        if not key or key in blocked or key in seen:
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
        "key": feature.get("key") or "",
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
