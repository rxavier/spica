"""What each tool actually hands back: one view, lean rows, and a page that fits.

The payload is the model's whole picture of a lookup, so it has to be cheap enough to
send twelve times per set, complete enough to choose from, and honest about what it left
out — `returned: 30` beside twenty-three rows makes `offset` paging skip tracks that were
never shown.
"""

from __future__ import annotations

import json

import pytest

from spica import config, enrich, genres, mixer, navidrome, queue
from tests.factories import insert_tracks
from spica.picker import library as picker_library
from spica.picker import llm


def track(track_id: str, artist: str, *, album: str = "Record", album_id: str = "al1",
          track_no: int = 1, year: int = 2000) -> tuple:
    return (
        track_id, f"http://navidrome/{track_id}.flac", f"title {track_id}", artist,
        album, album_id, track_no, year, "", 200, artist, navidrome.name_key(artist),
    )


@pytest.fixture
def db(station, tmp_path, monkeypatch):
    monkeypatch.setattr(genres, "alias_path", lambda: tmp_path / "genre-aliases.json")
    genres.reset_cache()
    monkeypatch.setattr(mixer, "queued_ids", lambda st=None: [])
    monkeypatch.setattr(enrich, "lfm_similar", lambda name, limit=100: [])
    monkeypatch.setattr(queue, "recent", lambda st, n=20: [])
    conn = navidrome.connect(tmp_path / "index.db")
    yield conn
    conn.close()


def seed(conn, tracks, labels=()):
    with conn:
        insert_tracks(conn, tracks)
        if labels:
            conn.executemany(
                "INSERT OR REPLACE INTO track_genres (track_id, label, source) VALUES (?,?,?)",
                labels,
            )


def many(count: int, artist: str = "Band") -> list[tuple]:
    return [track(f"{i:05d}".ljust(22, "x"), artist, track_no=i + 1) for i in range(count)]


# --------------------------------------------------------------- catalogue


def test_catalogue_answers_with_albums_only(db, station):
    seed(db, [track("a" * 22, "Band", album="One", album_id="al1", track_no=1)])
    res = picker_library.Library(db).catalogue(artist="Band", station=station)

    assert "tracks" not in res, "the flat list was the same tracks a second time"
    assert [a["album"] for a in res["albums"]] == ["One"]


def test_a_catalogue_track_carries_nothing_the_album_already_says(db, station):
    seed(db, [track("a" * 22, "Band", album="One", album_id="al1", track_no=1)])
    res = picker_library.Library(db).catalogue(artist="Band", station=station)
    item = res["albums"][0]["tracks"][0]

    assert set(item) == {"id", "title", "track_no", "duration", "popularity", "played"}
    album = res["albums"][0]
    assert album["year"] == 2000 and "genre" in album


def test_catalogue_says_how_much_it_left_out(db, station):
    seed(db, many(40))
    res = picker_library.Library(db).catalogue(artist="Band", limit=25, station=station)

    assert res["shown"] == 25 and res["total"] == 40
    assert "showing 25 of 40" in res["note"]
    assert sum(len(a["tracks"]) for a in res["albums"]) == 25


# ------------------------------------------------------------------ paging


def test_filter_pages_by_what_it_actually_returned(db, station):
    seed(db, many(60))
    res = picker_library.Library(db).filter(station=station, limit=200)

    assert res["returned"] == len(res["results"])
    assert res["returned"] < 200, "a page that does not fit is trimmed, not shipped"
    assert res["next_offset"] == res["offset"] + res["returned"]
    assert len(json.dumps(res, ensure_ascii=False)) <= llm.TOOL_RESULT_CHARS


def test_a_second_filter_page_starts_where_the_first_stopped(db, station):
    seed(db, many(60))
    lib = picker_library.Library(db)
    lib.draw_seed = 7
    first = lib.filter(station=station, limit=200)
    second = lib.filter(station=station, limit=200, offset=first["next_offset"])

    assert first["results"] and second["results"]
    assert {r["id"] for r in first["results"]} & {r["id"] for r in second["results"]} == set()


def test_history_fits_its_budget(db, station, monkeypatch):
    seed(db, many(60))
    plays = [{"id": f"{i:05d}".ljust(22, "x"), "artist": "Band"} for i in range(60)]
    monkeypatch.setattr(queue, "recent", lambda st, n=20: plays[-n:])

    got = picker_library.Library(db).history(station, limit=100)

    assert len(got) <= 100
    assert len(json.dumps(got, ensure_ascii=False)) <= llm.TOOL_RESULT_CHARS


# --------------------------------------------------------------- the trimmer


def test_fit_trims_the_largest_list_not_the_first_one_it_knows():
    payload = {
        "albums": [{"album": f"record {i}", "tracks": [{"id": f"{i}-{j}"} for j in range(20)]}
                   for i in range(6)],
        "note": "hand-made",
    }
    text = llm.fit(payload, limit=2000)

    assert len(text) <= 2000
    parsed = json.loads(text)
    assert parsed["note"] == "hand-made", "a note the tool wrote is not overwritten"
    remaining = sum(len(album["tracks"]) for album in parsed["albums"])
    assert remaining < 120, "the nested track lists are what got cut"


def test_fit_never_returns_a_payload_over_the_cap(db, station):
    seed(db, many(120))
    payload = picker_library.Library(db).filter(station=station, limit=200)

    for limit in (600, 1200, 3000, 6000):
        assert len(llm.fit(payload, limit=limit)) <= limit
