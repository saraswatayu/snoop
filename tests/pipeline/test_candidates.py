"""Direct unit tests for lib.pipeline.candidates — clustering + ranking algebra."""

from __future__ import annotations

from lib.pipeline.candidates import (
    cluster_candidates,
    pattern_template,
    primary_localparts,
    probe_rank,
    speculative_rank,
)
from lib.schema import ResolverResult
from tests.pipeline import cand, src


def _result(resolver, *candidates):
    return ResolverResult(resolver=resolver, candidates=list(candidates), status="ok")


# ---- cluster_candidates -----------------------------------------------------

def test_cluster_merges_same_address_across_resolvers():
    clustered = cluster_candidates([
        _result("git_emails", cand("pete@openai.com", src("git_commit", url="u1"))),
        _result("gh_profile", cand("pete@openai.com", src("gh_profile", url="u2"))),
    ])
    assert len(clustered) == 1
    assert {s.type for s in clustered[0].sources} == {"git_commit", "gh_profile"}


def test_cluster_lowercases_address_for_display():
    clustered = cluster_candidates([
        _result("gh_profile", cand("Pete@OpenAI.COM", src("gh_profile"))),
    ])
    assert clustered[0].address == "pete@openai.com"


def test_cluster_dedupes_identical_source_and_ors_flags():
    clustered = cluster_candidates([
        _result("personal_site", cand("x@y.com", src("personal_site", url="same"))),
        _result("personal_site", cand("x@y.com", src("personal_site", url="same"),
                                      employer_match=True)),
    ])
    assert len(clustered) == 1
    assert len(clustered[0].sources) == 1          # exact (type,url) dup dropped
    assert clustered[0].employer_match is True     # flag OR-merged in


# ---- probe_rank: observed before pure guesses -------------------------------

def test_probe_rank_orders_observed_before_pattern_then_by_corroboration():
    observed_two = cand("a@x.com", src("git_commit"), src("gh_profile"))
    observed_one = cand("b@x.com", src("personal_site"))
    pattern_only = cand("c@x.com", src("pattern"))
    ranked = sorted([pattern_only, observed_one, observed_two], key=probe_rank)
    assert [c.address for c in ranked] == ["a@x.com", "b@x.com", "c@x.com"]


# ---- primary_localparts -----------------------------------------------------

def test_primary_localparts_none_for_empty_name():
    assert primary_localparts("") is None


def test_primary_localparts_returns_lowercased_set_for_a_name():
    out = primary_localparts("Jibben Hillen")
    assert out is not None and len(out) > 0
    assert all(lp == lp.lower() for lp in out)


# ---- pattern_template / speculative_rank ------------------------------------

def test_pattern_template_extracts_named_template():
    assert pattern_template(cand("x@y.com",
                                 src("pattern", detail="generic template 'flast'"))) == "flast"
    assert pattern_template(cand("x@y.com", src("pattern", detail="no template here"))) is None


def test_speculative_rank_puts_company_inferred_first():
    inferred = cand("a@y.com", src("pattern", detail="matches company pattern 'first'"))
    plain = cand("b@y.com", src("pattern", detail="generic template 'flast'"))
    assert sorted([plain, inferred], key=speculative_rank)[0].address == "a@y.com"
