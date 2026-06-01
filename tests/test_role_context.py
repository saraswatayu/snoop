"""Tests for lib/role_context.py — employer/title/tenure facts.

Deterministic, no network. Provenance is the point: a plan-declared employer is
only "possibly" unless the github profile corroborated it (Codex #2).
"""

from __future__ import annotations

from datetime import datetime, timezone

from lib.role_context import collect_role_context
from lib.schema import Employer, Person


def _now():
    return datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _person(**overrides):
    base = dict(name="Peter Steinberger", ambiguity="single_plausible_match")
    base.update(overrides)
    return Person(**base)


def test_corroborated_current_employer_is_asserted():
    person = _person(
        employer=Employer(name="OpenAI", domains=["openai.com"], since="2024-01"),
        bound_anchors=[
            ("github_name_match", "Peter Steinberger"),
            ("github_employer_match", "OpenAI"),
        ],
    )
    result = collect_role_context(person, now=_now())
    assert result.status == "ok"
    assert result.resolver == "role_context"
    role = result.contributions[0]
    assert role.kind == "role"
    assert role.employer == "OpenAI"
    assert role.until is None  # current
    assert role.since == "2024-01"
    assert role.bind_tier == "asserted"


def test_plan_only_employer_is_possibly():
    """No github_employer_match anchor: the employer is a plan hint -> possibly."""
    person = _person(
        employer=Employer(name="OpenAI", domains=["openai.com"]),
        bound_anchors=[("github_name_match", "Peter Steinberger")],
    )
    result = collect_role_context(person, now=_now())
    assert result.contributions[0].bind_tier == "possibly"


def test_former_employer_has_until_and_is_possibly():
    person = _person(
        employer=Employer(name="OpenAI", domains=["openai.com"]),
        former_employers=[Employer(name="PSPDFKit", domains=["pspdfkit.com"], until="2023")],
        bound_anchors=[
            ("github_name_match", "Peter Steinberger"),
            ("github_employer_match", "OpenAI"),
        ],
    )
    result = collect_role_context(person, now=_now())
    formers = [r for r in result.contributions if r.employer == "PSPDFKit"]
    assert len(formers) == 1
    assert formers[0].until == "2023"
    assert formers[0].bind_tier == "possibly"


def test_no_employer_is_empty():
    result = collect_role_context(_person(), now=_now())
    assert result.status == "empty"
    assert result.contributions == []
