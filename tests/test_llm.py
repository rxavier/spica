"""Reading a reply out of whatever the model wrapped it in, and trimming tool results."""

from __future__ import annotations

import json

from spica.picker import llm


def test_a_bare_object_is_found():
    assert llm.json_object('{"theme": "t", "ids": ["a"]}') == {"theme": "t", "ids": ["a"]}


def test_a_fenced_object_is_found():
    assert llm.json_object('```json\n{"ids": ["a"]}\n```') == {"ids": ["a"]}


def test_the_first_object_with_ids_wins():
    text = '{"note": "thinking"}\n{"ids": ["a", "b"]}'
    assert llm.json_object(text) == {"ids": ["a", "b"]}


def test_an_object_without_ids_is_not_an_answer():
    assert llm.json_object('{"theme": "t"}') is None


def test_prose_is_not_an_answer():
    assert llm.json_object("nothing here") is None


def test_the_old_single_track_shape_still_reads():
    """An install still on an older prompt answers with track_id."""
    found = llm.json_object('{"track_id": "abc", "why": "x"}')
    assert found is not None and found["track_id"] == "abc"


def test_a_small_tool_result_is_untouched():
    payload = {"results": [{"id": "a"}]}
    assert json.loads(llm.fit(payload)) == payload


def test_a_large_tool_result_is_trimmed_by_whole_rows():
    rows = [{"id": f"{i:022d}", "title": "x" * 60} for i in range(400)]
    text = llm.fit({"matching": 400, "results": rows}, limit=2000)
    trimmed = json.loads(text)
    assert len(text) <= 2000
    assert trimmed["results"], "trimming must not empty the answer"
    assert trimmed["matching"] == 400
    assert all(len(row["id"]) == 22 for row in trimmed["results"])


def test_a_large_list_is_trimmed_to_a_prefix():
    rows = [{"id": f"{i:022d}", "title": "x" * 60} for i in range(400)]
    trimmed = json.loads(llm.fit(rows, limit=1500))
    assert isinstance(trimmed, list)
    assert trimmed[0]["id"] == f"{0:022d}"


def test_too_much_with_no_rows_says_so_instead_of_cutting():
    payload = {"note": "x" * 5000}
    text = llm.fit(payload, limit=100)
    assert json.loads(text)["results"] == []
