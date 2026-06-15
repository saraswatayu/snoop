"""Direct unit tests for lib.pipeline.binding — the stranger-proofing core.

The headline is `candidate_is_bound`: an address binds only when ≥2 INDEPENDENT
evidence classes agree AND at least one is identity-bearing (anchored observation
or rel=me ownership). employer_match and a PGP owner-UID are corroborating but
target-agnostic, so the two of them together must NOT bind — that is precisely
what stops snoop opening a socket to a same-named stranger on the employer domain.
"""

from __future__ import annotations

import pytest

from lib.pipeline.binding import (
    bind_context,
    bound_github_handle,
    candidate_is_bound,
    github_identity_bound,
    reassess_identity,
    verified_personal_domains,
)
from tests.pipeline import cand, person, src


def _github_bound_person():
    """A person whose GitHub handle bound via a validating anchor."""
    return person(
        name="Peter Steinberger",
        handles={"github": "steipete"},
        ambiguity="single_plausible_match",
        bound_anchors=[("github_name_match", "Peter Steinberger"),
                       ("github_handle_exists", "steipete")],
    )


def _rel_me_person(domain: str):
    return person(
        name="Indie Dev",
        personal_domains=[domain],
        ambiguity="single_plausible_match",
        bound_anchors=[("personal_domain_verified", domain)],
    )


# ---- candidate_is_bound: the ≥2-signals truth table -------------------------

def test_manual_known_short_circuits_to_bound():
    c = cand("jane@acme.com", src("manual_known"))
    assert candidate_is_bound(c, person(name="Jane")) is True


def test_non_email_never_binds():
    assert candidate_is_bound(cand("not-an-address", src("manual_known")),
                              person(name="X")) is False


def test_anchored_github_surface_plus_employer_binds():
    """Anchored observation (identity-bearing) + employer_match = 2 signals."""
    c = cand("pete@openai.com", src("git_commit"), employer_match=True)
    assert candidate_is_bound(c, _github_bound_person()) is True


def test_anchored_observation_alone_is_one_signal_not_bound():
    """A single signal — even an identity-bearing one — does not bind."""
    c = cand("pete@personal.dev", src("gh_profile"))
    assert candidate_is_bound(c, _github_bound_person()) is False


def test_employer_plus_pgp_never_binds_no_identity_bearing_signal():
    """The namesake defense: domain-on-employer + a published key are both
    corroborating but target-agnostic. Two of them is 2 signals but ZERO
    identity-bearing, so snoop must NOT bind (and never SMTP-probes it)."""
    c = cand("p.smith@acme.com", src("pattern"), src("pgp"), employer_match=True)
    p = person(name="Pat Smith", ambiguity="single_plausible_match")
    assert candidate_is_bound(c, p) is False


def test_rel_me_owned_domain_plus_employer_binds():
    """rel=me ownership is identity-bearing; + employer_match = 2 signals."""
    c = cand("hi@indie.dev", src("pattern"), employer_match=True)
    assert candidate_is_bound(c, _rel_me_person("indie.dev")) is True


def test_github_surface_without_bound_handle_is_not_anchored():
    """Same github-surface reading, but the handle never bound → not anchored, so
    employer_match alone is one corroborating signal → not bound."""
    p = person(name="X", handles={"github": "guess"},
               ambiguity="insufficient_identity_evidence",
               bound_anchors=[("github_handle_exists", "guess")])
    c = cand("x@acme.com", src("gh_profile"), employer_match=True)
    assert candidate_is_bound(c, p) is False


# ---- bound_github_handle: defense against hallucinated handles --------------

@pytest.mark.parametrize("anchors,ambiguity,expected", [
    ([("github_name_match", "P"), ("github_handle_exists", "h")],
     "single_plausible_match", "h"),
    ([("github_handle_exists", "h")], "insufficient_identity_evidence", None),
    ([("github_handle_exists", "h")], "single_plausible_match", "h"),
])
def test_bound_github_handle_trust_gate(anchors, ambiguity, expected):
    p = person(name="P", handles={"github": "h"}, ambiguity=ambiguity,
               bound_anchors=anchors)
    assert bound_github_handle(p) == expected


def test_bound_github_handle_none_without_handle():
    assert bound_github_handle(person(name="X")) is None


# ---- bind_context hoists the person-level invariants ------------------------

def test_bind_context_reports_invariants():
    p = _rel_me_person("indie.dev")
    rel_me_domains, surface_domains, gh_bound = bind_context(p)
    assert rel_me_domains == {"indie.dev"}
    assert "indie.dev" in surface_domains
    assert gh_bound is False
    assert github_identity_bound(_github_bound_person()) is True
    assert verified_personal_domains(p) == {"indie.dev"}


# ---- reassess_identity: promote on a verified Google name-match -------------

def test_reassess_promotes_single_verified_name_match():
    p = person(name="Jane Doe", ambiguity="insufficient_identity_evidence")
    c = cand("jane@acme.com", src("pattern"),
             account_exists="verified", account_display_name="Jane Doe")
    reassess_identity(p, [c])
    assert p.ambiguity == "single_plausible_match"
    assert any(t == "google_name_match" for t, _ in p.bound_anchors)


def test_reassess_no_promotion_when_two_verified_matches():
    p = person(name="Jane Doe", ambiguity="insufficient_identity_evidence")
    cs = [cand("jane@a.com", src("pattern"), account_exists="verified",
               account_display_name="Jane Doe"),
          cand("jane@b.com", src("pattern"), account_exists="verified",
               account_display_name="Jane Doe")]
    reassess_identity(p, cs)
    assert p.ambiguity == "insufficient_identity_evidence"


def test_reassess_leaves_declared_namesake_alone():
    """A declared real namesake (multiple_plausible_matches) is never auto-promoted."""
    p = person(name="Jane Doe", ambiguity="multiple_plausible_matches")
    c = cand("jane@acme.com", src("pattern"), account_exists="verified",
             account_display_name="Jane Doe")
    reassess_identity(p, [c])
    assert p.ambiguity == "multiple_plausible_matches"
