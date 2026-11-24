"""The rotation is a list, but it still has to respect what just played.

The pool is written from a fresh shuffle at rebuild time. These pin the two things
that keep it from opening on the last record: recent tracks are left out of the
list, and the first placement is seeded with the artists and album that just aired.
"""

from __future__ import annotations

from spica import config, navidrome, pool, queue, subsonic
from tests.factories import insert_track, insert_tracks


def _track(track_id: str, artist: str, album: str, album_id: str) -> tuple:
    return (
        track_id,
        f"http://navidrome/rest/stream?id={track_id}",
        f"title {track_id}",
        artist,
        album,
        album_id,
    )


def test_the_pool_leaves_out_recent_tracks_and_seeds_the_artist_window(
    tmp_path, monkeypatch, station,
):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    conn = navidrome.connect(tmp_path / "index.db")
    with conn:
        insert_tracks(
            conn,
            [
                _track("a1", "Band A", "First", "al1"),
                _track("a2", "Band A", "First", "al1"),
                _track("a3", "Band A", "Second", "al2"),
                _track("b1", "Band B", "Other", "al3"),
                _track("b2", "Band B", "Other", "al3"),
                _track("c1", "Band C", "Third", "al4"),
                _track("c2", "Band C", "Third", "al4"),
                _track("d1", "Band D", "Fourth", "al5"),
            ],
            columns=("id", "uri", "title", "artist", "album", "album_id"),
        )
    # A1 and A2 just aired; Band A is the artist that must not open the rotation.
    queue.record(station, "a1", "title a1", "Band A", "standby")
    queue.record(station, "a2", "title a2", "Band A", "standby")

    pool.write_pools(conn, [station], log=lambda *args, **kwargs: None)
    lines = station.pool_file.read_text(encoding="utf-8").splitlines()
    ids = [subsonic.song_id(line) for line in lines]

    assert "a1" not in ids and "a2" not in ids, "recent tracks stay out of the rotation"
    assert len(ids) == 6
    first = conn.execute("SELECT * FROM tracks WHERE id = ?", (ids[0],)).fetchone()
    assert (first["album_artist"] or first["artist"]) != "Band A", (
        "the first placement is seeded with the artist that just aired"
    )


def test_a_tiny_station_keeps_a_rotation_even_when_everything_is_recent(
    tmp_path, monkeypatch, station,
):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    conn = navidrome.connect(tmp_path / "index.db")
    with conn:
        track_id, uri, title, artist, album, album_id = _track(
            "only", "Only Band", "Only", "only"
        )
        insert_track(
            conn, id=track_id, uri=uri, title=title, artist=artist,
            album=album, album_id=album_id,
        )
    queue.record(station, "only", "title only", "Only Band", "standby")

    pool.write_pools(conn, [station], log=lambda *args, **kwargs: None)
    assert station.pool_file.read_text(encoding="utf-8").strip()
