"""Tests for lib/consistency_notes.py — text-only identity-consistency notes.

Deterministic, no network, text only. A note is neutral evidence: a diminutive
is "info", a real disagreement is "mismatch". No photo matching anywhere.
"""

from __future__ import annotations

from datetime import datetime, timezone

from lib.consistency_notes import collect_consistency_notes
from lib.schema import Employer, Person


def _now():
    return datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _person(**overrides):
    base = dict(
        name="Peter Steinberger",
        ambiguity="single_plausible_match",
        bound_anchors=[
            ("github_name_match", "Peter Steinberger"),
            ("github_employer_match", "OpenAI"),
        ],
    )
    base.update(overrides)
    return Person(**base)


def test_diminutive_name_is_info():
    person = _person(name="Dan", gh_name="Daniel Neil")
    result = collect_consistency_notes(person, now=_now())
    assert result.status == "ok"
    note = result.contributions[0]
    assert note.kind == "consistency_note"
    assert note.severity == "info"
    assert "Dan" in note.note and "Daniel Neil" in note.note
    assert note.bind_tier == "asserted"


def test_employer_disagreement_is_mismatch():
    person = _person(
        employer=Employer(name="OpenAI", domains=["openai.com"]),
        gh_company="Anthropic",
    )
    result = collect_consistency_notes(person, now=_now())
    mismatches = [n for n in result.contributions if n.severity == "mismatch"]
    assert len(mismatches) == 1
    assert "OpenAI" in mismatches[0].note and "Anthropic" in mismatches[0].note


def test_consistent_identity_produces_no_notes():
    person = _person(
        name="Peter Steinberger",
        gh_name="Peter Steinberger",
        employer=Employer(name="OpenAI", domains=["openai.com"]),
        gh_company="@openai",
    )
    result = collect_consistency_notes(person, now=_now())
    assert result.status == "empty"
    assert result.contributions == []


def test_no_gh_data_produces_no_notes():
    """Nothing to cross-check against -> no notes (never invents a concern)."""
    person = _person(name="Peter Steinberger")  # no gh_name / gh_company
    result = collect_consistency_notes(person, now=_now())
    assert result.status == "empty"


def test_note_is_possibly_when_handle_unbound():
    person = _person(
        name="Dan", gh_name="Daniel Neil",
        bound_anchors=[("github_name_match", "Dan")],  # only 1 validating anchor
    )
    result = collect_consistency_notes(person, now=_now())
    assert result.contributions[0].bind_tier == "possibly"
