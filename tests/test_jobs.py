"""Job kinds: one registry, and nothing ships half-registered."""

from __future__ import annotations

import re
from pathlib import Path

from spica import jobs, worker


def test_every_kind_has_a_handler():
    assert set(worker.HANDLERS) == set(jobs.KINDS)
    assert all(jobs.KIND_LABELS[kind] for kind in jobs.KINDS)
    assert jobs.SLICED <= set(jobs.KINDS)


def test_the_admin_buttons_only_name_real_kinds():
    template = Path(__file__).resolve().parent.parent / "spica" / "templates" / "admin_library.html"
    text = template.read_text(encoding="utf-8")
    kinds = set(re.findall(r'name="kind" value="([^"]+)"', text))
    assert kinds, "the library page should still have job buttons"
    assert kinds <= set(jobs.KINDS), f"unknown job kind(s) in the admin page: {kinds - set(jobs.KINDS)}"


def test_the_due_helper_shares_the_pending_and_failure_rules(monkeypatch):
    import time

    from spica import worker

    monkeypatch.setattr(worker.job_queries, "pending", lambda conn, kind: {"id": 1})
    assert worker._due(None, "analysis", True, lambda: 1) is False

    monkeypatch.setattr(worker.job_queries, "pending", lambda conn, kind: None)
    monkeypatch.setattr(
        worker.jobs, "last",
        lambda conn, kind: {"status": "failed", "finished_at": str(int(time.time()))},
    )
    assert worker._due(None, "analysis", True, lambda: 1) is False

    monkeypatch.setattr(worker.jobs, "last", lambda conn, kind: None)
    assert worker._due(None, "analysis", True, lambda: 1) is True
    assert worker._due(None, "analysis", True, lambda: 0) is False
    assert worker._due(None, "analysis", False, lambda: 1) is False
