"""Shared fixtures.

Every test runs against a temporary state directory. The station's paths are
properties over `config.STATE_DIR`, so pointing that at `tmp_path` is enough to keep
a test from touching the real `state/` — which is the one thing a test suite for this
code must never do, since that directory is the station that is on air.
"""

from __future__ import annotations

import pytest

from spica import config


@pytest.fixture
def station(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    return config.Station(id="test", name="Test station")


@pytest.fixture
def other_station(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    return config.Station(id="other", name="Another station")
