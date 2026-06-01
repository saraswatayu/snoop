"""Tests for lib/reachability.py — OBSERVED reachability channel aggregation.

Deterministic, no network. We assert that observed channels surface with the
right type, evidence, and binding tier, that unbound channels are dropped,
and that the list is ordered by rank_hint (observed ordering, not a delivery
guarantee).
"""

from __future__ import annotations

from datetime import datetime, timezone

from lib.reachability import collect_channels
from lib.schema import EmailCandidate, Person, Source


def _now():
    return datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _src(type_, url=None, detail="x"):
    return Source(type=type_, url=url, observed_at=_now(), detail=detail)


def _bound_person(**overrides):
    """A person with >=2 validating anchors so profile-typed sources assert."""
    kw = dict(
        name="Peter Steinberger",
        handles={"github": "steipete"},
        bound_anchors=[
            ("github_name_match", "Peter Steinberger"),
            ("github_employer_match", "OpenAI"),
        ],
        ambiguity="single_plausible_match",
    )
    kw.update(overrides)
    return Person(**kw)


# ---- email channel ----------------------------------------------------------


def test_email_channel_from_top_candidate_is_asserted():
    person = _bound_person()
    emails = [EmailCandidate(
        address="p@openai.com",
        belongs_to_person=0.8,
        smtp_verdict="verified",
        sources=[_src("gh_profile", url="https://github.com/steipete")],
    )]
    result = collect_channels(person, emails, now=_now())

    assert result.resolver == "reachability"
    assert result.status == "ok"
    email = next(c for c in result.contributions if c.channel_type == "email")
    assert email.value == "p@openai.com"
    assert email.evidence == "SMTP verified"
    assert email.bind_tier == "asserted"


def test_email_channel_picks_highest_belongs():
    person = _bound_person()
    emails = [
        EmailCandidate(address="low@openai.com", belongs_to_person=0.2,
                       sources=[_src("gh_profile", url="https://github.com/steipete")]),
        EmailCandidate(address="high@openai.com", belongs_to_person=0.9,
                       sources=[_src("gh_profile", url="https://github.com/steipete")]),
    ]
    result = collect_channels(person, emails, now=_now())
    email = next(c for c in result.contributions if c.channel_type == "email")
    assert email.value == "high@openai.com"
    assert email.evidence == "belongs=0.9"


# ---- x_dm channel -----------------------------------------------------------


def test_x_dm_channel_when_dms_open():
    person = _bound_person(channel_hints={"x_dms_open": True}, gh_twitter="steipete")
    result = collect_channels(person, None, now=_now())

    dm = next(c for c in result.contributions if c.channel_type == "x_dm")
    assert dm.value == "@steipete"
    assert "DMs open" in (dm.evidence or "")
    assert dm.bind_tier == "possibly"


def test_x_dm_channel_unknown_handle():
    person = _bound_person(channel_hints={"x_dms_open": True}, gh_twitter=None)
    result = collect_channels(person, None, now=_now())
    dm = next(c for c in result.contributions if c.channel_type == "x_dm")
    assert dm.value == "(x handle unknown)"


# ---- linkedin channel -------------------------------------------------------


def test_linkedin_channel_is_possibly():
    person = _bound_person(channel_hints={"linkedin": "https://linkedin.com/in/x"})
    result = collect_channels(person, None, now=_now())

    li = next(c for c in result.contributions if c.channel_type == "linkedin")
    assert li.value == "https://linkedin.com/in/x"
    assert li.bind_tier == "possibly"


# ---- ordering ---------------------------------------------------------------


def test_channels_sorted_by_rank_hint_desc():
    """Verified email first, then DMs open, then linkedin, then calendly."""
    person = _bound_person(
        channel_hints={
            "x_dms_open": True,
            "linkedin": "https://linkedin.com/in/x",
            "calendly": "https://calendly.com/x",
        },
        gh_twitter="steipete",
    )
    emails = [EmailCandidate(
        address="p@openai.com",
        belongs_to_person=0.9,
        smtp_verdict="verified",
        sources=[_src("gh_profile", url="https://github.com/steipete")],
    )]
    result = collect_channels(person, emails, now=_now())

    order = [c.channel_type for c in result.contributions]
    assert order == ["email", "x_dm", "linkedin", "calendly"]
    ranks = [c.rank_hint for c in result.contributions]
    assert ranks == sorted(ranks, reverse=True)


# ---- empty ------------------------------------------------------------------


def test_nothing_observed_is_empty():
    person = _bound_person()
    result = collect_channels(person, None, now=_now())
    assert result.status == "empty"
    assert result.contributions == []


def test_empty_email_list_observes_nothing():
    person = _bound_person()
    result = collect_channels(person, [], now=_now())
    assert result.status == "empty"
