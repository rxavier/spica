"""MusicBrainz genre caching: ask once, remember the answer, remember the misses.

The job used to store only answers, so forty artists whose lookup came back empty were
re-asked on every refresh — eight minutes of throttled requests per import, for ever.
These tests pin both halves of the fix: a miss is an answer worth caching, a failure to
ask is not, and the id Navidrome already carries is used instead of a name search.
"""

from __future__ import annotations

import time

import pytest

from spica import config, enrich, navidrome, worker
from tests.factories import insert_tracks


def seed(conn, artists=("Loud Band", "Quiet Band")) -> None:
    rows = []
    for i, name in enumerate(artists):
        for j in range(2):
            track_id = f"{i}{j}" + "x" * 20
            rows.append((track_id, f"http://navidrome/{track_id}.flac", f"song {i}-{j}",
                         name, f"album {i}", f"al{i}", j + 1, 2000, "", 200,
                         name, navidrome.name_key(name)))
    with conn:
        insert_tracks(conn, rows)


@pytest.fixture
def conn(station, tmp_path):
    db = navidrome.connect(tmp_path / "index.db")
    seed(db)
    yield db
    db.close()


def answer(artist, albums=None, mbid=None):
    return {"mbid": mbid or "id-" + artist, "name": artist,
            "genres": [{"name": "Progressive Rock", "votes": 3}]}


def test_a_miss_is_cached_and_not_asked_again(conn, monkeypatch):
    asked: list[str] = []

    def empty(artist, albums=None, mbid=None):
        asked.append(artist)
        return {"mbid": "", "name": artist, "genres": []}

    monkeypatch.setattr(enrich, "mb_artist_genres", empty)
    result = worker.run_musicbrainz(conn, 1, log=lambda *a: None)

    assert result["artists"] == 2 and result["found"] == 0
    assert worker.missing_musicbrainz(conn) == 0
    assert worker.run_musicbrainz(conn, 1, log=lambda *a: None)["artists"] == 0
    assert len(asked) == 2, "the second run must not ask again"


def test_a_failed_ask_is_not_cached(conn, monkeypatch):
    """`None` is "could not ask", not "nothing there": the next run has to try again."""
    monkeypatch.setattr(enrich, "mb_artist_genres",
                        lambda artist, albums=None, mbid=None: None)
    result = worker.run_musicbrainz(conn, 1, log=lambda *a: None)
    assert result["artists"] == 2
    assert worker.missing_musicbrainz(conn) == 2
    stored = conn.execute("SELECT COUNT(*) FROM mb_artist_genres").fetchone()[0]
    assert stored == 0


def test_an_answer_with_genres_is_cached_with_them(conn, monkeypatch):
    monkeypatch.setattr(enrich, "mb_artist_genres", answer)
    result = worker.run_musicbrainz(conn, 1, log=lambda *a: None)
    assert result["found"] == 2
    row = conn.execute("SELECT genres, mbid FROM mb_artist_genres WHERE artist_key = ?",
                       ("loud band",)).fetchone()
    assert "Progressive Rock" in row["genres"] and row["mbid"] == "id-Loud Band"


def test_a_stale_miss_is_asked_again(conn):
    with conn:
        conn.execute(
            "INSERT INTO mb_artist_genres (artist_key, mbid, genres, fetched_at)"
            " VALUES (?,?,?,?)",
            ("loud band", "", "[]", int(time.time()) - config.MB_GENRES_TTL_S - 60),
        )
        conn.execute(
            "INSERT INTO mb_artist_genres (artist_key, mbid, genres, fetched_at)"
            " VALUES (?,?,?,?)",
            ("quiet band", "", "[]", int(time.time())),
        )
    missing = [row["artist"] for row in worker._artists_without_mb_genres(conn)]
    assert missing == ["Loud Band"]


def test_the_navidrome_id_is_used_instead_of_a_name_search(conn, monkeypatch):
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO artist_mbids (name, mbid, source, fetched_at)"
            " VALUES (?,?,?,?)", ("Loud Band", "0f1e2d3c", "navidrome", int(time.time())),
        )
    seen: dict[str, str | None] = {}

    def capture(artist, albums=None, mbid=None):
        seen[artist] = mbid
        return {"mbid": mbid or "", "name": artist, "genres": []}

    monkeypatch.setattr(enrich, "mb_artist_genres", capture)
    worker.run_musicbrainz(conn, 1, log=lambda *a: None)
    assert seen["Loud Band"] == "0f1e2d3c"
    assert seen["Quiet Band"] is None


def test_a_known_id_skips_the_name_search(monkeypatch):
    urls: list[str] = []

    def fake_get(url, attempts=3):
        urls.append(url)
        if "/artist/abc?" in url:
            return {"genres": [{"name": "indie rock", "count": 4}]}
        raise AssertionError(f"name search on a known id: {url}")

    monkeypatch.setattr(enrich, "_mb_get", fake_get)
    monkeypatch.setattr(enrich, "_artist_overrides", lambda: {})
    info = enrich.mb_artist_genres("Some Name", mbid="abc", pause=0)
    assert info["genres"] == [{"name": "indie rock", "votes": 4}]
    assert len(urls) == 1 and "/artist/abc?" in urls[0]


def test_a_name_with_no_hits_is_an_empty_answer_not_a_failure(monkeypatch):
    monkeypatch.setattr(enrich, "_mb_get", lambda url, attempts=3: {"artists": []})
    monkeypatch.setattr(enrich, "_artist_overrides", lambda: {})
    info = enrich.mb_artist_genres("Nobody At All", pause=0)
    assert info == {"mbid": "", "name": "Nobody At All", "genres": []}


def test_a_service_that_cannot_be_reached_is_a_failure(monkeypatch):
    def boom(url, attempts=3):
        raise TimeoutError("musicbrainz is down")

    monkeypatch.setattr(enrich, "_mb_get", boom)
    monkeypatch.setattr(enrich, "_artist_overrides", lambda: {})
    assert enrich.mb_artist_genres("Some Name", pause=0) is None
