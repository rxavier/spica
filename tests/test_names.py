"""The three normalisations, each answering a different question."""

from __future__ import annotations

from spica import names


def test_a_seed_key_is_stable_within_a_seed_and_scoped_to_it():
    assert names.seed_key(7, "a") == names.seed_key(7, "a")
    assert names.seed_key(7, "a") != names.seed_key(8, "a")
    assert names.seed_key(7, "a") != names.seed_key(7, "b")


def test_name_key_is_identity_not_title_mangling():
    assert names.name_key("L’Impératrice") == names.name_key("l'imperatrice")
    assert names.name_key("blink‐182") == names.name_key("blink-182")
    assert names.name_key("  A   Band ") == "a band"


def test_title_keys_keep_the_punctuation_out():
    assert names.norm("Jane’s Addiction") == "janesaddiction"
    assert names.norm_loose("Fikołek") == "fikolek"
    assert names.wildcard_match("Fiko*ek", "Fikołek")
