"""Tests for render.render_profile_card — the profile-expansion default output.

The email answer must lead (D2-B), profile sections follow, and every field
carries a provenance marker. The identity gate (D4 level 1) downgrades every
field to "possibly" when the person is not a single confident match.
"""

from __future__ import annotations

from datetime import datetime, timezone

from lib.profile_build import build_profile
from lib.render import render_profile_card
from lib.schema import EmailCandidate, Employer, GitHubRepo, Person, Source


def _now():
    return datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _person(ambiguity="single_plausible_match"):
    return Person(
        name="Dan",
        gh_name="Daniel Neil",
        handles={"github": "danneil"},
        gh_twitter="danneil",
        employer=Employer(name="Formation Bio", domains=["formation.bio"]),
        gh_company="Formation Bio",
        channel_hints={"x_dms_open": True, "linkedin": "https://linkedin.com/in/danneil"},
        gh_recent_repos=[GitHubRepo(
            name="danneil/x", description="clinical pipeline",
            html_url="https://github.com/danneil/x", pushed_at="2026-05-01T00:00:00Z",
        )],
        bound_anchors=[
            ("github_name_match", "Daniel Neil"),
            ("github_employer_match", "Formation Bio"),
        ],
        ambiguity=ambiguity,
    )


def _candidate():
    return EmailCandidate(
        address="dan@formation.bio", belongs_to_person=0.8, smtp_verdict="verified",
        sources=[Source(type="gh_profile", url="https://github.com/danneil",
                        observed_at=_now(), detail="profile email")],
    )


def test_profile_card_leads_with_email_then_sections():
    profile = build_profile(_person(), [_candidate()], now=_now())
    out = render_profile_card(profile)
    # email answer leads
    assert "dan@formation.bio" in out.split("Social:")[0]
    # sections present
    assert "Social:" in out
    assert "Body of work:" in out
    assert "Roles:" in out
    assert "Other ways in:" in out  # x_dm / linkedin (email pick is the lead)


def test_asserted_fields_marked_when_single_match():
    profile = build_profile(_person(), [_candidate()], now=_now())
    out = render_profile_card(profile)
    assert "[+]" in out  # at least one asserted field (github social / repo)


def test_identity_gate_downgrades_when_ambiguous():
    profile = build_profile(_person(ambiguity="multiple_plausible_matches"),
                            [_candidate()], now=_now())
    out = render_profile_card(profile)
    assert "identity is NOT a single confident match" in out
    # nothing asserts under an ambiguous identity
    assert "[+]" not in out
    assert "[?]" in out


def test_empty_profile_still_renders_lead():
    person = Person(name="Nobody", ambiguity="insufficient_identity_evidence")
    profile = build_profile(person, [], now=_now())
    out = render_profile_card(profile)
    assert "Nobody" in out
