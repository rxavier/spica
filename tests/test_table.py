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


def vector(x: float, y: float):
    np = pytest.importorskip("numpy")
    values = np.zeros(8, np.float32)
    values[0], values[1] = x, y
    values /= np.linalg.norm(values)
    return values.astype(np.float32).tobytes()


def seed_station(tmp_path, artists: int = 12, per_artist: int = 3):
    conn = navidrome.connect(tmp_path / "index.db")
    rows, genres = [], []
    for a in range(artists):
        for j in range(per_artist):
            track_id = f"t{a:02d}{j:02d}" + "x" * 17
            name = f"Artist {a:02d}"
            rows.append((track_id, f"http://navidrome/{track_id}.flac", f"Song {a}-{j}",
                         name, f"Album {a}", f"al{a}", j + 1, 1970 + a * 3, "",
                         200, name, navidrome.name_key(name)))
            genres.append((track_id, f"Label {a % 4}", "tag"))
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO tracks (id, uri, title, artist, album, album_id,"
            " track_no, year, genre, duration, album_artist, album_artist_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows,
        )
        conn.executemany(
            "INSERT OR REPLACE INTO track_genres (track_id, label, source) VALUES (?,?,?)",
            genres,
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
    monkeypatch.setattr(queue, "recent", lambda st, n=20: [])
    monkeypatch.setattr(queue, "recent_ids", lambda st, n=40: set())
    monkeypatch.setattr(queue, "last_shelf", lambda st: [])
    monkeypatch.setattr(mixer, "queued_ids", lambda st=None: [])
    return Library(conn), store, rows


def test_the_table_has_four_directions_and_no_repeats(rig):
    lib, _store, rows = rig
    built = table.build(lib, config.Station(id="t", name="T"))
    assert built is not None
    assert len(built["ids"]) == len(set(built["ids"]))
    assert len(built["ids"]) >= table.MIN_TABLE
    assert len(built["ids"]) <= sum(size for _name, size in table.TARGETS)
    artists = [track["artist"] for group in built["groups"] for track in group["tracks"]]
    assert len(artists) == len(set(artists)), "one record per artist on the table"
    assert {group["name"] for group in built["groups"]} <= {n for n, _ in table.TARGETS}
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
    monkeypatch.setattr(queue, "recent", lambda st, n=20: [])
    monkeypatch.setattr(queue, "recent_ids", lambda st, n=40: set())
    monkeypatch.setattr(queue, "last_shelf", lambda st: [])
    monkeypatch.setattr(mixer, "queued_ids", lambda st=None: [])
    assert table.build(Library(conn), config.Station(id="t", name="T")) is None


def test_unlabelled_tracks_do_not_cap_the_table(rig, monkeypatch):
    lib, _store, _rows = rig
    monkeypatch.setattr(navidrome, "labels_by_track", lambda conn: {})
    built = table.build(lib, config.Station(id="t", name="T"))
    assert built is not None
    assert len(built["ids"]) >= table.MIN_TABLE


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
