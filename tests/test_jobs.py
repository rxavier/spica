"""Job kinds: one registry, and nothing ships half-registered."""

from __future__ import annotations

import re
from pathlib import Path

from spica import jobs, worker


def test_every_kind_has_a_handler():
    assert set(worker.HANDLERS) == set(jobs.KINDS)
    assert all(jobs.KIND_LABELS[kind] for kind in jobs.KINDS)


def test_the_admin_buttons_only_name_real_kinds():
    template = Path(__file__).resolve().parent.parent / "spica" / "templates" / "admin_library.html"
    text = template.read_text(encoding="utf-8")
    kinds = set(re.findall(r'name="kind" value="([^"]+)"', text))
    assert kinds, "the library page should still have job buttons"
    assert kinds <= set(jobs.KINDS), f"unknown job kind(s) in the admin page: {kinds - set(jobs.KINDS)}"
