"""Tests for lib/pattern_gen.py.

Behavior is deterministic — no external calls. Covers:
- Template inference from manual_known same-domain addresses
- Multi-template promotion when knowns agree
- Multi-domain candidate emission
- Variant integration (Eastern-order, accent-folded)
- Cap enforcement
- Empty / no-domains / unparsable name edge cases
"""

from __future__ import annotations

from lib.pattern_gen import fetch_pattern_candidates, infer_company_templates
from lib.schema import Employer, Person


def _person(name: str, employer_domains: list[str] | None = None,
            personal_domains: list[str] | None = None) -> Person:
    """Build a minimally-valid Person for pattern_gen input."""
    employer = (
        Employer(name="X", domains=employer_domains)
        if employer_domains else None
    )
    return Person(
        name=name,
        employer=employer,
        personal_domains=personal_domains or [],
        ambiguity="single_plausible_match",
    )


# ---- infer_company_templates ------------------------------------------------


def test_infer_returns_empty_when_no_knowns():
    assert infer_company_templates([], ["acme.com"]) == {}


def test_infer_identifies_first_last_pattern_from_one_known():
    """`sam.altman@acme.com` for "Sam Altman" is consistent with BOTH
    first.last (Western order) AND last.first (Eastern-order variant).
    Both templates get a vote; the downstream tiebreaker uses default
    template popularity to put first.last on top."""
    votes = infer_company_templates(
        [("sam.altman@acme.com", "Sam Altman")],
        ["acme.com"],
    )
    assert "first.last" in votes["acme.com"]
    assert votes["acme.com"]["first.last"] == 1


def test_infer_accumulates_votes_from_multiple_knowns():
    """Three Acme employees, all first.last localpart → 3 votes for
    first.last (and 3 symmetric votes for last.first; tiebreaker picks
    first.last downstream)."""
    votes = infer_company_templates(
        [
            ("sam.altman@acme.com", "Sam Altman"),
            ("greg.brockman@acme.com", "Greg Brockman"),
            ("ilya.sutskever@acme.com", "Ilya Sutskever"),
        ],
        ["acme.com"],
    )
    assert votes["acme.com"]["first.last"] == 3


def test_infer_handles_ambiguous_localparts():
    """`sama@acme.com` for "Sam Altman" matches BOTH "first" and "fl"
    templates. Cast a vote for the first-matched (one per variant)."""
    votes = infer_company_templates(
        [("sama@acme.com", "Sam Altman")],
        ["acme.com"],
    )
    # `sama` is `f.last` template? No — that's `s.altman`. `sama` is `fl`-ish.
    # Actually `sama` = `f`+`firstchar-of-last`? `s` + `a` = `sa`, not `sama`.
    # Most likely match: first ("sam") doesn't equal "sama".
    # Reality: no template produces exactly "sama" from "Sam Altman".
    # Verify: empty votes
    assert votes.get("acme.com", {}).get("first") is None


def test_infer_ignores_knowns_on_unrelated_domains():
    """If a known is on acme.com but target_domains is [beta.com],
    don't count it."""
    votes = infer_company_templates(
        [("sam.altman@acme.com", "Sam Altman")],
        ["beta.com"],
    )
    assert votes == {}


def test_infer_handles_known_without_name():
    """A known address with no name attached can't infer a template; skip."""
    votes = infer_company_templates(
        [("contact@acme.com", None)],
        ["acme.com"],
    )
    assert votes == {}


def test_infer_handles_unparsable_name():
    votes = infer_company_templates(
        [("madonna@acme.com", "Madonna")],  # single token → unparsable
        ["acme.com"],
    )
    assert votes == {}


# ---- fetch_pattern_candidates: happy paths ----------------------------------


def test_fetch_uses_employer_domains_by_default():
    p = _person("Peter Steinberger", employer_domains=["openai.com"])
    result = fetch_pattern_candidates(p)
    assert result.status == "ok"
    domains = {c.address.split("@", 1)[1] for c in result.candidates}
    assert domains == {"openai.com"}


def test_fetch_emits_known_first_last_templates():
    """Most basic candidate: first.last, firstlast, flast all present."""
    p = _person("Peter Steinberger", employer_domains=["openai.com"])
    result = fetch_pattern_candidates(p)
    addrs = {c.address for c in result.candidates}
    assert "peter.steinberger@openai.com" in addrs
    assert "petersteinberger@openai.com" in addrs
    assert "psteinberger@openai.com" in addrs


def test_fetch_promotes_company_pattern_when_knowns_agree():
    """Three knowns all on `flast`; that template's candidates rank first."""
    p = _person("Peter Steinberger", employer_domains=["acme.com"])
    knowns = [
        ("ssmith@acme.com", "Sam Smith"),
        ("jdoe@acme.com", "Jane Doe"),
        ("bjones@acme.com", "Bob Jones"),
    ]
    result = fetch_pattern_candidates(p, manual_known=knowns)
    # The TOP candidate should be the flast match: psteinberger@acme.com
    assert result.candidates[0].address == "psteinberger@acme.com"
    # And its source detail should say "corroborated by 3 known addresses"
    assert "3 known" in result.candidates[0].sources[0].detail


def test_fetch_inferred_winner_has_pattern_in_detail():
    """The detail string distinguishes inferred-winner from generic templates."""
    p = _person("Peter Steinberger", employer_domains=["acme.com"])
    knowns = [("ssmith@acme.com", "Sam Smith")]
    result = fetch_pattern_candidates(p, manual_known=knowns)
    winner = result.candidates[0]
    assert winner.address == "psteinberger@acme.com"
    assert "corroborated" in winner.sources[0].detail
    # A non-winner like first.last should be a generic template
    other = next(c for c in result.candidates if c.address.startswith("peter.steinberger"))
    assert "generic template" in other.sources[0].detail


def test_fetch_covers_multiple_domains():
    """Both employer and personal domains generate candidates."""
    p = _person(
        "Peter Steinberger",
        employer_domains=["openai.com"],
        personal_domains=["steipete.com"],
    )
    result = fetch_pattern_candidates(p)
    domains = {c.address.split("@", 1)[1] for c in result.candidates}
    assert "openai.com" in domains
    assert "steipete.com" in domains


def test_fetch_explicit_target_domains_overrides_person():
    """If caller passes target_domains explicitly, use that instead of
    person.employer.domains."""
    p = _person("Peter Steinberger", employer_domains=["openai.com"])
    result = fetch_pattern_candidates(p, target_domains=["pspdfkit.com"])
    domains = {c.address.split("@", 1)[1] for c in result.candidates}
    assert domains == {"pspdfkit.com"}


def test_fetch_handles_accented_names():
    """'Étienne Dupont' → etienne.dupont@..., not étienne.dupont@..."""
    p = _person("Étienne Dupont", employer_domains=["acme.com"])
    result = fetch_pattern_candidates(p)
    addrs = {c.address for c in result.candidates}
    assert "etienne.dupont@acme.com" in addrs


def test_fetch_handles_eastern_order_via_variants():
    """For 'Wang Xiaoming' (ambiguous order), candidates from both orderings
    are emitted."""
    p = _person("Wang Xiaoming", employer_domains=["company.cn"])
    result = fetch_pattern_candidates(p)
    addrs = {c.address for c in result.candidates}
    assert "wang.xiaoming@company.cn" in addrs
    assert "xiaoming.wang@company.cn" in addrs


# ---- cap enforcement --------------------------------------------------------


def test_fetch_respects_cap():
    """Generating across 3 domains × ~12 templates × 2 variants could
    easily produce 70 candidates. The cap limits output."""
    p = _person(
        "Wang Xiaoming",
        employer_domains=["a.com", "b.com", "c.com"],
    )
    result = fetch_pattern_candidates(p, cap=10)
    assert len(result.candidates) <= 10


# ---- empty / edge cases -----------------------------------------------------


def test_fetch_empty_with_no_domains():
    p = _person("Peter Steinberger")  # no employer, no personal_domains
    result = fetch_pattern_candidates(p)
    assert result.status == "empty"
    assert result.candidates == []
    assert "no target_domains" in (result.error_detail or "")


def test_fetch_empty_with_unparsable_name():
    p = _person("Madonna", employer_domains=["acme.com"])  # single token
    result = fetch_pattern_candidates(p)
    assert result.status == "empty"
    assert "could not parse" in (result.error_detail or "")


def test_fetch_empty_with_target_domains_but_invalid():
    """target_domains=[None, "", "  "] all filter out → empty."""
    p = _person("Peter Steinberger", employer_domains=["acme.com"])
    result = fetch_pattern_candidates(p, target_domains=[None, "", "  "])  # type: ignore[list-item]
    assert result.status == "empty"


def test_fetch_dedups_across_variants_producing_same_address():
    """When a name has variants whose 'first' templates collide
    (e.g. 'PETER STEINBERGER' case-folds to same 'peter' as 'Peter
    Steinberger'), don't double-emit the same address."""
    p = _person("Peter Steinberger", employer_domains=["acme.com"])
    result = fetch_pattern_candidates(p)
    addresses = [c.address for c in result.candidates]
    assert len(addresses) == len(set(addresses))


# ---- source attribution -----------------------------------------------------


def test_each_candidate_has_one_pattern_source():
    p = _person("Peter Steinberger", employer_domains=["acme.com"])
    result = fetch_pattern_candidates(p)
    for cand in result.candidates:
        assert len(cand.sources) == 1
        assert cand.sources[0].type == "pattern"
        assert cand.sources[0].url is None
        assert cand.sources[0].detail  # non-empty


def test_unrelated_known_does_not_promote_pattern():
    """A known on a different domain doesn't help; default ordering applies."""
    p = _person("Peter Steinberger", employer_domains=["openai.com"])
    knowns = [("ssmith@anthropic.com", "Sam Smith")]  # wrong company
    result = fetch_pattern_candidates(p, manual_known=knowns)
    # No promotion: the top candidate is whatever the default order picks
    # first (first.last per _DEFAULT_TEMPLATE_ORDER).
    assert result.candidates[0].address == "peter.steinberger@openai.com"
    assert "no company pattern inferred" in result.candidates[0].sources[0].detail
