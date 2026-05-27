"""Tests for lib/schema.py.

These cover the structural choices made post-dual-voice-review:
- 3-field score with explicit None abstention
- 3-state ambiguity (no `unique`)
- bound_anchors as the anti-hallucination defense
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lib.schema import (
    EmailCandidate,
    Employer,
    Person,
    ResolverResult,
    Source,
)


def _now():
    return datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)


def test_source_is_frozen():
    s = Source(type="git_commit", url="https://github.com/x/y/commit/abc",
               observed_at=_now(), detail="commit in x/y")
    with pytest.raises(Exception):
        # frozen dataclass: assignment must fail
        s.url = "https://example.com"  # type: ignore[misc]


def test_email_candidate_defaults_to_full_abstention():
    """All three score fields default to None — abstain until evidence arrives.

    This is the contract: an EmailCandidate with no sources and no SMTP probe
    must produce None on every field, NOT 0.0. Zero would imply 'measured
    and known to be bad' which is different from 'never evaluated.'"""
    c = EmailCandidate(address="x@example.com")
    assert c.belongs_to_person is None
    assert c.current_work_address is None
    assert c.deliverable is None
    assert c.smtp_verdict == "unprobed"
    assert c.sources == []
    assert c.score_reasons == []


def test_email_candidate_holds_three_fields_independently():
    c = EmailCandidate(
        address="pete@openai.com",
        belongs_to_person=0.85,
        current_work_address=0.70,
        deliverable=None,  # SMTP inconclusive on M365 → abstain
    )
    assert c.belongs_to_person == 0.85
    assert c.current_work_address == 0.70
    assert c.deliverable is None


def test_person_default_ambiguity_is_insufficient_evidence():
    """A Person constructed with no anchors must default to the safe state.
    `single_plausible_match` would be a false-confidence default."""
    p = Person(name="Test Target")
    assert p.ambiguity == "insufficient_identity_evidence"
    assert p.bound_anchors == []
    assert p.ambiguity_candidates == []


def test_person_supports_three_ambiguity_states():
    for state in (
        "single_plausible_match",
        "multiple_plausible_matches",
        "insufficient_identity_evidence",
    ):
        p = Person(name="X", ambiguity=state)  # type: ignore[arg-type]
        assert p.ambiguity == state


def test_person_bound_anchors_records_independent_evidence():
    """Each anchor is a (type, value) tuple. ≥2 distinct anchors bind a handle."""
    p = Person(
        name="Peter Steinberger",
        handles={"github": "steipete"},
        bound_anchors=[
            ("name_match", "Peter Steinberger"),
            ("github_repo_owner", "steipete"),
        ],
    )
    assert len(p.bound_anchors) == 2
    assert ("github_repo_owner", "steipete") in p.bound_anchors


def test_employer_minimal_construction():
    e = Employer(name="OpenAI", domains=["openai.com"])
    assert e.since is None
    assert e.until is None


def test_resolver_result_can_express_timeout():
    """The pipeline needs to distinguish empty-result from timeout-result so the
    renderer can surface 'degraded run' to the user."""
    r = ResolverResult(
        resolver="personal_site",
        candidates=[],
        status="timeout",
        elapsed_ms=5000,
        error_detail="future.result(timeout=5) hit deadline",
    )
    assert r.status == "timeout"
    assert r.candidates == []
    assert r.elapsed_ms == 5000


def test_resolver_result_ok_with_candidates():
    cand = EmailCandidate(address="pete@openai.com")
    r = ResolverResult(resolver="git_emails", candidates=[cand], status="ok", elapsed_ms=200)
    assert r.status == "ok"
    assert len(r.candidates) == 1
