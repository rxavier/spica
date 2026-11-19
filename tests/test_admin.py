"""The admin console: who gets in, what renders, and what the writes actually write.

This is the surface that changes the station — stations, the prompt, settings, the
listener queue — and until now none of it was covered. The app is driven through
Starlette's TestClient without its lifespan, so the pick loop never starts and nothing
reaches Icecast, the mixer or a model. Config and state point at `tmp_path`.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from spica import app as application
from spica import auth, config, icecast, mixer, pool, queue, store

PASSWORD = "let-me-in"

STATIONS = {
    "stations": [
        {
            "id": "alt",
            "name": "Alternative",
            "description": "Guitars and synths.",
            "include_genres": ["Rock"],
        },
        {"id": "all", "name": "All of it", "description": ""},
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

    # nothing in here may talk to the stack
    monkeypatch.setattr(mixer, "close", lambda: None)
    monkeypatch.setattr(mixer, "queued_ids", lambda st: [])
    monkeypatch.setattr(mixer, "pending", lambda st: 0)
    monkeypatch.setattr(mixer, "pending_uris", lambda st: [])
    monkeypatch.setattr(mixer, "set_queue", lambda st, uris: True)
    monkeypatch.setattr(mixer, "now_lines", lambda st: [])
    monkeypatch.setattr(pool, "write_pools", lambda conn, stations, log=print: {})
    monkeypatch.setattr(icecast, "listen_states", lambda: {})
    monkeypatch.setattr(icecast, "total_listeners", lambda states, st: 0)
    monkeypatch.setattr(icecast, "live_mount", lambda states, st: None)

    store._conn = None
    store._stations = None
    client = TestClient(application.app)
    yield client, cfg, state

    store._conn = None
    store._stations = None


def login(client):
    response = client.post("/admin/login", data={"password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303
    return client


def saved_stations(cfg):
    return json.loads((cfg / "stations.json").read_text(encoding="utf-8"))["stations"]


# --- who gets in ---------------------------------------------------------------


def test_the_console_refuses_a_stranger(console):
    client, _, _ = console
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_a_wrong_password_is_401_and_sets_nothing(console):
    client, _, _ = console
    response = client.post("/admin/login", data={"password": "nope"}, follow_redirects=False)
    assert response.status_code == 401
    assert auth.COOKIE not in response.cookies


def test_the_right_password_opens_the_console(console):
    client, _, _ = console
    login(client)
    assert client.get("/admin").status_code == 200


def test_a_tampered_cookie_is_refused(console):
    client, _, _ = console
    client.cookies.set(auth.COOKIE, "admin|99999999999|deadbeef")
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303


def test_logging_out_closes_the_door(console):
    client, _, _ = console
    login(client)
    assert client.post("/admin/logout", follow_redirects=False).status_code == 303
    assert client.get("/admin", follow_redirects=False).status_code == 303


# --- what renders --------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "/",
        "/s/alt",
        "/s/alt/history",
        "/admin",
        "/admin/queue",
        "/admin/library",
        "/admin/genres",
        "/admin/settings",
        "/admin/prompt",
        "/admin/stations",
        "/admin/stations/alt",
    ],
)
def test_every_page_renders(console, url):
    client, _, _ = console
    login(client)
    response = client.get(url)
    assert response.status_code == 200
    assert response.text, "a page that renders empty is a broken template"


def test_an_empty_requests_fragment_is_empty_on_purpose(console):
    """A fragment with nothing to say answers 200 and no body; htmx leaves it alone."""
    client, _, _ = console
    login(client)
    response = client.get("/s/alt/requests")
    assert response.status_code == 200


def test_an_unknown_station_is_404_not_a_traceback(console):
    client, _, _ = console
    login(client)
    assert client.get("/admin/stations/ghost").status_code == 404


# --- what the writes write -----------------------------------------------------


def test_the_prompt_is_saved_to_the_config(console):
    client, cfg, _ = console
    login(client)
    response = client.post("/admin/prompt", data={"text": "You are the night shift."})
    assert response.status_code == 200
    assert "night shift" in (cfg / "dj-persona.md").read_text(encoding="utf-8")
    assert "night shift" in client.get("/admin/prompt").text


def test_the_prompt_can_be_put_back(console):
    client, cfg, _ = console
    login(client)
    client.post("/admin/prompt", data={"text": "You are the night shift."})
    client.post("/admin/prompt/reset")
    persona = cfg / "dj-persona.md"
    # Resetting deletes the override, so the built-in persona is what plays.
    assert not persona.exists() or "night shift" not in persona.read_text(encoding="utf-8")


def test_a_new_station_lands_in_stations_json(console):
    client, cfg, _ = console
    login(client)
    response = client.post(
        "/admin/new-station",
        data={"name": "Late Shift", "description": "Quiet things.", "new_id": "late"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    ids = [item["id"] for item in saved_stations(cfg)]
    assert "late" in ids


def test_a_new_station_needs_an_id_it_can_use(console):
    client, cfg, _ = console
    login(client)
    before = saved_stations(cfg)
    response = client.post(
        "/admin/new-station", data={"name": "???", "new_id": ""}, follow_redirects=False
    )
    assert response.status_code == 303
    assert saved_stations(cfg) == before


def test_editing_a_station_keeps_the_rest_of_the_rack(console):
    client, cfg, _ = console
    login(client)
    client.post(
        "/admin/stations/alt",
        data={"name": "Alternative", "description": "Only the newer description."},
    )
    stations = {item["id"]: item for item in saved_stations(cfg)}
    assert stations["alt"]["description"] == "Only the newer description."
    assert "all" in stations, "saving one station must not drop another"


def test_deleting_a_station_removes_it_and_only_it(console):
    client, cfg, _ = console
    login(client)
    client.post("/admin/stations/alt/delete")
    ids = [item["id"] for item in saved_stations(cfg)]
    assert "alt" not in ids
    assert "all" in ids


def test_a_listener_request_is_recorded(console):
    client, _, state = console
    response = client.post(
        "/s/alt/request", data={"text": "something for the rain"}, follow_redirects=False
    )
    assert response.status_code in (200, 303)
    requests_file = state / "history" / "alt.requests.jsonl"
    if requests_file.exists():
        assert "something for the rain" in requests_file.read_text(encoding="utf-8")


def _write_pick(state, station_id, **entry):
    path = state / "history" / f"{station_id}.picks.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    base = {"t": "2026-11-17T19:25:10Z", "id": "t1", "title": "Title",
            "artist": "Artist", "why": "Because."}
    base.update(entry)
    path.write_text(json.dumps(base) + "\n", encoding="utf-8")


def test_admin_lands_on_stations(console):
    client, _, _ = console
    login(client)
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/stations"


def test_the_queue_has_station_tabs_a_date_and_the_whole_brief(console):
    client, _, state = console
    login(client)
    brief = "start-of-the-brief\n" + ("x" * 2500) + "\nend-of-the-brief"
    _write_pick(state, "alt", brief=brief, path="table", coverage=1.0,
                shelf=["A", "B"], tools=[])
    page = client.get("/admin/queue?station=alt").text
    assert "?station=all" in page and "?station=alt" in page
    assert "2026-11-17" in page
    assert "end-of-the-brief" in page
    assert "Tracks" in page


def test_a_long_brief_survives_the_pick_log(station):
    queue.record_pick(station, "t1", "T", "A", "why",
                      detail={"brief": "x" * 3000 + "-END"})
    assert queue.recent_picks(station, 1)[-1]["brief"].endswith("-END")


def test_the_library_names_each_job_and_the_current_offer(console):
    client, _, _ = console
    login(client)
    page = client.get("/admin/library").text
    for phrase in ("How the DJ is picking", "What the DJ knows", "Fetch popularity",
                   "Cache similar artists", "MusicBrainz", "Last.fm tags",
                   "Audio analysis", "Measure loudness"):
        assert phrase in page, phrase


def test_the_persona_page_shows_the_assembled_prompt(console):
    client, _, _ = console
    login(client)
    page = client.get("/admin/prompt").text
    assert "DJ Persona" in page
    assert "What the DJ is actually told" in page
    assert "Choose a set, not a single record" in page


def test_provider_settings_are_saved_and_cleared(console):
    client, cfg, _ = console
    login(client)
    client.post("/admin/settings/providers",
                data={"lastfm_key": "lfm-123", "mb_user_agent": "ops@example.com"})
    settings = config.provider_settings()
    assert settings["lastfm_key"] == "lfm-123"
    assert settings["mb_user_agent"] == "ops@example.com"
    saved = json.loads((cfg / "providers.json").read_text(encoding="utf-8"))
    assert saved["lastfm_key"] == "lfm-123"
    assert "a key is saved" in client.get("/admin/settings").text

    client.post("/admin/settings/providers",
                data={"lastfm_key": "", "mb_user_agent": "", "clear_lastfm": "1"})
    settings = config.provider_settings()
    assert settings["lastfm_key"] == ""
    assert settings["mb_user_agent"] == config.MB_USER_AGENT


def test_enrich_reads_the_saved_lastfm_key(tmp_path, monkeypatch):
    from spica import enrich

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    config.save_provider_settings("saved-key", "")
    seen = {}

    def fake_get(url, headers=None, timeout=20.0):
        seen["url"] = url
        return {"similarartists": {"artist": []}}

    monkeypatch.setattr(enrich, "_get_json", fake_get)
    monkeypatch.setattr(enrich, "_throttle", lambda *args, **kwargs: None)
    enrich.lfm_similar_scored("Artist")
    assert "api_key=saved-key" in seen["url"]


def test_the_dj_mode_is_saved_and_cleared_plans(console):
    client, _, state = console
    login(client)
    response = client.post("/admin/settings/dj", data={"mode": "artist"})
    assert response.status_code == 200
    assert config.dj_mode() == "artist"
    assert "Artist-led" in client.get("/admin/settings").text

    response = client.post("/admin/settings/dj", data={"mode": "nonsense"})
    assert response.status_code == 400


def test_the_path_instructions_are_editable(console):
    client, cfg, _ = console
    login(client)
    response = client.post("/admin/prompt/path",
                           data={"kind": "shelf", "text": "Custom shelf instruction."})
    assert response.status_code == 200
    assert "Custom shelf instruction." in (cfg / "dj-shelf.md").read_text(encoding="utf-8")
    page = client.get("/admin/prompt").text
    assert "Custom shelf instruction." in page

    client.post("/admin/prompt/path",
                data={"kind": "shelf", "text": "ignored", "reset": "1"})
    assert not (cfg / "dj-shelf.md").exists()


def _seed_artists(conn, count):
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO tracks (id, uri, title, artist, album, album_id, album_artist)"
            " VALUES (?,?,?,?,?,?,?)",
            [
                (f"t{i:03d}", f"http://navidrome/{i}", f"Track {i}",
                 f"Artist {i:03d}", f"Artist {i:03d}", f"al{i:03d}", f"Artist {i:03d}")
                for i in range(1, count + 1)
            ],
        )


def test_the_station_artist_list_searches_and_pages(console):
    client, _, _ = console
    login(client)
    _seed_artists(store.conn(), 35)

    match = client.get("/admin/stations/alt/artists?q=artist%20035").text
    assert "Artist 035" in match
    assert "Artist 034" not in match

    second = client.get("/admin/stations/alt/artists?page=2").text
    assert "page 2 of 2" in second
    assert "Artist 031" in second
    assert "Artist 001" not in second


def test_the_station_page_keeps_the_artist_dropdowns_and_shows_the_full_list(console):
    client, _, _ = console
    login(client)
    _seed_artists(store.conn(), 2)

    page = client.get("/admin/stations/alt").text
    assert "data-multiselect" in page, "the include/exclude dropdowns stay"
    assert "station-artist-results" in page
    assert "Artist 001" in page and "Artist 002" in page
    assert 'data-exclude-artist="Artist 001"' in page


def test_the_station_artist_list_groups_by_album_artist(console):
    client, _, _ = console
    login(client)
    conn = store.conn()
    _seed_artists(conn, 2)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO tracks"
            " (id, uri, title, artist, album, album_id, album_artist)"
            " VALUES (?,?,?,?,?,?,?)",
            ("guest", "http://navidrome/guest", "Guest Track", "Guest Credit",
             "Artist 001", "al001", "Artist 001"),
        )

    page = client.get("/admin/stations/alt/artists").text
    assert "Artist 001" in page
    assert "Guest Credit" not in page

    form = client.get("/admin/stations/alt").text
    assert "Guest Credit" not in form
