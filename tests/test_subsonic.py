"""The one-per-installation Subsonic salt."""

from __future__ import annotations

import stat

from spica import config, subsonic


def test_the_salt_is_created_once_and_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(subsonic, "_SALT", None)

    first = subsonic._salt()
    assert len(first) == 16
    assert stat.S_IMODE((tmp_path / ".subsonic-salt").stat().st_mode) == 0o600

    monkeypatch.setattr(subsonic, "_SALT", None)
    assert subsonic._salt() == first


def test_an_empty_salt_file_is_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(subsonic, "_SALT", None)
    (tmp_path / ".subsonic-salt").write_text("", encoding="utf-8")

    salt = subsonic._salt()
    assert len(salt) == 16
    assert (tmp_path / ".subsonic-salt").read_text(encoding="utf-8").strip() == salt
