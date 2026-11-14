"""The two lines drawn from what actually aired.

These test the arithmetic, not the model: which names are eligible, which one is named
as overplayed, and that neither list is weighted towards anybody.
"""

from __future__ import annotations

import random

from spica import picker


def library(artists, played=()):
    """A Library with only the two lookups fresh_artists needs stubbed out."""
    lib = picker.Library(conn=None)
    lib.roster = lambda station, limit=60: {
        "artists": [(name, 5) for name in artists],
        "artists_total": len(artists),
    }
    lib.recent_labels = lambda station, n=5: ([], list(played))
    return lib


def test_artists_who_just_played_are_not_offered(station):
    lib = library(["A", "B", "C"], played=["A", "B"])
    assert lib.fresh_artists(station, k=5) == ["C"]


def test_the_draw_is_a_sample_of_the_eligible_names(station):
    names = [f"artist {i}" for i in range(40)]
    lib = library(names, played=["artist 0"])
    drawn = lib.fresh_artists(station, k=10)
    assert len(drawn) == 10
    assert "artist 0" not in drawn
    assert len(set(drawn)) == 10


def test_the_draw_moves_between_calls(station):
    lib = library([f"artist {i}" for i in range(30)])
    draws = {tuple(lib.fresh_artists(station, k=10)) for _ in range(20)}
    assert len(draws) > 1, "the same ten names every time would be a static list"


def test_no_weighting_between_names(station):
    """Rough check that nobody is favoured: every name lands near k/n."""
    names = [f"artist {i}" for i in range(20)]
    lib = library(names)
    counts = {name: 0 for name in names}
    trials = 2000
    random.seed(1234)
    for _ in range(trials):
        for name in lib.fresh_artists(station, k=10):
            counts[name] += 1
    rates = [c / trials for c in counts.values()]
    expected = 10 / len(names)
    assert abs(sum(rates) / len(rates) - expected) < 0.02
    assert max(rates) < expected * 1.4
    assert min(rates) > expected * 0.6


def test_names_are_folded_before_comparing(station):
    """L'Impératrice and L’Impératrice are one artist, not two eligible names."""
    lib = library(["L’Impératrice", "Other"], played=["l'impératrice"])
    assert lib.fresh_artists(station, k=5) == ["Other"]


def test_the_artist_that_played_most_is_named(station):
    lib = library(["A", "B"], played=["A", "A", "A", "B"])
    assert lib.heavy_artist(station) == ("A", 3)


def test_one_play_is_not_a_corner(station):
    lib = library(["A", "B"], played=["A", "B"])
    assert lib.heavy_artist(station) is None


def test_an_empty_history_says_nothing(station):
    lib = library(["A", "B"])
    assert lib.heavy_artist(station) is None
    assert len(lib.fresh_artists(station, k=2)) == 2
