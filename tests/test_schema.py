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
    Channel,
    ConsistencyNote,
    EmailCandidate,
    Employer,
    Identity,
    Person,
    Profile,
    ResolverResult,
    RoleFact,
    SocialLink,
    Source,
    WorkItem,
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


# ---- profile expansion (2026-06-01) ----------------------------------------


def test_email_candidate_is_a_contribution():
    """EmailCandidate is the original Contribution; kind discriminates it."""
    c = EmailCandidate(address="x@example.com")
    assert c.kind == "email"


def test_new_contribution_kinds_and_defaults():
    """Every new fact type carries its kind and defaults to unbound — a fact
    with no binding evidence must not render as 'theirs' until lib.binding
    classifies it."""
    w = WorkItem(title="My talk on X")
    ch = Channel(channel_type="x_dm", value="@dan")
    sl = SocialLink(platform="x", url="https://x.com/dan")
    role = RoleFact(employer="OpenAI")
    note = ConsistencyNote(note="github name matches plan")
    assert (w.kind, ch.kind, sl.kind, role.kind, note.kind) == (
        "work_item", "channel", "social_link", "role", "consistency_note",
    )
    for fact in (w, ch, sl, role, note):
        assert fact.bind_tier == "unbound"
        assert fact.sources == []
        assert fact.bind_reasons == []


def test_consistency_note_severity_defaults_to_info():
    """A consistency note is neutral evidence by default, never an accusation."""
    assert ConsistencyNote(note="ok").severity == "info"
    assert ConsistencyNote(note="company differs", severity="mismatch").severity == "mismatch"


def test_identity_is_person_for_now():
    """Identity aliases Person during the migration; new code references Identity."""
    assert Identity is Person


def test_resolver_result_contributions_defaults_empty():
    """Additive field: email resolvers that only set `candidates` still work."""
    r = ResolverResult(resolver="gh_profile", candidates=[], status="empty")
    assert r.contributions == []


def test_profile_add_dispatches_by_kind():
    """Profile.add is the merge primitive — it routes each contribution into
    its section by .kind. This is the dispatch-on-kind contract (D2)."""
    p = Profile(identity=Person(name="Dan Neil"))
    p.add(EmailCandidate(address="dan@acme.com"))
    p.add(WorkItem(title="Conference talk"))
    p.add(Channel(channel_type="linkedin", value="in/danneil"))
    p.add(SocialLink(platform="github", url="https://github.com/danneil"))
    p.add(RoleFact(employer="Acme"))
    p.add(ConsistencyNote(note="name consistent"))
    assert len(p.emails) == 1
    assert len(p.work_items) == 1
    assert len(p.channels) == 1
    assert len(p.social_links) == 1
    assert len(p.roles) == 1
    assert len(p.consistency_notes) == 1


def test_profile_contributions_flattens_in_section_order():
    p = Profile(identity=Person(name="Dan Neil"))
    p.add(WorkItem(title="t"))
    p.add(EmailCandidate(address="a@b.com"))
    flat = p.contributions()
    # emails come first in the stable section order regardless of insert order
    assert flat[0].kind == "email"
    assert {c.kind for c in flat} == {"email", "work_item"}


def test_profile_starts_empty_except_identity():
    p = Profile(identity=Person(name="X"))
    assert p.emails == [] and p.work_items == [] and p.channels == []
    assert p.social_links == [] and p.roles == [] and p.consistency_notes == []
