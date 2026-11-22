"""A station's rules: what gets in, what is vetoed, and what is too short to be music.

Two decisions live here. An artist named in the rules is the deliberate act, so it beats
a genre veto in both directions — a station that excludes Progressive Rock and lists
Giraffe wants Giraffe. And a four-second gap between songs is not a record: the rotation
aired one of those fifty-two times, so a minimum length now applies everywhere.
"""

from __future__ import annotations

import pytest

from spica import config, navidrome


def track(track_id: str, artist: str, *, duration: int = 200, year: int = 2000) -> tuple:
    return (
        track_id,
        f"http://navidrome/{track_id}.flac",
        f"title {track_id}",
        artist,
        "Record",
        "al1",
        1,
        year,
        "",
        duration,
        artist,
        navidrome.name_key(artist),
    )


@pytest.fixture
def db(station, tmp_path):
    conn = navidrome.connect(tmp_path / "index.db")
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO tracks (id, uri, title, artist, album, album_id,"
            " track_no, year, genre, duration, album_artist, album_artist_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                track("g" * 22, "Giraffe"),
                track("p" * 22, "Porcupine Tree"),
                track("c" * 22, "Clash", duration=150),
                track("s" * 22, "Silence", duration=4),
                track("z" * 22, "Unknown Length", duration=0),
                # A station whose rules match nothing falls back to the whole library,
                # so the date test needs one record on the right side of the clock.
                track("m" * 22, "Modern", year=2015),
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO track_genres (track_id, label, source) VALUES (?,?,?)",
            [
                ("g" * 22, "Progressive Rock", "mb"),
                ("p" * 22, "Progressive Rock", "mb"),
                ("c" * 22, "Pop Rock", "mb"),
                ("s" * 22, "Pop Rock", "mb"),
                ("z" * 22, "Pop Rock", "mb"),
                ("m" * 22, "Pop Rock", "mb"),
            ],
        )
    yield conn
    conn.close()


def ids(conn, station) -> set[str]:
    return {row["id"] for row in navidrome.rows_for_station(conn, station)}


def test_a_named_artist_beats_the_genre_veto(db):
    """The genre is a shelf, the name is a decision: Giraffe plays on a prog-free station."""
    giraffe = config.Station(
        id="alt", name="Alt",
        genres=("Pop Rock",), exclude_genres=("Progressive Rock",),
        artists=("Giraffe",),
    )
    got = ids(db, giraffe)
    assert "g" * 22 in got
    assert "p" * 22 not in got, "a prog band that was not named is still vetoed"


def test_an_excluded_artist_still_wins_over_a_named_one(db):
    """Naming an artist in both lists is a contradiction; the exclusion is the safe read."""
    both = config.Station(
        id="alt", name="Alt",
        genres=("Pop Rock",), artists=("Giraffe",), exclude_artists=("Giraffe",),
    )
    assert "g" * 22 not in ids(db, both)


def test_a_named_artist_still_has_to_clear_the_artist_veto_and_the_clock(db):
    """The exception is for genres, not for every other rule in the matcher."""
    dated = config.Station(
        id="alt", name="Alt",
        genres=("Pop Rock",), exclude_genres=("Progressive Rock",),
        artists=("Giraffe",), year_from=2010,
    )
    assert "g" * 22 not in ids(db, dated)


def test_a_gap_between_songs_is_not_a_record(db):
    station = config.Station(id="alt", name="Alt", genres=("Pop Rock",))
    got = ids(db, station)
    assert "s" * 22 not in got, "four seconds of silence is not a track"
    assert "c" * 22 in got, "a two-and-a-half minute song is a song"
    assert "z" * 22 in got, "no duration is no data, not a veto"


def test_the_floor_can_be_turned_off(db, monkeypatch):
    monkeypatch.setattr(config, "MIN_TRACK_S", 0)
    station = config.Station(id="alt", name="Alt", genres=("Pop Rock",))
    assert "s" * 22 in ids(db, station)
