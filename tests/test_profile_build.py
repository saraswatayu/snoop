"""Tests for lib/profile_build.py — Profile assembly (the merge step).

Deterministic, no network. Verifies that producers' contributions land in the
right Profile sections and that capability notes (T8) reach person.notes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from lib.profile_build import build_profile
from lib.schema import EmailCandidate, Employer, GitHubRepo, Person, Source


def _now():
    return datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _rich_person():
    return Person(
        name="Dan",
        gh_name="Daniel Neil",                       # diminutive -> consistency note
        handles={"github": "danneil"},
        gh_twitter="danneil",
        employer=Employer(name="Formation Bio", domains=["formation.bio"]),
        gh_company="Formation Bio",
        channel_hints={"x_dms_open": True},
        gh_recent_repos=[GitHubRepo(
            name="danneil/x", description="d",
            html_url="https://github.com/danneil/x", pushed_at="2026-05-01T00:00:00Z",
        )],
        bound_anchors=[
            ("github_name_match", "Daniel Neil"),
            ("github_employer_match", "Formation Bio"),
        ],
        ambiguity="single_plausible_match",
    )


def _candidate():
    return EmailCandidate(
        address="dan@formation.bio",
        belongs_to_person=0.8,
        smtp_verdict="verified",
        sources=[Source(type="gh_profile", url="https://github.com/danneil",
                        observed_at=_now(), detail="profile email")],
    )


def test_build_profile_routes_every_section():
    person = _rich_person()
    profile = build_profile(person, [_candidate()], now=_now())

    assert profile.identity is person
    assert len(profile.emails) == 1
    assert any(s.platform == "github" for s in profile.social_links)
    assert any(c.channel_type == "email" for c in profile.channels)
    assert any(c.channel_type == "x_dm" for c in profile.channels)
    assert any(w.item_type == "repo" for w in profile.work_items)
    assert any(r.employer == "Formation Bio" for r in profile.roles)
    # "Dan" vs "Daniel Neil" -> a diminutive consistency note
    assert any(n.severity == "info" for n in profile.consistency_notes)


def test_search_no_results_note_reaches_person_notes():
    person = _rich_person()
    build_profile(person, [_candidate()], enable_search=True, search_fn=None, now=_now())
    assert any("work_search_results" in n for n in person.notes)


def test_supplied_search_results_become_work_items():
    """End-to-end: host-model-style results with a crosslink to the person's
    bound domain land as work items; a namesake without crosslink is dropped."""
    person = _rich_person()
    person.bound_anchors.append(("github_personal_domain_match", "formation.bio"))
    person.personal_domains = ["formation.bio"]
    results = [
        {"title": "My clinical-data talk", "url": "https://confvids.example/v/1",
         "item_type": "talk", "crosslink_url": "https://formation.bio/talks"},
        {"title": "Talk by a different Dan", "url": "https://randomconf.example/x",
         "item_type": "talk"},  # no crosslink -> namesake -> dropped
    ]
    profile = build_profile(person, [_candidate()],
                            search_fn=lambda q: results, now=_now())
    talks = [w for w in profile.work_items if w.item_type == "talk"]
    assert len(talks) == 1
    assert talks[0].title == "My clinical-data talk"


def test_empty_person_yields_profile_with_only_emails():
    person = Person(name="Nobody", ambiguity="insufficient_identity_evidence")
    profile = build_profile(person, [], now=_now())
    assert profile.emails == []
    assert profile.work_items == []
    assert profile.social_links == []
    assert profile.roles == []


def test_contributions_flatten_covers_all_added():
    person = _rich_person()
    profile = build_profile(person, [_candidate()], now=_now())
    kinds = {c.kind for c in profile.contributions()}
    assert "email" in kinds
    assert "social_link" in kinds
    assert "channel" in kinds
    assert "work_item" in kinds
    assert "role" in kinds


def test_one_producer_crash_does_not_lose_the_profile(monkeypatch):
    """C2: producers are isolated. One raising must NOT abort the whole profile —
    the email answer and the surviving sections still build, and the failure is
    recorded as a note instead of propagating (mirrors the email fan-out)."""
    import lib.profile_build as pb

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(pb, "collect_social_links", boom)
    person = _rich_person()
    profile = pb.build_profile(person, [_candidate()], now=_now())

    # The email answer (the original deliverable) survives.
    assert len(profile.emails) == 1
    # A surviving producer still ran (the diminutive name -> a consistency note).
    assert profile.consistency_notes
    # The crashed producer left no section but DID leave a diagnostic note.
    assert profile.social_links == []
    assert any("social_links" in n and "kaboom" in n for n in person.notes)
