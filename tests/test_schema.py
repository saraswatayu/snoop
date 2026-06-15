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


def test_email_candidate_defaults_to_unprobed_no_sources():
    """A bare EmailCandidate is a raw slot: unprobed verdicts, no sources. The
    host model scores it; snoop carries only the readings."""
    c = EmailCandidate(address="x@example.com")
    assert c.smtp_verdict == "unprobed"
    assert c.account_exists == "unprobed"
    assert c.sources == []


def test_email_candidate_carries_verdicts_and_domain_facts():
    c = EmailCandidate(
        address="pete@openai.com",
        smtp_verdict="verified",
        account_exists="verified",
        account_display_name="Pete S",
        employer_match=True,
    )
    assert c.smtp_verdict == "verified"
    assert c.account_exists == "verified"
    assert c.account_display_name == "Pete S"
    assert c.employer_match is True


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


def test_resolver_result_defaults():
    """A resolver result with only the required fields defaults cleanly."""
    r = ResolverResult(resolver="gh_profile", candidates=[], status="empty")
    assert r.elapsed_ms is None
    assert r.error_detail is None


# ---- RunRecord / sensor status -----------------------------------------------


def test_sensor_status_mapping():
    from lib.schema import sensor_status_of
    assert sensor_status_of("ok") == "ran"
    assert sensor_status_of("empty") == "ran"
    assert sensor_status_of("timeout") == "degraded"
    assert sensor_status_of("unavailable") == "degraded"
    assert sensor_status_of("error") == "degraded"


def test_runrecord_from_resolver_ran_with_candidates():
    from lib.schema import RunRecord
    r = ResolverResult(resolver="git_emails",
                       candidates=[EmailCandidate(address="a@b.com")],
                       status="ok", elapsed_ms=120)
    rec = RunRecord.from_resolver(r)
    assert rec.sensor == "git_emails"
    assert rec.status == "ran"
    assert rec.outcome == "candidates"
    assert rec.elapsed_ms == 120


def test_runrecord_from_resolver_degraded_carries_reason():
    from lib.schema import RunRecord
    r = ResolverResult(resolver="personal_site", candidates=[], status="timeout",
                       elapsed_ms=5000, error_detail="exceeded 5s budget")
    rec = RunRecord.from_resolver(r)
    assert rec.status == "degraded"
    assert rec.outcome == "timeout"
    assert rec.reason == "exceeded 5s budget"


def test_deadline_exceeded_reason_distinct_from_internal_timeout():
    """2B: a sensor abandoned at the SHARED wall-clock deadline and a sensor that
    hit its OWN internal timeout both degrade with outcome='timeout', but their
    `reason` text must stay distinguishable — the host should be able to tell
    'snoop ran out of budget' from 'this sensor's socket timed out'."""
    from lib.schema import RunRecord
    internal = RunRecord.from_resolver(ResolverResult(
        resolver="personal_site", candidates=[], status="timeout",
        error_detail="socket read timed out after 5s"))
    deadline = RunRecord.from_resolver(ResolverResult(
        resolver="git_emails", candidates=[], status="timeout",
        error_detail="deadline-exceeded: abandoned after 60s shared budget"))
    assert internal.outcome == deadline.outcome == "timeout"
    assert internal.reason != deadline.reason
    assert "deadline-exceeded" in (deadline.reason or "")
    assert "deadline-exceeded" not in (internal.reason or "")


def test_runrecord_to_dict_omits_none():
    from lib.schema import RunRecord
    rec = RunRecord(sensor="x", status="skipped")
    d = rec.to_dict()
    assert d == {"sensor": "x", "status": "skipped"}  # no None keys
