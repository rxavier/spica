"""The table: records grouped by direction, with ids, and the checks around it.

The tests build a small synthetic station — a dozen artists whose tracks move along
one axis — so the four directions have something to find. Everything here runs
without the analysis extra: vectors and features are written straight into the store.
"""

from __future__ import annotations

import json

import pytest

from spica import analysis, config, mixer, navidrome, queue
from spica.picker import loop, table
from spica.picker.library import Library
from tests.factories import insert_tracks


def vector(x: float, y: float):
    np = pytest.importorskip("numpy")
    values = np.zeros(8, np.float32)
    values[0], values[1] = x, y
    values /= np.linalg.norm(values)
    return values.astype(np.float32).tobytes()


def seed_station(tmp_path, artists: int = 12, per_artist: int = 3):
    conn = navidrome.connect(tmp_path / "index.db")
    rows, genres, pops = [], [], []
    for a in range(artists):
        for j in range(per_artist):
            track_id = f"t{a:02d}{j:02d}" + "x" * 17
            name = f"Artist {a:02d}"
            rows.append((track_id, f"http://navidrome/{track_id}.flac", f"Song {a}-{j}",
                         name, f"Album {a}", f"al{a}", j + 1, 1970 + a * 3, "",
                         200, name, navidrome.name_key(name)))
            genres.append((track_id, f"Label {a % 4}", "tag"))
            pops.append((track_id, (j + 1) * 100 + a, 10, "listenbrainz", 0))
    with conn:
        insert_tracks(conn, rows)
        conn.executemany(
            "INSERT OR REPLACE INTO track_genres (track_id, label, source) VALUES (?,?,?)",
            genres,
        )
        conn.executemany(
            "INSERT OR REPLACE INTO track_popularity"
            " (track_id, playcount, users, source, fetched_at) VALUES (?,?,?,?,?)",
            pops,
        )
    return conn, rows


def seed_analysis(store, artists: int = 12, per_artist: int = 3, first: int = 0):
    """Artist i sits on a gradient: later artists are faster, louder and electronic."""
    for a in range(first, artists):
        for j in range(per_artist):
            track_id = f"t{a:02d}{j:02d}" + "x" * 17
            family = "Rock" if a < artists // 2 else "Electronic"
            style = f"{family}---{'Prog Rock' if a < artists // 2 else 'House'}"
            with store:
                store.execute(
                    "INSERT OR REPLACE INTO track_analysis (track_id, artist_key, duration,"
                    " bpm, bpm_confidence, key, key_scale, energy, styles, heads, vocal,"
                    " vec, model_version, analyzed_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (track_id, navidrome.name_key(f"Artist {a:02d}"), 200.0,
                     100 + a * 5 + j, 1.0, ["C", "D", "E", "F"][a % 4], "major",
                     0.15 + a * 0.06, json.dumps([[style, 0.6], [f"{family}---Other", 0.2]]),
                     "{}", 0, vector(1.0 + j * 0.05, a * 0.12),
                     analysis.MODEL_VERSION, 0),
                )


@pytest.fixture
def rig(station, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ANALYSIS", True)
    monkeypatch.setattr(analysis, "installed", lambda: True)
    conn, rows = seed_station(tmp_path)
    store = analysis.connect(path=tmp_path / "analysis.db", create=True)
    seed_analysis(store)
    # A station always has something on air; the fixture's on-air track is analysed.
    monkeypatch.setattr(queue, "recent",
                        lambda st, n=20: [{"id": rows[0][0], "artist": rows[0][3]}])
    monkeypatch.setattr(queue, "recent_ids", lambda st, n=40: {rows[0][0]})
    monkeypatch.setattr(queue, "last_shelf", lambda st: [])
    monkeypatch.setattr(mixer, "queued_ids", lambda st=None: [])
    return Library(conn), store, rows


def test_the_table_has_four_directions_and_no_repeats(rig):
    lib, _store, rows = rig
    built = table.build(lib, config.Station(id="t", name="T"))
    assert built is not None
    assert len(built["ids"]) == len(set(built["ids"]))
    assert len(built["ids"]) >= table.MIN_TABLE
    station = config.Station(id="t", name="T")
    assert len(built["ids"]) <= sum(size for _name, size in table.targets(station))
    artists = [track["artist"] for group in built["groups"] for track in group["tracks"]]
    assert len(artists) == len(set(artists)), "one record per artist on the table"
    assert {group["name"] for group in built["groups"]} <= {n for n, _ in table.targets(station)}
    known = {row[0] for row in rows}
    assert set(built["ids"]) <= known
    rendered = table.render(built)
    for track_id in built["ids"]:
        assert track_id in rendered


def test_played_and_queued_are_not_offered(rig):
    lib, _store, rows = rig
    played = rows[0][0]           # on air
    queued = rows[3][0]
    lib.by_id = lambda track_id: None  # artist fallback must not invent one
    queue.recent = lambda st, n=20: [{"id": played, "artist": "Artist 00"}]
    queue.recent_ids = lambda st, n=40: {played}
    queue.last_shelf = lambda st: ["Artist 05"]
    mixer.queued_ids = lambda st=None: [queued]
    built = table.build(lib, config.Station(id="t", name="T"))
    assert built is not None
    assert played not in built["ids"]
    assert queued not in built["ids"]
    assert "Artist 00" not in built["artists"]
    assert "Artist 05" not in built["artists"]


def test_too_little_analysis_falls_back(station, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ANALYSIS", True)
    monkeypatch.setattr(analysis, "installed", lambda: True)
    conn, _rows = seed_station(tmp_path, artists=2, per_artist=2)
    store = analysis.connect(path=tmp_path / "analysis.db", create=True)
    seed_analysis(store, artists=2, per_artist=2)
    # A station always has something on air; the fixture's on-air track is analysed.
    monkeypatch.setattr(queue, "recent",
                        lambda st, n=20: [{"id": _rows[0][0], "artist": _rows[0][3]}])
    monkeypatch.setattr(queue, "recent_ids", lambda st, n=40: {_rows[0][0]})
    monkeypatch.setattr(queue, "last_shelf", lambda st: [])
    monkeypatch.setattr(mixer, "queued_ids", lambda st=None: [])
    assert table.build(Library(conn), config.Station(id="t", name="T")) is None


def test_a_direction_with_nothing_to_offer_is_dropped(rig):
    lib, store, rows = rig
    # Every track is hot and the anchor is hottest: there is nothing calmer to land
    # on, so "land" must disappear rather than become a relabelled random block.
    with store:
        # Everything is hot and moves on tempo only: "land" has nothing below the
        # anchor, while "continue"/"move one axis" still have candidates.
        store.execute("UPDATE track_analysis SET energy = 0.9, bpm = 112")
        store.execute("UPDATE track_analysis SET energy = 1.0 WHERE track_id = ?", (rows[0][0],))
    queue.recent = lambda st, n=20: [{"id": rows[0][0], "artist": rows[0][3]}]
    built = table.build(lib, config.Station(id="t", name="T"))
    assert built is not None
    assert "land" not in {group["name"] for group in built["groups"]}


def test_the_familiar_block_follows_the_station_lean(rig):
    lib, _store, _rows = rig

    def known(station):
        built = table.build(lib, station)
        return len(next((g["tracks"] for g in built["groups"] if g["name"] == "known"), []))

    assert known(config.Station(id="h", name="H", popularity="hits")) > \
        known(config.Station(id="d", name="D", popularity="deep"))


def test_a_pick_stays_out_of_the_table_when_nobody_was_listening(rig, monkeypatch):
    """Forty airings is three hours, and half of them are the standby pool.

    The DJ's own choices are the memory that covers a quiet night: without them a
    station that sat idle since the morning can open with the set it played then.
    """
    lib, _store, _rows = rig
    baseline = table.build(lib, config.Station(id="t", name="T"))
    assert baseline is not None
    victim = baseline["ids"][0]
    # Nobody listened: the ledger holds nothing the DJ chose.
    monkeypatch.setattr(queue, "recent_ids", lambda st, n=40: set())
    monkeypatch.setattr(queue, "recent_pick_ids", lambda st, n=60: {victim})
    built = table.build(lib, config.Station(id="t", name="T"))
    assert built is not None
    assert victim not in built["ids"]


def test_a_record_offered_a_set_ago_is_not_offered_again(rig, monkeypatch):
    """Being offered is having had a turn, played or not.

    A record only left the table by airing, so the familiar block handed over the same
    records it kept refusing — more than once in five tables, and never picked.
    """
    lib, _store, _rows = rig
    baseline = table.build(lib, config.Station(id="t", name="T"))
    assert baseline is not None
    victim = baseline["ids"][0]
    monkeypatch.setattr(queue, "offered_ids", lambda st, sets=4: {victim}, raising=False)
    built = table.build(lib, config.Station(id="t", name="T"))
    assert built is not None
    assert victim not in built["ids"]


def test_the_offer_memory_gives_way_before_the_table_does(rig, monkeypatch):
    """On a station narrower than four tables' worth, a repeated offer beats no offer."""
    lib, _store, rows = rig
    monkeypatch.setattr(queue, "offered_ids", lambda st, sets=4: {row[0] for row in rows},
                        raising=False)
    built = table.build(lib, config.Station(id="t", name="T"))
    assert built is not None
    assert len(built["ids"]) >= table.MIN_TABLE


def test_the_familiar_block_comes_from_whoever_was_off_the_air_longest(rig, monkeypatch):
    """The single is the same single every time; the one you have not heard in ages is not.

    Popularity ranks the familiar block on its own, so the song it offers is the one the
    artist is known for — and it offers it again the next time. Absence comes first now:
    between two familiar records, the one that has not aired wins even when it is the
    less famous of the two.
    """
    lib, _store, rows = rig
    just_played, away = rows[3][0], rows[6][0]   # Artist 01 and Artist 02, both Rock
    monkeypatch.setattr(
        lib, "popularity_ranks",
        lambda candidates: {
            row[0]: (1.0 if row[0] == just_played else 0.7 if row[0] == away else 0.5)
            for row in rows
        },
    )
    monkeypatch.setattr(
        table, "_absence",
        lambda lib, station, look=240: {
            navidrome.name_key(f"Artist {a:02d}"): (200 if a == 2 else 1 if a == 1 else 60)
            for a in range(12)
        },
    )
    monkeypatch.setattr(table, "targets", lambda station: (("known", 1),))
    monkeypatch.setattr(table, "MIN_TABLE", 1)
    monkeypatch.setattr(table, "_spread_decades", lambda *a, **k: None)
    built = table.build(lib, config.Station(id="t", name="T"))
    assert built is not None
    assert built["ids"] == [away]


def _fake_candidate(index: int, **over) -> dict:
    item = {
        "id": str(index) * 22, "artist": f"A{index}", "artist_key": f"a{index}",
        "title": f"t{index}", "album": "al", "year": 2000, "duration": 200,
        "label": f"L{index}", "decade": 2000, "energy": 0.5, "bpm": 120.0, "key": "C maj",
        "sound": "Rock", "family": "Rock", "popularity": 1.0, "popularity_known": True,
        "absence": 50 - index, "sim": 0.5, "vector": None,
    }
    item.update(over)
    return item


def test_the_familiar_block_drops_an_artist_offered_a_set_ago():
    """Ids alone rotate; the artist is what repeats, so the artist is what is remembered."""
    candidates = [_fake_candidate(i) for i in range(6)]
    anchor = {"energy": 0.5, "bpm": 120.0, "key": "C maj", "family": "Rock"}
    groups = table._groups(candidates, anchor, 0, config.Station(id="t", name="T"),
                           offered_artists={"a0", "a1", "a2"})
    known = next(g for g in groups if g["name"] == "known")
    assert {track["artist"] for track in known["tracks"]} == {"A3", "A4", "A5"}


def test_the_familiar_block_comes_back_when_there_is_no_choice_left():
    """Every familiar artist was offered last set: a block of six beats no block at all."""
    candidates = [_fake_candidate(i) for i in range(6)]
    anchor = {"energy": 0.5, "bpm": 120.0, "key": "C maj", "family": "Rock"}
    groups = table._groups(candidates, anchor, 0, config.Station(id="t", name="T"),
                           offered_artists={f"a{i}" for i in range(6)})
    known = next(g for g in groups if g["name"] == "known")
    assert len(known["tracks"]) == 6


def test_absence_moves_a_record_up_its_direction():
    just_played = {"sim": 0.90, "absence": 2, "popularity": 0.5}
    off_the_air = {"sim": 0.88, "absence": 200, "popularity": 0.5}
    stranger = {"sim": 0.60, "absence": 200, "popularity": 0.5}
    assert table._offer_rank(off_the_air, 0.0) > table._offer_rank(just_played, 0.0)
    # A bonus, not a new axis: it lifts neighbours, it does not smuggle in a stranger.
    assert table._offer_rank(stranger, 0.0) < table._offer_rank(just_played, 0.0)


def test_familiar_records_stay_in_the_on_air_family(rig):
    lib, store, rows = rig
    built = table.build(lib, config.Station(id="t", name="T"))
    features = analysis.feature_map(store)
    group = next(g for g in built["groups"] if g["name"] == "known")
    families = {features[t["id"]]["family"] for t in group["tracks"]}
    assert families == {"Rock"}  # the fixture's anchor is Rock; the Electronic half stays out


def test_continue_needs_tempo_and_key_not_just_energy():
    anchor_bpm, anchor_key = 138.0, table._camelot("C maj")
    same = {"energy": 0.41, "bpm": 130.0, "key": "G maj"}
    jump = {"energy": 0.40, "bpm": 112.0, "key": "Eb min"}
    assert table._same_temperature(same, 0.41, anchor_bpm, anchor_key)
    assert not table._same_temperature(jump, 0.41, anchor_bpm, anchor_key)


def test_prominence_is_always_spelled_out(rig):
    assert table._prominence(0.9) == "known"
    assert table._prominence(0.5) == "mid"
    assert table._prominence(0.1) == "deep cut"
    assert table._prominence(0.5, False) == "unknown"
    lib, _store, _rows = rig
    built = table.build(lib, config.Station(id="t", name="T"))
    for line in table.render(built).splitlines():
        if line.startswith("- "):
            tag = line.rsplit(" · ", 1)[-1]
            assert tag in {"known", "mid", "deep cut", "unknown"}, line


def test_unlabelled_tracks_do_not_cap_the_table(rig, monkeypatch):
    lib, _store, _rows = rig
    monkeypatch.setattr(navidrome, "labels_by_track", lambda conn: {})
    built = table.build(lib, config.Station(id="t", name="T"))
    assert built is not None
    assert len(built["ids"]) >= table.MIN_TABLE


def test_a_partially_analysed_station_adds_a_not_measured_group(rig):
    lib, store, rows = rig
    with store:
        store.executemany("DELETE FROM track_analysis WHERE track_id = ?",
                          [(row[0],) for row in rows[-4:]])
    built = table.build(lib, config.Station(id="t", name="T"))
    assert built is not None and built["partial"] is True
    group = next(g for g in built["groups"] if g["name"] == "unmeasured")
    assert 1 <= len(group["tracks"]) <= table.UNMEASURED
    assert all(t["measured"] is False and t["energy"] is None for t in group["tracks"])
    assert "not measured" in table.render(built)


def test_a_ready_station_has_no_not_measured_group(rig):
    lib, store, rows = rig
    with store:
        store.executemany("DELETE FROM track_analysis WHERE track_id = ?",
                          [(row[0],) for row in rows[-2:]])
    built = table.build(lib, config.Station(id="t", name="T"))
    assert built is not None and built["partial"] is False
    assert "unmeasured" not in {g["name"] for g in built["groups"]}


def test_no_anchor_means_the_shelf_instead_of_medians(rig):
    lib, store, rows = rig
    with store:  # the whole on-air artist is unanalysed
        store.executemany("DELETE FROM track_analysis WHERE track_id = ?",
                          [(row[0],) for row in rows[:3]])
    queue.recent = lambda st, n=20: [{"id": rows[0][0], "artist": rows[0][3]}]
    assert table.build(lib, config.Station(id="t", name="T")) is None


def test_analysis_off_means_no_table(station, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ANALYSIS", False)
    conn, _rows = seed_station(tmp_path)
    assert table.build(Library(conn), config.Station(id="t", name="T")) is None


# --------------------------------------------------------------- the reply check

class FakeTrack:
    def __init__(self, track_id, artist):
        self.id = track_id
        self.uri = f"http://navidrome/{track_id}.flac"
        self.artist = artist
        self.album_artist = artist


class FakeLibrary:
    def __init__(self, tracks):
        self.tracks = {t.id: t for t in tracks}

    def by_id(self, track_id):
        return self.tracks.get(track_id)

    def by_ids(self, track_ids):
        return {track_id: self.tracks[track_id] for track_id in track_ids if track_id in self.tracks}

    def artists_by_ids(self, track_ids):
        return {
            track_id: (self.tracks[track_id].album_artist or self.tracks[track_id].artist)
            for track_id in track_ids if track_id in self.tracks
        }

    def on_station(self, station, track_id):
        return track_id in self.tracks


def test_parse_set_from_the_table_only_takes_offered_ids():
    a, b, c, d, e = "a" * 22, "b" * 22, "c" * 22, "d" * 22, "e" * 22
    lib = FakeLibrary([FakeTrack(a, "One"), FakeTrack(b, "One"),
                       FakeTrack(c, "Three"), FakeTrack(d, "Four"),
                       FakeTrack(e, "Five")])
    station = config.Station(id="t", name="T")
    reply = json.dumps({"theme": "x", "ids": [a, b, c, d, e]})
    theme, ids = loop.parse_set(lib, reply, set(), station,
                                allowed_ids={a, b, c, d})  # e is not on the table
    assert ids == [a, c, d]  # b is a second track by One
    assert theme == "x"
