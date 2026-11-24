"""One replace-based write for every small text file."""

from __future__ import annotations

import stat

from spica import atomic


def test_an_atomic_write_replaces_the_file_without_leaving_a_temp(tmp_path):
    path = tmp_path / "state" / "station.txt"
    atomic.atomic_write(path, "new contents\n", mode=0o600)

    assert path.read_text(encoding="utf-8") == "new contents\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not atomic.temp_path(path).exists()
