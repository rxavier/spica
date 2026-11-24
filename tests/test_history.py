"""The ledger's grouping: a DJ set is one block, rotation is not part of it."""

from __future__ import annotations

from spica.web.shared import SOURCE_LABELS, history_blocks


def row(artist: str, title: str, source: str, why: str = "", stamp: str = "") -> dict:
    return {
        "artist": artist,
        "title": title,
        "source": source,
        "source_raw": source,
        "is_dj": source in ("dj", "llm") and bool(why),
        "why": why,
        "stamp": stamp,
        "clock": stamp[11:16],
    }


def test_contiguous_dj_picks_with_one_line_are_a_set():
    rows = [
        row("B", "two", "dj", "Same line", "2026-11-24T18:10:00Z"),
        row("A", "one", "dj", "Same line", "2026-11-24T18:05:00Z"),
        row("C", "rotation", "fallback_rotation", "", "2026-11-24T18:00:00Z"),
    ]
    blocks = history_blocks(rows)
    assert [block["kind"] for block in blocks] == ["set", "track"]
    assert blocks[0]["count"] == 2
    assert blocks[0]["start"] == "18:05"
    assert blocks[0]["end"] == "18:10"
    assert blocks[0]["key"] == "2026-11-24T18:05:00Z"


def test_rotation_or_a_new_line_ends_the_set():
    rows = [
        row("B", "two", "dj", "Next line", "2026-11-24T18:15:00Z"),
        row("A", "one", "fallback_rotation", "", "2026-11-24T18:10:00Z"),
        row("C", "three", "dj", "First line", "2026-11-24T18:05:00Z"),
    ]
    blocks = history_blocks(rows)
    assert [block["kind"] for block in blocks] == ["track", "track", "track"]


def test_a_single_dj_pick_is_a_plain_row():
    rows = [row("A", "one", "dj", "Only one", "2026-11-24T18:05:00Z")]
    blocks = history_blocks(rows)
    assert blocks[0]["kind"] == "track"


def test_the_ui_calls_the_picker_the_dj():
    assert SOURCE_LABELS["dj"] == "DJ pick"
    assert SOURCE_LABELS["fallback_rotation"] == "Rotation"
