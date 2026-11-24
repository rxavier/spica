"""The query layer: shared SQL fragments and the track lookups built on them."""

from __future__ import annotations

from pathlib import Path

from spica import db, navidrome
from spica.queries import enrichment, jobs, labels, tracks
from tests.factories import insert_track


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
        insert_track(
            conn, id="a", uri="http://navidrome/a.flac", title="A",
            artist="Band", album="Record",
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
        insert_track(
            conn, id="a", uri="http://navidrome/a.flac", title="A", artist="Band",
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


def test_jobs_coverage_is_counted_once_per_few_seconds(tmp_path, monkeypatch):
    from spica import jobs

    conn = navidrome.connect(tmp_path / "index.db")
    calls: list[int] = []
    real = jobs.job_queries.coverage_counts
    monkeypatch.setattr(
        jobs.job_queries, "coverage_counts",
        lambda c: calls.append(1) or real(c),
    )
    monkeypatch.setattr(jobs, "_coverage_cache", None)

    first = jobs.coverage(conn)
    first["analysis"] = {"added": True}
    second = jobs.coverage(conn)

    assert len(calls) == 1
    assert "analysis" not in second, "the caller's additions do not enter the cache"


def test_open_store_is_optional_and_creates_when_asked(tmp_path):
    path = tmp_path / "optional.db"
    assert db.open_store(path) is None
    assert db.open_store(path, readonly=True) is None

    ensured: list[int] = []
    conn = db.open_store(path, create=True, ensure=lambda c: ensured.append(1))
    assert conn is not None
    assert ensured == [1]
    assert path.exists()
    conn.close()


def test_open_store_migrates_each_file_once_per_process(tmp_path):
    from spica import db as db_mod

    path = tmp_path / "once.db"
    calls: list[int] = []
    for _ in range(2):
        conn = db_mod.open_store(path, create=True, ensure=lambda c: calls.append(1))
        conn.close()
    assert calls == [1]

    path.unlink()
    conn = db_mod.open_store(path, create=True, ensure=lambda c: calls.append(1))
    conn.close()
    assert calls == [1, 1], "a new file is a new store and gets its schema"
