"""Shared builders for the lib.pipeline unit tests.

These tests import lib.pipeline.* DIRECTLY (not via the snoop re-export shim), so
they pin the extracted modules' contracts as standalone units. They complement —
not duplicate — the entry-point tests in tests/test_snoop_entry.py, which drive
the same logic through the monkeypatch-coupled harness.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from lib.schema import EmailCandidate, Person, Source

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


def src(type_: str, *, url: str | None = None, detail: str | None = None,
        days_ago: int = 10) -> Source:
    return Source(type=type_, url=url, observed_at=NOW - timedelta(days=days_ago),
                  detail=detail or f"{type_} source")


def cand(address: str, *sources: Source, **flags: Any) -> EmailCandidate:
    """An EmailCandidate from an address + sources, with any verdict/flag kwargs
    (employer_match=, account_exists=, is_personal_provider=, ...)."""
    return EmailCandidate(address=address, sources=list(sources), **flags)


def person(**kw: Any) -> Person:
    return Person(**kw)
