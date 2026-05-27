"""Tests for lib/score.py — the 3-field provenance-aware scorer.

The structural choices being tested:
- 3 independent fields with None=abstain
- belongs_to_person reflects source weight × corroboration × cap rules
- current_work_address is gated by employer_match / personal-provider /
  former-employer signals
- deliverable abstains on SMTP inconclusive (per Codex c1)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lib.schema import EmailCandidate, Employer, Person, Source
from lib.score import (
    PERSONAL_PROVIDER_DOMAINS,
    is_personal_provider,
    score_all,
    score_candidate,
)


NOW = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)


def src(type_, days_ago=10, detail=None, url=None):
    return Source(
        type=type_,
        url=url,
        observed_at=NOW - timedelta(days=days_ago),
        detail=detail or f"{type_} {days_ago}d ago",
    )


def person_with_employer(name="Peter Steinberger", domain="openai.com"):
    return Person(
        name=name,
        employer=Employer(name="OpenAI", domains=[domain]),
        ambiguity="single_plausible_match",
    )


# ---- is_personal_provider ---------------------------------------------------


def test_is_personal_provider_recognizes_majors():
    assert is_personal_provider("gmail.com")
    assert is_personal_provider("yahoo.com")
    assert is_personal_provider("icloud.com")
    assert is_personal_provider("protonmail.com")
    assert is_personal_provider("outlook.com")


def test_is_personal_provider_case_insensitive():
    assert is_personal_provider("Gmail.COM")


def test_is_personal_provider_rejects_corporate():
    assert not is_personal_provider("openai.com")
    assert not is_personal_provider("acme.dev")


# ---- belongs_to_person ------------------------------------------------------


def test_no_sources_yields_none_belongs():
    """A candidate with no sources can't be scored — abstain."""
    c = EmailCandidate(address="x@y.com")
    scored = score_candidate(c, person_with_employer(), now=NOW)
    assert scored.belongs_to_person is None
    assert scored.current_work_address is None
    assert scored.deliverable is None


def test_git_commit_weight_decays_with_age():
    """Same email source — fresh commit vs year-old commit should score
    differently."""
    fresh = EmailCandidate(
        address="pete@openai.com", sources=[src("git_commit", days_ago=5)],
    )
    old = EmailCandidate(
        address="pete@openai.com", sources=[src("git_commit", days_ago=365)],
    )
    score_candidate(fresh, person_with_employer(), now=NOW)
    score_candidate(old, person_with_employer(), now=NOW)
    assert fresh.belongs_to_person > old.belongs_to_person


def test_corroboration_boosts_belongs_score():
    """Two independent non-pattern sources should beat one."""
    one = EmailCandidate(
        address="pete@openai.com",
        sources=[src("git_commit", days_ago=10)],
    )
    two = EmailCandidate(
        address="pete@openai.com",
        sources=[src("git_commit", days_ago=10), src("gh_profile", days_ago=10)],
    )
    score_candidate(one, person_with_employer(), now=NOW)
    score_candidate(two, person_with_employer(), now=NOW)
    assert two.belongs_to_person > one.belongs_to_person


def test_pattern_only_caps_at_low_score():
    """Pattern-only candidates can't exceed 0.30 — no observational evidence."""
    c = EmailCandidate(
        address="pete@openai.com",
        sources=[src("pattern", days_ago=0, detail="first.last template")],
    )
    score_candidate(c, person_with_employer(), now=NOW)
    assert c.belongs_to_person is not None
    assert c.belongs_to_person <= 0.30
    assert "pattern-only" in " ".join(c.score_reasons)


def test_generic_localpart_caps_belongs_at_035():
    """info@/hello@/contact@ get capped even with strong source."""
    c = EmailCandidate(
        address="hello@steipete.com",
        sources=[
            src("personal_site", days_ago=5),
            src("gh_profile", days_ago=5),
        ],
    )
    score_candidate(c, person_with_employer(), now=NOW)
    assert c.belongs_to_person is not None
    assert c.belongs_to_person <= 0.35
    assert "generic localpart" in " ".join(c.score_reasons)


def test_manual_known_is_highest_trust():
    """A user-supplied known address scores higher than gh_profile."""
    known = EmailCandidate(
        address="pete@openai.com",
        sources=[src("manual_known", days_ago=0)],
    )
    profile = EmailCandidate(
        address="pete@openai.com",
        sources=[src("gh_profile", days_ago=0)],
    )
    score_candidate(known, person_with_employer(), now=NOW)
    score_candidate(profile, person_with_employer(), now=NOW)
    assert known.belongs_to_person > profile.belongs_to_person


# ---- current_work_address ---------------------------------------------------


def test_personal_provider_is_never_current_work():
    """Gmail/iCloud/etc. → current_work=0.0 regardless of other signals."""
    c = EmailCandidate(
        address="steipete@gmail.com",
        sources=[src("git_commit", days_ago=5), src("whois", days_ago=10)],
    )
    score_candidate(c, person_with_employer(), now=NOW)
    assert c.is_personal_provider is True
    assert c.current_work_address == 0.0
    # belongs is still high (we ARE confident it's his address)
    assert c.belongs_to_person is not None and c.belongs_to_person > 0.5


def test_no_employer_yields_none_current_work():
    """If person has no resolved employer, abstain on current_work."""
    c = EmailCandidate(
        address="x@randomcorp.com",
        sources=[src("git_commit", days_ago=10)],
    )
    p = Person(name="Anonymous", ambiguity="single_plausible_match")
    score_candidate(c, p, now=NOW)
    assert c.current_work_address is None


def test_wrong_domain_yields_none_current_work():
    """Address domain doesn't match resolved employer → abstain."""
    c = EmailCandidate(
        address="pete@other.com",  # not openai.com
        sources=[src("git_commit", days_ago=10)],
    )
    score_candidate(c, person_with_employer(), now=NOW)
    assert c.current_work_address is None
    assert "doesn't match" in " ".join(c.score_reasons)


def test_employer_match_with_fresh_git_commit_boosts():
    """openai.com address with a fresh git commit → high current_work."""
    c = EmailCandidate(
        address="pete@openai.com",
        sources=[src("git_commit", days_ago=10)],
    )
    score_candidate(c, person_with_employer(), now=NOW)
    assert c.belongs_to_person is not None
    assert c.current_work_address is not None
    assert c.current_work_address > c.belongs_to_person


def test_employer_match_pattern_only_capped():
    """openai.com address with ONLY pattern source — believe the domain
    is right, but no evidence target uses this specific address."""
    c = EmailCandidate(
        address="pete@openai.com",
        sources=[src("pattern")],
    )
    score_candidate(c, person_with_employer(), now=NOW)
    assert c.current_work_address is not None
    assert c.current_work_address <= 0.40


def test_former_employer_capped_at_030():
    """An address on a former-employer domain caps at 0.30 — likely inactive."""
    p = Person(
        name="Peter Steinberger",
        employer=Employer(name="OpenAI", domains=["openai.com"]),
        former_employers=[Employer(name="PSPDFKit", domains=["pspdfkit.com"])],
        ambiguity="single_plausible_match",
    )
    c = EmailCandidate(
        address="pete@pspdfkit.com",
        sources=[src("git_commit", days_ago=400)],
    )
    score_candidate(c, p, now=NOW)
    assert c.employer_former_match is True
    assert c.current_work_address == 0.30
    assert "former" in " ".join(c.score_reasons).lower()


# ---- deliverable ------------------------------------------------------------


def test_smtp_verified_yields_high_deliverable():
    c = EmailCandidate(
        address="pete@selfhosted.io",
        sources=[src("git_commit", days_ago=5)],
        smtp_verdict="verified",
    )
    score_candidate(c, person_with_employer(domain="selfhosted.io"), now=NOW)
    assert c.deliverable is not None
    assert c.deliverable > 0.7


def test_smtp_invalid_yields_low_deliverable():
    c = EmailCandidate(
        address="bogus@selfhosted.io",
        sources=[src("pattern")],
        smtp_verdict="invalid",
    )
    score_candidate(c, person_with_employer(), now=NOW)
    assert c.deliverable is not None
    assert c.deliverable < 0.1


def test_smtp_catch_all_yields_mid_deliverable():
    c = EmailCandidate(
        address="anything@catchall.com",
        sources=[src("pattern")],
        smtp_verdict="catch_all",
    )
    score_candidate(c, person_with_employer(domain="catchall.com"), now=NOW)
    assert c.deliverable is not None
    assert 0.3 <= c.deliverable <= 0.5


def test_smtp_inconclusive_yields_none_deliverable():
    """Per Codex c1: inconclusive on Google/M365 is zero information.
    Score must be None, NOT a floor."""
    c = EmailCandidate(
        address="pete@openai.com",
        sources=[src("git_commit", days_ago=10)],
        smtp_verdict="inconclusive",
        mx_provider="microsoft",
    )
    score_candidate(c, person_with_employer(), now=NOW)
    assert c.deliverable is None
    # And the reason should explicitly say "zero information"
    reasons_text = " ".join(c.score_reasons)
    assert "zero information" in reasons_text
    assert "microsoft" in reasons_text.lower()


def test_smtp_unprobed_personal_provider_yields_none_with_hint():
    """For @gmail addresses (never probed by design), deliverable abstains
    but the reason notes the provider's general reliability."""
    c = EmailCandidate(
        address="steipete@gmail.com",
        sources=[src("git_commit", days_ago=5)],
        smtp_verdict="unprobed",
    )
    score_candidate(c, person_with_employer(), now=NOW)
    assert c.deliverable is None
    assert "personal-provider" in " ".join(c.score_reasons)


# ---- score_all sorting ------------------------------------------------------


def test_score_all_sorts_by_belongs_desc_with_none_last():
    high = EmailCandidate(
        address="strong@openai.com",
        sources=[src("git_commit", days_ago=5), src("gh_profile")],
    )
    low = EmailCandidate(
        address="weak@openai.com",
        sources=[src("pattern")],
    )
    none_score = EmailCandidate(address="no-sources@openai.com")
    p = person_with_employer()
    result = score_all([none_score, low, high], p, now=NOW)
    assert result[0] is high
    assert result[1] is low
    assert result[2] is none_score
    assert result[2].belongs_to_person is None


def test_score_all_ties_broken_by_address_for_determinism():
    """Two candidates with identical sources tied at the same score must
    sort by address for deterministic output."""
    a = EmailCandidate(
        address="a@openai.com", sources=[src("pattern")],
    )
    b = EmailCandidate(
        address="b@openai.com", sources=[src("pattern")],
    )
    p = person_with_employer()
    result = score_all([b, a], p, now=NOW)
    # Both have equal belongs_to_person; sort by address ascending
    assert [c.address for c in result] == ["a@openai.com", "b@openai.com"]


# ---- end-to-end: the worked-example shape -----------------------------------


def test_worked_example_pete_openai_corroborated():
    """The plan's worked example: pete@openai.com gets a git_commit AND
    a pattern hit. Should rank above pattern-only candidates."""
    pete = EmailCandidate(
        address="pete@openai.com",
        sources=[
            src("git_commit", days_ago=12, detail="commit in steipete/lobster-os"),
            src("pattern", detail="matches company pattern 'first' from 3 knowns"),
        ],
    )
    pattern_alt = EmailCandidate(
        address="peter.steinberger@openai.com",
        sources=[src("pattern", detail="matches company pattern from 3 knowns")],
    )
    p = person_with_employer()
    result = score_all([pattern_alt, pete], p, now=NOW)
    assert result[0] is pete
    # pete's belongs should be meaningfully higher than the pattern-only
    assert (pete.belongs_to_person or 0) > (pattern_alt.belongs_to_person or 0) + 0.2


def test_worked_example_personal_gmail_belongs_but_not_work():
    """steipete@gmail.com gets git_commit + WHOIS sources → high belongs,
    but current_work=0 because Gmail."""
    c = EmailCandidate(
        address="steipete@gmail.com",
        sources=[
            src("git_commit", days_ago=6, detail="commit in 4 repos"),
            src("whois", days_ago=10, detail="WHOIS steipete.com"),
        ],
    )
    p = person_with_employer()
    score_candidate(c, p, now=NOW)
    assert c.belongs_to_person is not None
    assert c.belongs_to_person > 0.6
    assert c.current_work_address == 0.0
    assert c.is_personal_provider is True
