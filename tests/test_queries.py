"""The query layer: shared SQL fragments and the track lookups built on them."""

from __future__ import annotations

from spica import db, navidrome
from spica.queries import labels, tracks


def test_artist_expr_follows_the_alias():
    assert db.artist_expr() == "COALESCE(NULLIF(album_artist, ''), artist)"
    assert db.artist_expr("t") == "COALESCE(NULLIF(t.album_artist, ''), t.artist)"


def test_where_in_handles_an_empty_list():
    assert db.where_in("id", []) == ("0", [])
    sql, args = db.where_in("id", ["a", "b"])
    assert sql == "id IN (?, ?)"
    assert args == ["a", "b"]


def test_coverage_shape_and_empty_library():
    assert db.coverage(1, 2) == {
        "known": 1, "total": 2, "remaining": 1, "percent": 50.0,
    }
    assert db.coverage(0, 0)["percent"] == 0.0


def test_track_lookups(tmp_path):
    conn = navidrome.connect(tmp_path / "index.db")
    with conn:
        conn.execute(
            "INSERT INTO tracks (id, uri, title, artist, album) VALUES (?,?,?,?,?)",
            ("a", "http://navidrome/a.flac", "A", "Band", "Record"),
        )

    assert tracks.track_by_id(conn, "a")["title"] == "A"
    assert tracks.track_by_id(conn, "") is None
    assert [row["id"] for row in tracks.tracks_by_ids(conn, ["a", ""])] == ["a"]
    assert tracks.tracks_by_ids(conn, []) == []
    assert tracks.library_size(conn) == 1
    assert tracks.album_name(conn, "missing") == ""


def test_label_queries_relabel_and_delete(tmp_path):
    conn = navidrome.connect(tmp_path / "index.db")
    with conn:
        conn.execute(
            "INSERT INTO tracks (id, uri, title, artist) VALUES (?,?,?,?)",
            ("a", "http://navidrome/a.flac", "A", "Band"),
        )
        labels.insert(conn, [("a", "Nu Disco")], "tag")

    assert labels.by_track(conn) == {"a": ["Nu Disco"]}

    with conn:
        labels.relabel(conn, "Electronic", "Nu Disco")
    assert labels.by_track(conn) == {"a": ["Electronic"]}

    with conn:
        labels.delete_for_tracks(conn, ["a"], "electronic")
    assert labels.by_track(conn) == {}
