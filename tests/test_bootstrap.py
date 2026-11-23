"""First run writes the config a fresh install needs, without exposing secrets."""

from __future__ import annotations

import stat

from spica import bootstrap, config


def test_icecast_file_is_private(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    bootstrap.ensure(verbose=False)
    mode = stat.S_IMODE((tmp_path / "icecast.xml").stat().st_mode)
    assert mode & 0o077 == 0, "icecast.xml holds passwords and must not be group/world readable"
