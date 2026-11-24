"""Reading the mixer's now-playing file."""

from __future__ import annotations

from spica import mixer


def test_now_tail_returns_a_whole_line(tmp_path):
    station = type("S", (), {"now_file": tmp_path / "now.txt"})()
    station.now_file.write_bytes(b"short\n" + b"x" * 5000 + b"\nlast line\n")
    assert mixer.now_tail(station) == "last line"

    station.now_file.write_bytes(b"first\nsecond partial")
    assert mixer.now_tail(station) == "first"
