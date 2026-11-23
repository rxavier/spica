"""The admin signing secret: created once, exclusively, and private."""

from __future__ import annotations

import os
import stat

from spica import auth, config


def test_the_session_secret_is_created_exclusively(tmp_path, monkeypatch):
    """Two processes can race to create it; exclusive create means only one wins."""
    path = tmp_path / ".session-secret"
    monkeypatch.setattr(config, "SESSION_SECRET_FILE", path)

    flags_seen = []
    real_open = os.open

    def spy(p, flags, mode):
        flags_seen.append(flags)
        return real_open(p, flags, mode)

    monkeypatch.setattr(auth.os, "open", spy)

    auth._secret()

    assert flags_seen and flags_seen[0] & os.O_EXCL


def test_the_session_secret_file_is_private(tmp_path, monkeypatch):
    path = tmp_path / ".session-secret"
    monkeypatch.setattr(config, "SESSION_SECRET_FILE", path)

    auth._secret()

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode & 0o077 == 0, "the signing key must not be group/world readable"
