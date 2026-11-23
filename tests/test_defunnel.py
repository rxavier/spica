"""De-funnelling: the library's fine labels in the index, rollups at match time.

The DJ's tools were the funnel. `filter(genre="Nu Disco")` was answered with the whole
Electronic shelf because the operator alias had rewritten the label at import; a search
miss handed back the same lead id for every absent band; `catalogue` opened on the same
record every time; and nothing demoted an artist that had aired ten minutes ago.

These tests seed a small index and call the same functions the tools call, so they check
what the model would actually receive.
"""

from __future__ import annotations

import json
import time

import pytest

from spica import config, enrich, genres, mixer, navidrome, queue, worker
from spica.picker import library as picker_library

ALIAS = {"aliases": [{"from": "nu disco", "to": "Electronic"}]}


def track(track_id: str, artist: str, *, album: str = "Record", album_id: str = "al1",
          track_no: int = 1, year: int = 2000, album_artist: str | None = None) -> tuple:
    record_artist = album_artist or artist
    return (
        track_id,
        f"http://navidrome/{track_id}.flac",
        f"title {track_id}",
        artist,
        album,
        album_id,
        track_no,
        year,
        "",
        200,
        record_artist,
        navidrome.name_key(record_artist),
    )


def label(track_id: str, name: str, source: str = "tag") -> tuple:
    return (track_id, name, source)


@pytest.fixture
def db(station, tmp_path, monkeypatch):
    """A real index in the temporary state directory, and the alias file beside it."""
    alias_file = tmp_path / "genre-aliases.json"
    alias_file.write_text(json.dumps(ALIAS), encoding="utf-8")
    monkeypatch.setattr(genres, "_FILE", alias_file)
    monkeypatch.setattr(genres, "_cache", (-1.0, {}))
    monkeypatch.setattr(mixer, "queued_ids", lambda st=None: [])
    monkeypatch.setattr(enrich, "lfm_similar", lambda name, limit=100: [])
    conn = navidrome.connect(tmp_path / "index.db")
    yield conn
    conn.close()


def seed(conn, tracks, labels):
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO tracks (id, uri, title, artist, album, album_id,"
            " track_no, year, genre, duration, album_artist, album_artist_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            tracks,
        )
        conn.executemany(
            "INSERT OR REPLACE INTO track_genres (track_id, label, source) VALUES (?,?,?)",
            labels,
        )


# --- 1. the rollup moved to match time ---------------------------------------

def test_a_station_ticks_the_shelf_and_still_catches_the_fine_label(db, station):
    seed(db, [track("1" * 22, "Disco Band"), track("2" * 22, "Electro Band"),
              track("3" * 22, "Jazz Band")],
         [label("1" * 22, "Nu Disco"), label("2" * 22, "Electronic"),
          label("3" * 22, "Jazz Fusion")])

    broad = config.Station(id="broad", name="Broad", genres=("Electronic",))
    ids = {row["id"] for row in navidrome.rows_for_station(db, broad)}
    assert "1" * 22 in ids, "a nu-disco track still answers the Electronic tick"
    assert "2" * 22 in ids

    narrow = config.Station(id="narrow", name="Narrow", genres=("Nu Disco",))
    ids = {row["id"] for row in navidrome.rows_for_station(db, narrow)}
    assert ids == {"1" * 22}

    without = config.Station(id="without", name="Without", exclude_genres=("Electronic",))
    ids = {row["id"] for row in navidrome.rows_for_station(db, without)}
    assert "1" * 22 not in ids and "2" * 22 not in ids
    assert "3" * 22 in ids, "the fallback must not swallow the whole library"


# --- 2. canonical() honours the flag -----------------------------------------

def test_the_operator_rule_is_opt_in(db):
    assert genres.canonical("Nu Disco") == "Electronic"
    assert genres.canonical("Nu Disco", operator=False) == "Nu Disco"
    assert genres.canonical("synthpop", operator=False) == "Synth Pop", "builtin still applies"


# --- 3. filter answers the fine label ----------------------------------------

def test_filter_matches_the_fine_label_not_the_shelf(db, station):
    seed(db, [track("1" * 22, "Disco Band"), track("2" * 22, "Electro Band")],
         [label("1" * 22, "Nu Disco"), label("2" * 22, "Electronic")])
    lib = picker_library.Library(db)

    disco = lib.filter(station=station, genre="Nu Disco")
    assert [r["id"] for r in disco["results"]] == ["1" * 22]
    assert disco["matching"] == 1

    electronic = lib.filter(station=station, genre="Electronic")
    assert [r["id"] for r in electronic["results"]] == ["2" * 22]


# --- 4. filter demotes a recent artist ---------------------------------------

def test_filter_puts_the_artist_that_just_played_last(db, station):
    seed(db, [track("1" * 22, "A", track_no=1), track("2" * 22, "A", track_no=2),
              track("3" * 22, "B", track_no=1), track("4" * 22, "B", track_no=2)],
         [label(t, "Rock") for t in ("1" * 22, "2" * 22, "3" * 22, "4" * 22)])
    queue.record(station, "1" * 22, "title 1", "A", "dj")
    queue.record(station, "2" * 22, "title 2", "A", "dj")

    lib = picker_library.Library(db)
    res = lib.filter(station=station)
    artists = [row["artist"] for row in res["results"]]
    assert res["matching"] == 4, "demotion reorders, it does not remove"
    assert artists[:2] == ["B", "B"]
    assert set(artists[2:]) == {"A"}


# --- 5. similar demotes a recent artist --------------------------------------

def test_similar_puts_the_artist_that_just_played_last(db, station, monkeypatch):
    seed(db, [track("1" * 22, "A"), track("2" * 22, "B")],
         [label("1" * 22, "Rock"), label("2" * 22, "Rock")])
    queue.record(station, "1" * 22, "title 1", "A", "dj")
    monkeypatch.setattr(enrich, "similar_artists", lambda conn, name, limit=20: (["A", "B"], "cache"))

    lib = picker_library.Library(db)
    res = lib.similar("Somebody", station=station)
    assert [r["artist"] for r in res["tracks"]] == ["B", "A"]


# --- 6 and 7. the search-miss path ------------------------------------------

def test_a_search_miss_offers_at_most_three_names_and_no_track(db, station, monkeypatch):
    """A miss is a route, not a pick: three artist names, no ids, no albums.

    The four acts that sound like the model's favourite French-touch names were being
    handed over with a playable id on every miss, so a night of asking for that scene
    fed them straight into the sets. Opening the record with `catalogue` puts the pick
    back through the album order and the recency demotion.
    """
    names = ["Solo", "Duo", "Trio", "Quartet"]
    seed(db, [track(f"{i}" * 22, n) for i, n in enumerate(names)],
         [label(f"{i}" * 22, "Rock") for i in range(len(names))])
    monkeypatch.setattr(enrich, "similar_artists",
                        lambda conn, name, limit=25: (names, "cache"))

    lib = picker_library.Library(db)
    lib.draw_seed = 4
    res = lib.run_tool("search", {"query": "Nobody We Own", "limit": 5}, station)
    offers = res["similar_here"]
    assert res["results"] == []
    assert len(offers) == 3, "three names, not a shopping list"
    assert all(set(offer) == {"artist", "tracks"} for offer in offers)
    assert "catalogue" in res["note"]


def test_a_search_miss_stops_offering_what_just_played(db, station, monkeypatch):
    """Everything similar has been on air: say so, and offer nothing."""
    names = ["Solo", "Duo"]
    seed(db, [track("1" * 22, "Solo", track_no=1), track("2" * 22, "Duo")],
         [label("1" * 22, "Rock"), label("2" * 22, "Rock")])
    queue.record(station, "1" * 22, "title 1", "Solo", "dj")
    queue.record(station, "2" * 22, "title 2", "Duo", "dj")
    monkeypatch.setattr(enrich, "similar_artists", lambda conn, name, limit=25: (names, "cache"))

    lib = picker_library.Library(db)
    lib.draw_seed = 4
    res = lib.run_tool("search", {"query": "Nobody We Own", "limit": 5}, station)
    assert res["similar_here"] == [], "the corner has had its turn"
    assert "Duo" not in res["note"] and "Solo" not in res["note"], "no names smuggled in the note"
    assert "on air recently" in res["note"]


def test_a_search_miss_with_nothing_similar_says_to_change_the_vibe(db, station, monkeypatch):
    seed(db, [track("1" * 22, "Solo")], [label("1" * 22, "Rock")])
    monkeypatch.setattr(enrich, "similar_artists", lambda conn, name, limit=25: ([], "none"))
    monkeypatch.setattr(enrich, "lfm_similar", lambda name, limit=100: [])

    lib = picker_library.Library(db)
    res = lib.run_tool("search", {"query": "Nobody We Own", "limit": 5}, station)
    assert res["similar_here"] == []
    assert "another vibe" in res["note"]


def test_a_search_miss_no_longer_hands_out_one_fixed_track(db, station, monkeypatch):
    seed(db, [track("1" * 22, "Solo", album="One", album_id="al1", track_no=1),
              track("2" * 22, "Solo", album="Two", album_id="al2", track_no=1)],
         [label("1" * 22, "Rock"), label("2" * 22, "Rock")])
    monkeypatch.setattr(enrich, "similar_artists", lambda conn, name, limit=25: (["Solo"], "cache"))

    lib = picker_library.Library(db)
    offered = set()
    for seed_value in range(1, 21):
        lib.draw_seed = seed_value
        offers = lib.similar_here("Nobody We Own", station=station)
        offered.add(offers[0]["id"])
    assert len(offered) > 1, "the lead track is drawn per pick, not fixed for ever"


def test_a_search_miss_offers_the_recent_artist_last(db, station, monkeypatch):
    seed(db, [track("1" * 22, "A"), track("2" * 22, "B")],
         [label("1" * 22, "Rock"), label("2" * 22, "Rock")])
    queue.record(station, "1" * 22, "title 1", "A", "dj")
    monkeypatch.setattr(enrich, "similar_artists", lambda conn, name, limit=25: (["A", "B"], "cache"))

    lib = picker_library.Library(db)
    lib.draw_seed = 7
    offers = lib.similar_here("Nobody We Own", station=station)
    assert [o["artist"] for o in offers] == ["B", "A"]


# --- 8. browsing an artist no longer opens on the same record ----------------

def test_catalogue_album_order_follows_the_pick(db, station):
    tracks = []
    for album_no, album in enumerate(("Alpha", "Bravo", "Charlie")):
        for track_no in (1, 2, 3):
            album_id = f"al{album_no}"
            tracks.append(track(f"{album_no}{track_no}".ljust(22, "x"), "Band",
                                album=album, album_id=album_id, track_no=track_no))
    seed(db, tracks, [label(t[0], "Rock") for t in tracks])

    lib = picker_library.Library(db)
    orders = set()
    for seed_value in range(1, 21):
        lib.draw_seed = seed_value
        res = lib.catalogue(artist="Band", station=station)
        orders.add(tuple(a["album"] for a in res["albums"]))
        for album in res["albums"]:
            numbers = [item["track_no"] for item in album["tracks"]]
            assert numbers == sorted(numbers), "tracks stay in running order inside an album"
    assert len(orders) > 1, "an artist's first page is not always the same record"

    lib.draw_seed = 3
    one = lib.catalogue(artist="Band", album="Bravo", station=station)
    assert [a["album"] for a in one["albums"]] == ["Bravo"]
    assert [t["track_no"] for t in one["albums"][0]["tracks"]] == [1, 2, 3]


# --- the admin keeps the collapse -------------------------------------------

def test_a_veto_on_the_shelf_name_still_bites_a_fine_label(db, station):
    seed(db, [track("1" * 22, "Disco Band")], [label("1" * 22, "Nu Disco")])
    with db:
        db.execute("INSERT INTO track_genre_veto (track_id, label) VALUES (?,?)",
                   ("1" * 22, "Electronic"))

    navidrome.apply_genre_precedence(db)
    left = {row["label"] for row in db.execute(
        "SELECT label FROM track_genres WHERE track_id = ?", ("1" * 22,))}
    assert "Nu Disco" not in left, "a veto placed on Electronic covers the fine label"


def test_the_console_shows_one_row_per_shelf(db, station):
    seed(db, [track("1" * 22, "Disco Band"), track("2" * 22, "Other")],
         [label("1" * 22, "Nu Disco"), label("1" * 22, "Electronic", "musicbrainz"),
          label("2" * 22, "Jazz Fusion")])
    counts = {row["label"]: row for row in navidrome.genre_counts(db)}
    assert "Electronic" in counts
    assert counts["Electronic"]["members"], "the fine labels are nested under the shelf"
    assert {m["label"] for m in counts["Electronic"]["members"]} == {"Nu Disco"}
    assert navidrome.genres(db)[0][0] in {"Electronic", "Jazz Fusion"}


def test_a_station_genre_written_as_the_shelf_name_survives_a_save(db, station, monkeypatch):
    from spica.web import shared

    seed(db, [track("1" * 22, "Disco Band")], [label("1" * 22, "Nu Disco")])
    monkeypatch.setattr(shared, "_conn", lambda: db)
    assert shared.known_labels(["Electronic"]) == ["Electronic"]
    assert shared.known_labels(["Nonsense Genre"]) == []


# --- 8. the record's artist, not the guest credit ----------------------------

def test_similar_matches_the_album_artist_not_the_guest_credit(db, station, monkeypatch):
    seed(db, [track("1" * 22, "Band & Guest", album_artist="Band")],
         [label("1" * 22, "Rock")])
    monkeypatch.setattr(
        enrich, "similar_artists",
        lambda conn, name, limit=20: (["Band"], "cache"),
    )
    lib = picker_library.Library(db)
    res = lib.similar("Somebody", station=station)
    assert res["source"] == "cache"
    assert [r["artist"] for r in res["tracks"]] == ["Band"]


def test_label_albums_group_by_album_artist(db):
    seed(db, [track("1" * 22, "Band & Guest", album_artist="Band"),
              track("2" * 22, "Band", album_artist="Band")],
         [label("1" * 22, "Rock"), label("2" * 22, "Rock")])
    rows = navidrome.label_albums(db, "Rock")
    assert len(rows) == 1
    assert rows[0]["artist"] == "Band"
    assert rows[0]["tracks"] == 2


def test_musicbrainz_precedence_matches_the_album_artist(db):
    seed(db, [track("1" * 22, "Band & Guest", album_artist="Band")], [])
    with db:
        db.execute(
            "INSERT OR REPLACE INTO mb_artist_genres"
            " (artist_key, mbid, genres, fetched_at) VALUES (?,?,?,?)",
            ("band", "mbid", '[{"name": "Progressive Rock", "votes": 10}]', 0),
        )
    navidrome.apply_genre_precedence(db)
    labels = {
        row["label"] for row in db.execute(
            "SELECT label FROM track_genres WHERE track_id = ?", ("1" * 22,)
        )
    }
    assert "Progressive Rock" in labels


def test_missing_musicbrainz_counts_album_artists(db):
    """A cached check counts as done, genres or not: see test_mb_genres for the TTL."""
    seed(db, [track("1" * 22, "Band & Guest", album_artist="Band")], [])
    assert worker.missing_musicbrainz(db) == 1
    with db:
        db.execute(
            "INSERT INTO mb_artist_genres (artist_key, mbid, genres, fetched_at)"
            " VALUES (?,?,?,?)",
            ("band", "mbid", "[]", int(time.time())),
        )
    assert worker.missing_musicbrainz(db) == 0
