"""Tests for lib/binding.py — the per-fact provenance primitive.

The headline guarantee (IRON RULE): a source that merely mentions the person's
name, with no cross-link back to a bound signal, comes back "unbound" so the
caller drops it. That is what makes free-text search safe (D3).
"""

from __future__ import annotations

from datetime import datetime, timezone

from lib.binding import (
    _host,
    apply_identity_gate,
    bind_and_keep,
    bind_best,
    bind_source,
)
from lib.schema import Identity, SocialLink, Source


def _now():
    return datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _src(type_, url=None, detail="x"):
    return Source(type=type_, url=url, observed_at=_now(), detail=detail)


def _identity(*, anchors=None, personal_domains=None, ambiguity="single_plausible_match"):
    return Identity(
        name="Peter Steinberger",
        handles={"github": "steipete"},
        personal_domains=personal_domains or [],
        bound_anchors=anchors or [],
        ambiguity=ambiguity,
    )


def test_manual_known_is_asserted():
    b = bind_source(_src("manual_known"), _identity())
    assert b.tier == "asserted"


def test_cross_link_bound_domain_is_asserted():
    """A personal domain proven via a github_personal_domain_match anchor binds
    its own pages by construction."""
    ident = _identity(anchors=[("github_personal_domain_match", "steipete.com")])
    b = bind_source(_src("personal_site", url="https://steipete.com/blog/x"), ident)
    assert b.tier == "asserted"


def test_subdomain_of_bound_domain_is_asserted():
    ident = _identity(anchors=[("github_personal_domain_match", "steipete.com")])
    b = bind_source(_src("substack", url="https://blog.steipete.com/feed"), ident)
    assert b.tier == "asserted"


def test_validated_profile_source_is_asserted():
    """>=2 validating anchors == bound handle; profile-derived sources assert."""
    ident = _identity(anchors=[
        ("github_name_match", "Peter Steinberger"),
        ("github_employer_match", "OpenAI"),
    ])
    assert bind_source(_src("gh_profile", url="https://github.com/steipete"), ident).tier == "asserted"


def test_declared_but_unvalidated_domain_is_only_possibly():
    """Codex #2: a domain merely declared in the (untrusted) --person-plan is a
    hint, never asserted on its own."""
    ident = _identity(personal_domains=["steipete.com"], anchors=[])
    b = bind_source(_src("personal_site", url="https://steipete.com/x"), ident)
    assert b.tier == "possibly"


def test_profile_source_with_unbound_handle_is_possibly():
    ident = _identity(anchors=[("github_name_match", "Peter Steinberger")])  # only 1 anchor
    b = bind_source(_src("gh_profile", url="https://github.com/steipete"), ident)
    assert b.tier == "possibly"


def test_namesake_search_hit_is_unbound_DROPPED():
    """IRON RULE: a page that just mentions the name, no cross-link to a bound
    signal, must NOT be attributed."""
    ident = _identity(anchors=[
        ("github_name_match", "Peter Steinberger"),
        ("github_employer_match", "OpenAI"),
    ])
    # a random conference page for some other "Peter Steinberger"
    b = bind_source(_src("substack", url="https://randomconf.example/speakers/p-s"), ident)
    assert b.tier == "unbound"


def test_substring_domain_is_not_a_false_bind():
    """barfoo.com must not bind to a bound 'foo.com' (host parse, not substring)."""
    ident = _identity(anchors=[("github_personal_domain_match", "foo.com")])
    b = bind_source(_src("personal_site", url="https://barfoo.com/x"), ident)
    assert b.tier == "unbound"


def test_bind_best_takes_strongest_source():
    ident = _identity(anchors=[("github_personal_domain_match", "steipete.com")])
    sources = [
        _src("substack", url="https://randomconf.example/x"),   # unbound
        _src("personal_site", url="https://steipete.com/x"),    # asserted
    ]
    assert bind_best(sources, ident).tier == "asserted"


def test_bind_best_empty_is_unbound():
    assert bind_best([], _identity()).tier == "unbound"


def test_identity_gate_caps_asserted_when_ambiguous():
    """D4 Level 1: if we're unsure WHO this is, nothing can be asserted."""
    ambiguous = _identity(ambiguity="multiple_plausible_matches")
    assert apply_identity_gate("asserted", ambiguous) == "possibly"
    assert apply_identity_gate("possibly", ambiguous) == "possibly"


def test_identity_gate_passthrough_when_single_match():
    single = _identity(ambiguity="single_plausible_match")
    assert apply_identity_gate("asserted", single) == "asserted"


def test_provided_channel_is_possibly_not_dropped():
    """A channel_hint (host model found the person's LinkedIn during planning) is
    observed-for-this-person — possibly, not unbound. Distinct from a namesake
    web_search hit."""
    b = bind_source(_src("channel_hint", url="https://linkedin.com/in/steipete"), _identity())
    assert b.tier == "possibly"


def test_github_repo_from_validated_handle_is_asserted():
    ident = _identity(anchors=[
        ("github_name_match", "Peter Steinberger"),
        ("github_employer_match", "OpenAI"),
    ])
    b = bind_source(_src("github_repo", url="https://github.com/steipete/x"), ident)
    assert b.tier == "asserted"


def test_web_search_hit_with_no_crosslink_is_unbound():
    """web_search is namesake-risky: with no cross-link to a bound signal it must
    NOT be attributed (it is NOT a 'provided' channel)."""
    b = bind_source(_src("web_search", url="https://randomconf.example/p-s"), _identity())
    assert b.tier == "unbound"


def test_web_search_on_bound_domain_is_possibly_never_asserted():
    """I1 (first principles): a free-text web_search hit whose URL lands on a
    cross-link-bound personal domain is KEPT but only as 'possibly' — snoop never
    fetched the page to verify the link, so it is not bound-by-construction. The
    crosslink lifts it from unbound (drop) to possibly, never to asserted."""
    ident = _identity(anchors=[("github_personal_domain_match", "steipete.com")])
    b = bind_source(_src("web_search", url="https://steipete.com/talks/x"), ident)
    assert b.tier == "possibly"


def test_non_web_search_on_bound_domain_still_asserts():
    """The web_search cap is specific to free-text discovery: a non-search source
    observed ON the bound domain is snoop's own evidence and still asserts."""
    ident = _identity(anchors=[("github_personal_domain_match", "steipete.com")])
    b = bind_source(_src("github_repo", url="https://steipete.com/x"), ident)
    assert b.tier == "asserted"


def test_host_returns_none_for_non_string_url():
    """Defense-in-depth: host-model search results are untrusted and may carry a
    non-string url/crosslink_url. _host must return None, not raise, so the value
    never reaches url.strip()."""
    assert _host(123) is None          # type: ignore[arg-type]
    assert _host({"x": 1}) is None     # type: ignore[arg-type]
    assert _host(None) is None
    assert _host("   ") is None


def test_bind_and_keep_drops_unbound_and_stamps_tier():
    """The shared helper binds each fact, drops the unbound ones, and stamps
    bind_tier/bind_reasons on the survivors in input order."""
    ident = _identity(anchors=[
        ("github_name_match", "x"), ("github_employer_match", "y"),
    ])
    keep = SocialLink(platform="github", url="https://github.com/x",
                      sources=[_src("gh_profile", url="https://github.com/x")])
    drop = SocialLink(platform="rando", url="https://rando.example/x",
                      sources=[_src("web_search", url="https://rando.example/x")])
    kept = bind_and_keep([keep, drop], ident)
    assert [k.platform for k in kept] == ["github"]
    assert kept[0].bind_tier in ("asserted", "possibly")
    assert kept[0].bind_reasons  # stamped, not left at the default
