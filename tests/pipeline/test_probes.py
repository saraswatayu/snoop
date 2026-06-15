"""Direct unit tests for lib.pipeline.probes — Phase-2 eligibility predicates.

The split that makes snoop stranger-proof: smtp_candidates opens a socket so it
takes only bound candidates the caller passes; the Google existence check is an
authed no-socket call, so the google/speculative selectors MAY include unbound
pattern guesses on a Google-hosted domain.
"""

from __future__ import annotations

from lib.pipeline.probes import (
    autodetect_workspace_domains,
    google_account_candidates,
    google_target_domains,
    smtp_candidates,
    speculative_google_candidates,
)
from tests.pipeline import cand, person, src


# ---- google_target_domains --------------------------------------------------

def test_google_target_domains_always_includes_native_plus_workspace():
    assert google_target_domains(["Acme.com"]) == {"google.com", "acme.com"}
    assert google_target_domains([]) == {"google.com"}


# ---- autodetect_workspace_domains (MX lookup injected) ----------------------

def test_autodetect_adds_google_hosted_skips_personal_and_native():
    cands = [cand("a@acme.com", src("pattern")),
             cand("b@gmail.com", src("pattern")),      # personal provider → skipped
             cand("c@google.com", src("pattern"))]     # native → already covered
    out = autodetect_workspace_domains(
        cands, explicit=[], is_google_hosted_fn=lambda d: True)
    assert out == ["acme.com"]


def test_autodetect_preserves_explicit_domains():
    out = autodetect_workspace_domains(
        [cand("a@nothosted.com", src("pattern"))],
        explicit=["forced.com"], is_google_hosted_fn=lambda d: False)
    assert out == ["forced.com"]


# ---- google_account_candidates ----------------------------------------------

def test_google_account_candidates_filters_to_target_domains_and_unprobed():
    cands = [cand("on@acme.com", src("pattern")),
             cand("off@other.com", src("pattern")),                 # wrong domain
             cand("done@acme.com", src("pattern"), account_exists="verified")]  # already probed
    out = google_account_candidates(cands, ["acme.com"])
    assert [c.address for c in out] == ["on@acme.com"]


# ---- speculative_google_candidates ------------------------------------------

def test_speculative_excludes_bound_and_already_probed():
    p = person(name="Jibben Hillen", ambiguity="single_plausible_match")
    bound = cand("jibben@acme.com", src("git_commit"))
    fresh = cand("jhillen@acme.com", src("pattern"))
    probed = cand("j.hillen@acme.com", src("pattern"), account_exists="not_found")
    out = speculative_google_candidates(
        [bound, fresh, probed], {bound.address}, ["acme.com"], p)
    addrs = {c.address for c in out}
    assert "jibben@acme.com" not in addrs      # already bound → bound path
    assert "j.hillen@acme.com" not in addrs    # already has a verdict
    assert "jhillen@acme.com" in addrs


def test_speculative_selection_never_exceeds_the_cap():
    """The burst is capped so a name-variant blowup can't drain the daily budget.
    (The full cap-slicing path is exercised end-to-end in tests/test_snoop_entry.py;
    here we pin that the selector honors the constant.)"""
    from lib.pipeline.probes import _SPECULATIVE_GOOGLE_CAP
    p = person(name="Jibben Hillen", ambiguity="single_plausible_match")
    fresh = [cand("jibben@acme.com", src("pattern", detail="template 'first'"))]
    out = speculative_google_candidates(fresh, set(), ["acme.com"], p)
    assert len(out) <= _SPECULATIVE_GOOGLE_CAP


# ---- smtp_candidates --------------------------------------------------------

def test_smtp_candidates_excludes_personal_provider_sourceless_and_known_dead():
    cands = [
        cand("ok@acme.com", src("git_commit")),
        cand("nope@gmail.com", src("git_commit")),                  # personal provider
        cand("empty@acme.com"),                                     # no sources
        cand("dead@acme.com", src("pattern"), account_exists="not_found"),  # known dead
    ]
    out = smtp_candidates(cands)
    assert [c.address for c in out] == ["ok@acme.com"]


def test_smtp_candidates_orders_observed_first_and_caps_top_k():
    cands = [cand("p@acme.com", src("pattern")),
             cand("o@acme.com", src("git_commit"))]
    out = smtp_candidates(cands, top_k=1)
    assert [c.address for c in out] == ["o@acme.com"]
