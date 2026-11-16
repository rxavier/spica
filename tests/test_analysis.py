"""The optional analysis: off means off, and the store's shape stays honest.

These tests never import the analysis extra: everything below is the plumbing the
picker and the worker touch, which must work (and do nothing) without numpy, ONNX or
essentia installed. The mel/ONNX path is validated separately against essentia's own
TensorFlow wrapper.
"""

from __future__ import annotations

import json

import pytest

from spica import analysis, config, navidrome
from spica.picker.library import Library


def track(track_id: str, artist: str, uri: str | None = None) -> tuple:
    return (
        track_id, uri if uri is not None else f"http://navidrome/{track_id}.flac",
        f"title {track_id}", artist, "Record", "al1", 1, 2000, "", 200,
        artist, navidrome.name_key(artist),
    )


def seed(path, rows):
    conn = navidrome.connect(path)
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO tracks (id, uri, title, artist, album, album_id,"
            " track_no, year, genre, duration, album_artist, album_artist_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return conn


def insert(store, track_id: str, *, version: str = analysis.MODEL_VERSION,
           bpm=118.0, key="A", scale="minor", energy=0.81,
           styles=(("Nu-Disco", 0.4), ("Funk", 0.3))):
    with store:
        store.execute(
            "INSERT OR REPLACE INTO track_analysis (track_id, artist_key, duration, bpm,"
            " key, key_scale, energy, styles, heads, vocal, vec, model_version, analyzed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (track_id, "", 200.0, bpm, key, scale, energy, json.dumps(list(styles)),
             "{}", 0, b"", version, 0),
        )


# ------------------------------------------------------------------ off is off

def test_disabled_means_no_store_and_no_coverage(station):
    assert analysis.enabled() is False
    assert analysis.connect(readonly=True) is None
    assert analysis.feature_map(None) == {}
    assert analysis.coverage_for(None) is None
    assert analysis.shelf_sound(None, ["Band"]) == ""


def test_library_behaves_as_before_without_analysis(station, tmp_path):
    conn = seed(tmp_path / "index.db", [track("a", "Band A")])
    lib = Library(conn)
    assert lib.analysis_store() is None
    assert lib._features() == {}
    assert lib.sound_line(["Band A"]) == ""
    assert lib._audio_artists("Band A", k=3) == []


# ------------------------------------------------------------------- the store

def test_store_roundtrip_and_todo(station, tmp_path):
    index = seed(tmp_path / "index.db", [
        track("a", "Band A"), track("b", "Band B"), track("c", "Band C"),
        track("d", "Band D", uri=""),
    ])
    store = analysis.connect(path=tmp_path / "analysis.db", create=True)
    insert(store, "a")
    insert(store, "b", version="older-vector")  # a stale row must not count

    features = analysis.feature_map(store)
    assert set(features) == {"a"}
    assert features["a"]["key"] == "A min"
    assert features["a"]["sound"] == "Nu-Disco / Funk"
    assert features["a"]["energy"] == 0.81

    todo = analysis.todo(index, store, 10)
    assert {row["id"] for row in todo} == {"b", "c"}  # 'd' has no URI
    assert analysis.backlog(index, store) == 2
    assert analysis.backlog(index, None) == 3  # no store yet: everything with a URI

    assert analysis.coverage(index, store)["known"] == 1
    assert analysis.coverage(index, store)["total"] == 4


def test_labels_survive_odd_values():
    assert analysis.key_label("A", "minor") == "A min"
    assert analysis.key_label("F#", "major") == "F# maj"
    assert analysis.key_label("", "minor") == ""
    assert analysis.sound_label([["Downtempo", 0.2], ["Ambient", 0.1]]) == "Downtempo / Ambient"
    assert analysis.sound_label([]) == ""


# ------------------------------------------------------------------- neighbours

def test_audio_neighbours_are_ranked_and_relative(station, tmp_path):
    np = pytest.importorskip("numpy")

    def blob(values):
        return analysis.unit(np.asarray(values, np.float32)).astype(np.float32).tobytes()

    store = analysis.connect(path=tmp_path / "analysis.db", create=True)
    with store:
        store.executemany(
            "INSERT INTO artist_analysis (artist_key, name, vec, tracks, styles)"
            " VALUES (?,?,?,?,?)",
            [
                ("anchor", "Anchor", blob([1.0, 0.0, 0.0]), 3, "[]"),
                ("close", "Close", blob([0.98, 0.19, 0.0]), 3, "[]"),
                ("mid", "Mid", blob([0.70, 0.70, 0.0]), 3, "[]"),
                ("far", "Far", blob([0.0, 1.0, 0.0]), 3, "[]"),
            ],
        )
    wanted = {"anchor", "close", "mid", "far"}
    blocked = analysis.audio_neighbours(None, store, wanted, ["Anchor"], k=3)
    assert blocked == {"close"}  # relative to the anchor's own row, not an absolute cut
    assert analysis.audio_neighbours(None, store, wanted, ["Nobody"], k=3) == set()
    assert analysis.audio_neighbours(None, None, wanted, ["Anchor"]) == set()


def test_shelf_sound_line(station, tmp_path):
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(config, "ANALYSIS", True)
    monkeypatch.setattr(analysis, "installed", lambda: True)
    try:
        np = pytest.importorskip("numpy")
        store = analysis.connect(path=tmp_path / "analysis.db", create=True)
        with store:
            store.execute(
                "INSERT INTO artist_analysis (artist_key, name, vec, tracks, bpm, energy,"
                " styles) VALUES (?,?,?,?,?,?,?)",
                ("band a", "Band A", np.ones(4, np.float32).tobytes(), 3, 118.0, 0.8,
                 json.dumps([["Nu-Disco", 0.4], ["Funk", 0.3], ["Disco", 0.1]])),
            )
        line = analysis.shelf_sound(store, ["Band A", "Unknown Band"])
        assert "Band A — Nu-Disco / Funk, 118 bpm, high energy" in line
        assert "Unknown Band" not in line
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------- row payloads

def test_rows_gain_fields_only_when_the_track_was_analysed(station, tmp_path):
    conn = seed(tmp_path / "index.db", [track("a", "Band A")])
    lib = Library(conn)
    row = conn.execute("SELECT * FROM tracks WHERE id = 'a'").fetchone()
    base = {"labels": {}, "ranks": {}, "played": set(), "queued": set(), "analysis": {}}

    plain = lib._row(row, base)
    assert not {"tempo", "key", "sound", "energy"} & set(plain)

    heard = {
        "labels": {}, "ranks": {}, "played": set(), "queued": set(),
        "analysis": {"a": {"bpm": 118.4, "key": "A min", "sound": "Funk", "energy": 0.814}},
    }
    out = lib._row(row, heard)
    assert out["tempo"] == 118
    assert out["key"] == "A min"
    assert out["sound"] == "Funk"
    assert out["energy"] == 0.81
