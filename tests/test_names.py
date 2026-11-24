"""The picker's shared identity helpers."""

from __future__ import annotations

from spica.names import seed_key


def test_a_seed_key_is_stable_within_a_seed_and_scoped_to_it():
    assert seed_key(7, "a") == seed_key(7, "a")
    assert seed_key(7, "a") != seed_key(8, "a")
    assert seed_key(7, "a") != seed_key(7, "b")
