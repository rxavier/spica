"""The genre console and the job endpoints.

These write to the index and to the alias file, so every test seeds a small database
and then checks what actually landed on disk — not what the page said it did.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from spica import app as application
from spica import config, genres, icecast, mixer, navidrome, pool, store

PASSWORD = "let-me-in"

STATIONS = {
    "stations": [
        {"id": "alt", "name": "Alternative", "description": "", "include_genres": ["Rock"]},
    ]
}


@pytest.fixture
def console(monkeypatch, tmp_path):
    cfg = tmp_path / "config"
    state = tmp_path / "state"
    cfg.mkdir()
    state.mkdir()
    (cfg / "stations.json").write_text(json.dumps(STATIONS), encoding="utf-8")

    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "STATE_DIR", state)
    monkeypatch.setattr(config, "SESSION_SECRET_FILE", state / ".session-secret")
    monkeypatch.setattr(config, "DJ_PERSONA_FILE", cfg / "dj-persona.md")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", PASSWORD)
    # the alias file's path is bound at import time, so it has to be moved by hand
    monkeypatch.setattr(genres, "_FILE", state / "genre-aliases.json")

    monkeypatch.setattr(mixer, "close", lambda: None)
    monkeypatch.setattr(mixer, "queued_ids", lambda st: [])
    monkeypatch.setattr(mixer, "pending_uris", lambda st: [])
    monkeypatch.setattr(mixer, "now_lines", lambda st: [])
    monkeypatch.setattr(pool, "write_pools", lambda conn, stations, log=print: {"alt": 2})
    monkeypatch.setattr(icecast, "listen_states", lambda: {})
    monkeypatch.setattr(icecast, "total_listeners", lambda states, st: 0)
    monkeypatch.setattr(icecast, "live_mount", lambda states, st: None)

    store._conn = None
    store._stations = None
    seed(store.conn())

    client = TestClient(application.app)
    client.post("/admin/login", data={"password": PASSWORD}, follow_redirects=False)
    yield client, cfg, state, store.conn()

    store._conn = None
    store._stations = None


def seed(conn):
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO tracks (id, uri, title, artist, album, album_id,"
            " year, duration) VALUES (?,?,?,?,?,?,?,?)",
            [
                ("t1" * 11, "http://navidrome/1.flac", "First", "Band A", "Record", "al1", 2001, 210),
                ("t2" * 11, "http://navidrome/2.flac", "Second", "Band B", "Record", "al1", 2002, 240),
                ("t3" * 11, "http://navidrome/3.flac", "Third", "Band C", "Other", "al2", 1999, 180),
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO track_genres (track_id, label, source) VALUES (?,?,?)",
            [
                ("t1" * 11, "Indie", "file"),
                ("t1" * 11, "Rock", "file"),
                ("t2" * 11, "Indie", "file"),
                ("t3" * 11, "Rock", "file"),
            ],
        )


def labels_of(conn, track_id):
    return {
        row["label"]
        for row in conn.execute("SELECT label FROM track_genres WHERE track_id = ?", (track_id,))
    }


def aliases(state):
    path = state / "genre-aliases.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("aliases", [])


def has_alias(state, source, target):
    """Aliases are stored with the source folded, which is what a rebuild matches on."""
    return any(
        entry.get("from", "").lower() == source.lower() and entry.get("to") == target
        for entry in aliases(state)
    )


def test_the_genre_page_shows_what_the_library_has(console):
    client, _, _, _ = console
    response = client.get("/admin/genres")
    assert response.status_code == 200
    assert "Indie" in response.text and "Rock" in response.text


def test_merging_a_label_moves_its_tracks(console):
    client, _, state, conn = console
    response = client.post(
        "/admin/genres/merge", data={"labels": ["Indie"], "into": "Rock"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "Indie" not in labels_of(conn, "t1" * 11)
    assert "Rock" in labels_of(conn, "t1" * 11)
    assert has_alias(state, "Indie", "Rock"), "the rule is kept for the next rebuild"


def test_merging_nothing_changes_nothing(console):
    client, _, state, conn = console
    response = client.post(
        "/admin/genres/merge", data={"labels": [], "into": "Rock"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "Indie" in labels_of(conn, "t1" * 11)
    assert aliases(state) == []


def test_merging_into_the_same_name_records_no_rule(console):
    client, _, state, conn = console
    client.post(
        "/admin/genres/merge", data={"labels": ["Rock"], "into": "Rock"}, follow_redirects=False
    )
    assert aliases(state) == []
    assert "Rock" in labels_of(conn, "t1" * 11)


def test_a_mapping_is_added_and_can_be_removed(console):
    client, _, state, conn = console
    client.post("/admin/genres/add", data={"source": "Post Rock", "target": "Rock"},
                follow_redirects=False)
    assert has_alias(state, "Post Rock", "Rock")

    client.post("/admin/genres/delete", data={"source": "post rock"}, follow_redirects=False)
    assert aliases(state) == []


def test_a_label_can_be_put_on_by_hand(console):
    client, _, _, conn = console
    response = client.post(
        "/admin/genres/assign", data={"tracks": ["t3" * 11], "label": "Shoegaze"}
    )
    assert response.status_code == 200
    assert "Shoegaze" in labels_of(conn, "t3" * 11)
    row = conn.execute(
        "SELECT source FROM track_genres WHERE track_id = ? AND label = 'Shoegaze'",
        ("t3" * 11,),
    ).fetchone()
    assert row["source"] == "manual", "a hand-made label outranks the providers"


def test_taking_a_label_off_remembers_the_refusal(console):
    client, _, _, conn = console
    response = client.post(
        "/admin/genres/unassign", data={"tracks": ["t1" * 11], "label": "Indie"}
    )
    assert response.status_code == 200
    assert "Indie" not in labels_of(conn, "t1" * 11)
    veto = conn.execute(
        "SELECT 1 FROM track_genre_veto WHERE track_id = ? AND label = 'Indie'", ("t1" * 11,)
    ).fetchone()
    assert veto is not None, "a rebuild must not put the label back"


def test_exploring_a_label_lists_its_tracks(console):
    client, _, _, _ = console
    response = client.get("/admin/genres/tracks", params=[("label", "Rock")])
    assert response.status_code == 200
    assert "First" in response.text or "Indie" in response.text


def test_exploring_nothing_is_not_an_error(console):
    client, _, _, _ = console
    assert client.get("/admin/genres/tracks").status_code == 200


# --- jobs ---------------------------------------------------------------------


def test_a_job_can_be_queued(console):
    client, _, _, conn = console
    response = client.post("/admin/jobs", data={"kind": "refresh"})
    assert response.status_code == 200
    kinds = [row["kind"] for row in conn.execute("SELECT kind FROM jobs")]
    assert "refresh" in kinds


def test_a_job_nobody_has_heard_of_is_refused(console):
    client, _, _, conn = console
    response = client.post("/admin/jobs", data={"kind": "make-coffee"})
    assert response.status_code == 200
    assert "error" in response.text.lower() or "make-coffee" in response.text
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0


def test_rebuilding_reports_what_the_index_holds(console, monkeypatch):
    client, _, _, _ = console
    monkeypatch.setattr(navidrome, "rebuild_index", lambda conn, log=None: 3)
    response = client.post("/admin/rebuild")
    assert response.status_code == 200
    assert "3" in response.text


def test_reloading_writes_the_mixer_script(console, monkeypatch):
    client, cfg, _, _ = console
    from spica.web import admin_home

    monkeypatch.setattr(admin_home, "_restart_mixer", lambda: "the mixer was told to restart")
    response = client.post("/admin/reload")
    assert response.status_code == 200
    assert (cfg / "spica.liq").exists()
    assert "queue" in (cfg / "spica.liq").read_text(encoding="utf-8")
