"""Tests for lib/reason.py — build_evidence (the observation bundle).

No network: build_evidence is a pure flatten of the resolved person + scored
candidates into a numbered, typed, cited observation list that the host model
reasons over.
"""

from __future__ import annotations

from lib import reason
from lib.schema import EmailCandidate, Employer, GitHubRepo, Person, Source
from datetime import datetime, timezone


NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _person():
    return Person(
        name="Alice Smith",
        handles={"github": "alice"},
        gh_name="Alice Smith",
        gh_company="Corp",
        employer=Employer(name="Corp", domains=["corp.com"]),
        gh_recent_repos=[GitHubRepo(
            name="alice/widget", description="a widget",
            html_url="https://github.com/alice/widget", pushed_at="2026-05-01T00:00:00Z",
        )],
        bound_anchors=[("github_name_match", "Alice Smith"),
                       ("github_employer_match", "Corp")],
        ambiguity="single_plausible_match",
    )


def _candidate():
    return EmailCandidate(
        address="alice@corp.com", belongs_to_person=0.8, smtp_verdict="verified",
        sources=[Source(type="gh_profile", url="https://github.com/alice",
                        observed_at=NOW, detail="profile email")],
    )


# --- build_evidence ----------------------------------------------------------


def test_build_evidence_flattens_core_signals():
    obs = reason.build_evidence(_person(), [_candidate()])
    types = {o.type for o in obs}
    assert {"github_handle", "gh_profile", "github_repo", "email_candidate",
            "anchor", "employer"} <= types
    # ids are unique and contiguous
    assert [o.id for o in obs] == [f"o{i}" for i in range(1, len(obs) + 1)]
    blob = "\n".join(o.content for o in obs)
    assert "alice@corp.com" in blob and "alice/widget" in blob


def _email_obs(obs):
    return next(o for o in obs if o.type == "email_candidate")


def test_build_evidence_surfaces_google_display_name_with_name_match_yes():
    """The text disambiguator: a Google-confirmed account whose display name
    matches the target shows name_match=yes for the host model to bind on."""
    cand = EmailCandidate(
        address="alicesmith@corp.com", belongs_to_person=0.85,
        smtp_verdict="catch_all", account_exists="verified",
        account_display_name="Alice Smith",
    )
    content = _email_obs(reason.build_evidence(_person(), [cand])).content
    assert 'google_display_name="Alice Smith"' in content
    assert "name_match=yes" in content


def test_build_evidence_flags_name_match_no_for_namesake():
    """A real-but-different account on the same tenant (e.g. jdoe@ vs jdoeh@)
    must show name_match=no so it can be dropped — text, not faces."""
    cand = EmailCandidate(
        address="alice@corp.com", belongs_to_person=0.4,
        smtp_verdict="catch_all", account_exists="verified",
        account_display_name="Alice Wong",
    )
    content = _email_obs(reason.build_evidence(_person(), [cand])).content
    assert 'google_display_name="Alice Wong"' in content
    assert "name_match=no" in content


def test_build_evidence_labels_photo_as_human_review_artifact():
    """The avatar URL is surfaced for a human to eyeball, explicitly NOT as an
    automated match signal."""
    cand = EmailCandidate(
        address="alicesmith@corp.com", account_exists="verified",
        account_display_name="Alice Smith",
        account_photo_url="https://lh3.googleusercontent.com/a/alice=s96",
    )
    content = _email_obs(reason.build_evidence(_person(), [cand])).content
    assert "https://lh3.googleusercontent.com/a/alice=s96" in content
    assert "human-review artifact, not an automated match" in content


def test_build_evidence_omits_google_fields_when_absent():
    """No Google display name / photo → the observation stays as-is (no
    dangling 'name_match=' or 'google_photo=' noise)."""
    content = _email_obs(reason.build_evidence(_person(), [_candidate()])).content
    assert "google_display_name" not in content
    assert "google_photo" not in content
    assert "name_match" not in content


def test_build_evidence_empty_candidates_still_describes_identity():
    obs = reason.build_evidence(_person(), [])
    assert any(o.type == "gh_profile" for o in obs)
    assert not any(o.type == "email_candidate" for o in obs)
