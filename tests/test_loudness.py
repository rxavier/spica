"""Per-track gain: the arithmetic, the tag, and the request annotation.

The files are not consulted. What is pinned here is what the station does with a
ReplayGain tag, including the two cases that keep a boost safe: no peak means no
lift, and a peak near full scale means no room to lift.
"""

from __future__ import annotations

import pytest

from spica import config, liqgen, loudness, navidrome, pool, subsonic
from spica.picker.library import Library
from tests.factories import insert_track, insert_tracks

def test_replaygain_tag_becomes_loudness_and_peak():
    lufs, peak = loudness.from_replaygain({"trackGain": 0, "trackPeak": 1.0})
    assert lufs == loudness.REPLAYGAIN_REFERENCE_LUFS
    assert peak == pytest.approx(0.0)

    lufs, peak = loudness.from_replaygain({"trackGain": 6, "trackPeak": 0.5})
    assert lufs == pytest.approx(-24.0)
    assert peak == pytest.approx(-6.02, abs=0.01)

    assert loudness.from_replaygain({"trackGain": 6}) == (-24.0, None)
    assert loudness.from_replaygain(None) == (None, None)
    assert loudness.from_replaygain({"trackGain": "6"}) == (None, None)


def test_no_loudness_means_no_gain():
    assert loudness.gain_db(None, None) is None


def test_gain_is_clamped_and_boosts_need_a_peak():
    # On target: nothing to do.
    assert loudness.gain_db(-14.0, None) == 0.0
    # A loud track is turned down even with no peak.
    assert loudness.gain_db(-8.0, None) == -6.0
    # Cuts stop at the clamp.
    assert loudness.gain_db(-1.0, None) == -12.0
    # A quiet track with no peak is not lifted at all.
    assert loudness.gain_db(-24.0, None) == 0.0
    # With a peak, the lift is the smallest of what it wants, the cap, and the
    # headroom to the ceiling.
    assert loudness.gain_db(-24.0, -7.0) == 6.0
    assert loudness.gain_db(-24.0, -3.0) == 2.0
    assert loudness.gain_db(-24.0, -0.5) == 0.0


def test_gain_is_put_on_the_request_as_liq_amplify():
    uri = "http://navidrome/rest/stream?id=abc"
    assert loudness.annotate(uri, None) == uri
    assert loudness.annotate(uri, 3.0) == 'annotate:liq_amplify="3.0 dB":' + uri
    assert loudness.annotate(uri, -2.5) == 'annotate:liq_amplify="-2.5 dB":' + uri
    assert loudness.annotate("", 3.0) == ""


def test_song_id_survives_the_annotation():
    plain = "http://navidrome:4533/rest/stream?id=abc123&u=radio"
    assert subsonic.song_id(plain) == "abc123"
    assert subsonic.song_id(f'annotate:liq_amplify="3.0 dB":{plain}') == "abc123"


def test_loudness_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "LOUDNESS", False)
    assert loudness.for_tag(6, 0.5) is None


def test_picker_hands_the_mixer_an_annotated_uri(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    conn = navidrome.connect(tmp_path / "index.db")
    with conn:
        insert_track(
            conn, id="t1", uri="http://navidrome/rest/stream?id=t1", title="First",
            artist="Band", album="Record", album_id="al1",
            replaygain_gain=6, replaygain_peak=0.5,
        )
        insert_track(
            conn, id="t2", uri="http://navidrome/rest/stream?id=t2", title="Second",
            artist="Band", album="Record", album_id="al1",
        )
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM tracks")}
    lib = Library(conn)

    tagged = lib._full(rows["t1"])
    assert tagged.uri.startswith('annotate:liq_amplify="')
    assert subsonic.song_id(tagged.uri) == "t1"

    untagged = lib._full(rows["t2"])
    assert untagged.uri == "http://navidrome/rest/stream?id=t2"


def test_the_loudness_store_is_read_once_per_picker(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "LOUDNESS", True)
    (tmp_path / "loudness.db").write_bytes(b"")
    conn = navidrome.connect(tmp_path / "index.db")
    with conn:
        insert_track(
            conn, id="t1", uri="http://navidrome/stream?id=t1", title="First", artist="Band",
        )

    calls = []

    def once():
        calls.append(1)
        return {}

    monkeypatch.setattr(loudness, "all_measured", once)
    lib = Library(conn)
    lib.by_id("t1")
    lib.by_id("t1")

    assert len(calls) == 1


def test_mixer_applies_the_gain_before_the_crossfade():
    station = config.Station(id="alt", name="Alt")
    script = liqgen.render([station], "source-password")
    gain = 'amplify(override="liq_amplify", 1., s_alt)'
    cross = "crossfade(duration=3.0, s_alt)"
    assert gain in script
    assert script.index(gain) < script.index(cross)


def test_the_fallback_pool_is_levelled_too(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    station = config.Station(id="alt", name="Alt")
    conn = navidrome.connect(tmp_path / "index.db")
    with conn:
        insert_track(
            conn, id="t1", uri="http://navidrome/rest/stream?id=t1", title="First",
            artist="Band", album="Record", album_id="al1",
            replaygain_gain=6, replaygain_peak=0.5,
        )
    pool.write_pools(conn, [station], log=lambda *args, **kwargs: None)

    lines = station.pool_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert lines[0].startswith('annotate:liq_amplify="')
    assert subsonic.song_id(lines[0]) == "t1"


def test_rebuild_keeps_the_replaygain_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    album = {"id": "al1", "name": "Record", "artist": "Band", "year": 2001}
    song = {
        "id": "t1", "title": "First", "artist": "Band", "album": "Record",
        "albumId": "al1", "duration": 200,
        "replayGain": {"trackGain": 6.0, "trackPeak": 0.5},
    }
    monkeypatch.setattr(subsonic, "iter_albums", lambda page=500: iter([album]))
    monkeypatch.setattr(subsonic, "album_songs", lambda album_id: [song])
    monkeypatch.setattr(
        subsonic, "stream_url", lambda track_id: f"http://navidrome/rest/stream?id={track_id}"
    )

    conn = navidrome.connect(tmp_path / "index.db")
    navidrome.rebuild_index(conn, log=lambda *args, **kwargs: None)

    row = conn.execute("SELECT * FROM tracks WHERE id = 't1'").fetchone()
    assert row["replaygain_gain"] == pytest.approx(6.0)
    assert row["replaygain_peak"] == pytest.approx(0.5)


def test_parse_summary_takes_the_summary_not_the_progress_lines():
    stderr = """\
[Parsed_ebur128_0 @ 0x1] t: 1.0  TARGET:-23 LUFS  M: -20.0 S: -20.0  I: -70.0 LUFS  LRA: 0.0 LU
[Parsed_ebur128_0 @ 0x1] t: 200.0  TARGET:-23 LUFS  M: -10.0 S: -10.0  I: -9.5 LUFS  LRA: 4.2 LU

Summary:

  Integrated loudness:
    I:          -9.5 LUFS
    Threshold: -19.5 LUFS

  True peak:
    Peak:       -1.2 dBFS
"""
    assert loudness.parse_summary(stderr) == (-9.5, -1.2)
    with pytest.raises(ValueError):
        loudness.parse_summary("Summary:\n  Peak: -1.0 dBFS")


def test_measured_gain_is_the_fallback_and_the_tag_wins():
    row = {"replaygain_gain": None, "replaygain_peak": None}
    # -20 LUFS wants +6, but a -3 dBFS peak leaves only 2 dB of headroom.
    assert loudness.for_row(row, (-20.0, -3.0)) == 2.0
    tagged = {"replaygain_gain": 0.0, "replaygain_peak": 0.5}  # -18 LUFS, -6.02 dBFS
    assert loudness.for_row(tagged, (-30.0, -3.0)) == 4.0
    assert loudness.for_row(row, None) is None


def test_measured_store_round_trip_and_backlog(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    conn = navidrome.connect(tmp_path / "index.db")
    with conn:
        insert_tracks(
            conn,
            [
                ("t1", "http://navidrome/1", "Tagged", "Band", "Record", "al1", 6.0),
                ("t2", "http://navidrome/2", "Untagged", "Band", "Record", "al1", None),
                ("t3", "http://navidrome/3", "Also", "Band", "Record", "al1", None),
            ],
            columns=("id", "uri", "title", "artist", "album", "album_id", "replaygain_gain"),
        )

    assert [r["id"] for r in loudness.pending(conn, 10)] == ["t2", "t3"]
    assert loudness.backlog(conn) == 2

    store = loudness.connect(create=True)
    loudness.save(store, "t2", -20.0, -3.0)
    store.close()
    assert loudness.all_measured()["t2"] == (-20.0, -3.0)
    assert loudness.all_measured()["t2"] == (-20.0, -3.0)
    assert [r["id"] for r in loudness.pending(conn, 10)] == ["t3"]

    store = loudness.connect(create=True)
    loudness.save(store, "t3", None, None, "could not decode")
    store.close()
    assert loudness.pending(conn, 10) == []
    assert loudness.backlog(conn) == 0

    cover = loudness.coverage(conn)
    assert cover["tagged"] == 1
    assert cover["measured"] == 1
    assert cover["known"] == 2
    assert cover["errors"] == 1
    assert cover["remaining"] == 1


def test_loudness_job_measures_untagged_tracks_and_marks_failures(tmp_path, monkeypatch):
    from spica import jobs, worker

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "LOUDNESS_SLICE", 10)
    monkeypatch.setattr(config, "LOUDNESS_WORKERS", 1)
    monkeypatch.setattr(subsonic, "stream_url", lambda track_id: f"file:///{track_id}")
    monkeypatch.setattr(loudness, "measure_available", lambda: True)

    def fake_measure(url):
        if url.endswith("t3"):
            raise RuntimeError("boom")
        return (-20.0, -3.0)

    monkeypatch.setattr(loudness, "measure", fake_measure)

    conn = navidrome.connect(tmp_path / "index.db")
    with conn:
        insert_tracks(
            conn,
            [
                ("t1", "http://navidrome/1", "Tagged", "Band", "Record", "al1", 6.0),
                ("t2", "http://navidrome/2", "Untagged", "Band", "Record", "al1", None),
                ("t3", "http://navidrome/3", "Broken", "Band", "Record", "al1", None),
            ],
            columns=("id", "uri", "title", "artist", "album", "album_id", "replaygain_gain"),
        )
    jobs.ensure_schema(conn)
    job_id = jobs.create(conn, "loudness")

    rewritten = []
    monkeypatch.setattr(worker.config, "load_stations", lambda: [config.Station(id="test", name="Test")])
    monkeypatch.setattr(
        worker.pool, "write_pools",
        lambda conn, stations, log=print: rewritten.append(True) or {"test": 1},
    )
    monkeypatch.setattr(
        worker.pool, "resume_after_last_played",
        lambda stations, last, log=print: None,
    )

    result = worker.run_loudness(conn, job_id, log=lambda *args, **kwargs: None)
    assert result["tracks"] == 1
    assert result["failed"] == 1
    assert result["remaining"] == 0
    assert rewritten == [True]
    assert loudness.all_measured()["t2"] == (-20.0, -3.0)
    assert "t3" not in loudness.all_measured()
    store = loudness.connect()
    try:
        measured_ids = {row["track_id"] for row in store.execute("SELECT track_id FROM track_loudness")}
    finally:
        store.close()
    assert "t1" not in measured_ids


def test_the_loudness_job_honours_the_measure_opt_out(tmp_path, monkeypatch):
    """SPICA_LOUDNESS_MEASURE=0 means tags only, even for a manually queued job."""
    from spica import jobs, worker

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "LOUDNESS_MEASURE", False)
    conn = navidrome.connect(tmp_path / "index.db")
    with conn:
        insert_track(
            conn, id="t1", uri="http://navidrome/1", title="Untagged", artist="Band",
            album="Record", album_id="al1",
        )
    jobs.ensure_schema(conn)
    job_id = jobs.create(conn, "loudness")

    probes: list[int] = []
    monkeypatch.setattr(loudness, "measure_available", lambda: probes.append(1) or True)

    result = worker.run_loudness(conn, job_id, log=lambda *args, **kwargs: None)

    assert result["tracks"] == 0
    assert "measurement is off" in result["note"]
    assert probes == []
