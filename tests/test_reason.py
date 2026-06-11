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
        address="alice@corp.com", smtp_verdict="verified",
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
        address="alicesmith@corp.com",
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
        address="alice@corp.com",
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


# --- structured data mirror ---------------------------------------------------


def test_email_candidate_carries_structured_data():
    """The host model reads fields off `data` instead of re-parsing the sentence;
    every source URL survives (the content line keeps only one for readability)."""
    cand = EmailCandidate(
        address="alice@corp.com", smtp_verdict="verified",
        sources=[
            Source(type="git_commit", url="https://github.com/x/y/commit/abc",
                   observed_at=NOW, detail="commit"),
            Source(type="gh_profile", url="https://github.com/alice",
                   observed_at=NOW, detail="profile email"),
        ],
    )
    o = _email_obs(reason.build_evidence(_person(), [cand]))
    assert o.data is not None
    assert o.data["address"] == "alice@corp.com"
    assert o.data["smtp"] == "verified"
    assert {s["type"] for s in o.data["sources"]} == {"git_commit", "gh_profile"}
    # both source URLs survive in data (content keeps just one)
    urls = {s["url"] for s in o.data["sources"]}
    assert "https://github.com/x/y/commit/abc" in urls
    assert "https://github.com/alice" in urls


def test_structured_data_mirrors_google_name_match():
    cand = EmailCandidate(
        address="alicesmith@corp.com", account_exists="verified",
        account_display_name="Alice Smith",
    )
    o = _email_obs(reason.build_evidence(_person(), [cand]))
    assert o.data["google_display_name"] == "Alice Smith"
    assert o.data["name_match"] is True


def test_non_email_observations_have_no_data():
    obs = reason.build_evidence(_person(), [])
    assert all(o.data is None for o in obs)


# --- employer corroboration provenance ----------------------------------------


def _employer_obs(obs):
    return [o for o in obs if o.type == "employer"]


def test_employer_without_source_is_declared_only():
    person = Person(name="X", employer=Employer(name="Corp", domains=["corp.com"]))
    o = _employer_obs(reason.build_evidence(person, []))[0]
    assert "declared current employer: Corp" in o.content
    assert o.source_url is None


def test_employer_with_source_url_is_citable():
    """When the host set employer.source_url (where it confirmed the employer),
    the observation says 'confirmed via source' and carries the URL — so a role
    fact cites real corroboration, not just the host's plan declaration."""
    person = Person(
        name="X",
        employer=Employer(name="Simile", domains=["simile.ai"],
                          source_url="https://www.bloomberg.com/news/simile"),
    )
    o = _employer_obs(reason.build_evidence(person, []))[0]
    assert "current employer confirmed via source: Simile" in o.content
    assert o.source_url == "https://www.bloomberg.com/news/simile"


def test_former_employer_source_url_is_citable():
    person = Person(
        name="X",
        former_employers=[Employer(name="Figma", domains=["figma.com"], until="2026",
                                   source_url="https://lennys/figma")],
    )
    o = _employer_obs(reason.build_evidence(person, []))[0]
    assert "former employer confirmed via source: Figma" in o.content
    assert o.source_url == "https://lennys/figma"


# --- channel-hint confirmation ------------------------------------------------


def _channel_obs(obs):
    return [o for o in obs if o.type == "channel_hint"]


def test_bare_channel_hint_is_declared():
    person = Person(name="X", channel_hints={"linkedin": "https://linkedin.com/in/x"})
    o = _channel_obs(reason.build_evidence(person, []))[0]
    assert "declared channel hint: linkedin = https://linkedin.com/in/x" in o.content
    assert o.source_url == "https://linkedin.com/in/x"


def test_confirmed_channel_hint_carries_basis():
    """When the host confirmed a public profile during resolution, the channel is
    emitted as 'confirmed channel' with the basis and a citable URL."""
    person = Person(name="X", channel_hints={
        "linkedin": {"url": "https://linkedin.com/in/x",
                     "confirmed_via": "public profile: name + Simile match"},
    })
    o = _channel_obs(reason.build_evidence(person, []))[0]
    assert "confirmed channel: linkedin = https://linkedin.com/in/x" in o.content
    assert "public profile: name + Simile match" in o.content
    assert o.source_url == "https://linkedin.com/in/x"


def test_non_http_channel_hint_has_no_source_url():
    person = Person(name="X", channel_hints={"x_dms_open": True})
    o = _channel_obs(reason.build_evidence(person, []))[0]
    assert o.source_url is None


# --- mx provider / M365 honesty -----------------------------------------------


def test_m365_inconclusive_surfaces_lean_on_channel_hints():
    """On M365 there's no existence oracle, so an inconclusive RCPT is surfaced
    with the provider + explicit 'lean on channel hints' guidance."""
    cand = EmailCandidate(
        address="exec@corp.com", smtp_verdict="inconclusive", mx_provider="microsoft",
        sources=[Source(type="pattern", url=None, observed_at=NOW, detail="guess")],
    )
    o = _email_obs(reason.build_evidence(_person(), [cand]))
    assert "mx=microsoft" in o.content
    assert "no existence oracle" in o.content
    assert o.data["mx_provider"] == "microsoft"
    assert "channel hints" in o.data["smtp_note"]


def test_mx_provider_surfaced_without_m365_note_for_other_providers():
    cand = EmailCandidate(
        address="a@corp.com", smtp_verdict="verified", mx_provider="other",
        sources=[Source(type="gh_profile", url="https://github.com/a",
                        observed_at=NOW, detail="profile")],
    )
    o = _email_obs(reason.build_evidence(_person(), [cand]))
    assert "mx=other" in o.content
    assert "existence oracle" not in o.content  # the M365-specific note only on microsoft
    assert "smtp_note" not in o.data
