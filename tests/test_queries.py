"""The query layer: shared SQL fragments and the track lookups built on them."""

from __future__ import annotations

from pathlib import Path

from spica import db, navidrome
from spica.queries import enrichment, jobs, labels, tracks


def test_sql_lives_only_in_the_query_modules():
    root = Path(__file__).resolve().parent.parent / "spica"
    offenders = []
    for path in root.rglob("*.py"):
        if "queries" in path.relative_to(root).parts:
            continue
        text = path.read_text(encoding="utf-8")
        for marker in (".execute(", ".executemany(", ".executescript("):
            if marker in text:
                offenders.append(f"{path.relative_to(root)}{marker}")
    assert not offenders, f"SQL outside spica/queries: {offenders}"


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


def test_job_lifecycle(tmp_path):
    conn = navidrome.connect(tmp_path / "index.db")
    with conn:
        job_id = jobs.insert(conn, "refresh", 0)

    assert jobs.pending(conn, "refresh")["id"] == job_id
    with conn:
        claimed = jobs.claim(conn, 1)
    assert (claimed["id"], claimed["status"]) == (job_id, "running")
    with conn:
        jobs.finish(conn, job_id, 2, "{}")
    assert jobs.get(conn, job_id)["status"] == "done"
    assert jobs.recent(conn, 1)[0]["id"] == job_id


def test_enrichment_caches_roundtrip(tmp_path):
    conn = navidrome.connect(tmp_path / "index.db")
    with conn:
        enrichment.remember_mbid(conn, "Band", "mbid-1", "navidrome", 1)
        enrichment.remember_scores(conn, "Band", [("Other", 0.5)], "lastfm", 2)
        enrichment.replace_popularity(
            conn, [("a", 10, 1, "lastfm", 3)]
        )

    assert enrichment.cached_mbid(conn, "Band") == "mbid-1"
    assert enrichment.cached_scores(conn, "Band") == [("Other", 0.5)]
    assert enrichment.popularity(conn)[0]["playcount"] == 10
