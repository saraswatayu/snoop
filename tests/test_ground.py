"""Tests for lib/ground.py — the deterministic verifier.

The model proposes; this module checks the receipts. The headline guarantee: a
fact that cites no real observation is DROPPED (the namesake gate, now enforced
by grounding instead of a hand-written binding rule).
"""

from __future__ import annotations

from types import SimpleNamespace

from lib.ground import GroundedFact, ground


def _obs(id_, content):
    return SimpleNamespace(id=id_, content=content)


def _fact(value="x@y.com", ids=("o1",), kind="email", confidence=0.9):
    return {
        "kind": kind, "label": "", "value": value, "detail": "",
        "confidence": confidence, "evidence_ids": list(ids), "reasoning": "because",
    }


OBS = [
    _obs("o1", "candidate email: alice@corp.com (sources=gh_profile)"),
    _obs("o2", 'profile name: "Alice Smith"'),
]


def test_drops_fact_with_no_valid_citation():
    facts = [_fact(ids=["o99"])]  # o99 does not exist
    assert ground(facts, OBS) == []


def test_drops_fact_with_empty_citations():
    assert ground([_fact(ids=[])], OBS) == []


def test_keeps_fact_and_filters_bogus_ids():
    out = ground([_fact(value="alice@corp.com", ids=["o1", "o99"])], OBS)
    assert len(out) == 1
    assert out[0].evidence_ids == ["o1"]   # o99 dropped, o1 kept
    assert out[0].grounded is True


def test_verified_true_when_value_appears_in_cited_obs():
    out = ground([_fact(value="alice@corp.com", ids=["o1"])], OBS)
    assert out[0].verified is True


def test_verified_false_when_value_absent_from_cited_obs():
    # value cites o2 (the name obs) but the email isn't in it -> not verified,
    # but still grounded (cited a real obs), so it's kept.
    out = ground([_fact(value="alice@corp.com", ids=["o2"])], OBS)
    assert len(out) == 1
    assert out[0].verified is False


def test_verified_matches_on_longest_token_when_whole_value_normalized():
    obs = [_obs("o1", "recent public repo: steipete/PSPDFKit — pdf framework")]
    # model normalized the title differently but the significant token survives
    out = ground([_fact(value="PSPDFKit reader", ids=["o1"], kind="work_item")], obs)
    assert out[0].verified is True


def test_confidence_is_clamped():
    out = ground([_fact(confidence=5.0, ids=["o1"], value="alice@corp.com")], OBS)
    assert out[0].confidence == 1.0
    out = ground([_fact(confidence="junk", ids=["o1"], value="alice@corp.com")], OBS)
    assert out[0].confidence == 0.0


def test_returns_grounded_fact_objects_in_order():
    facts = [
        _fact(value="alice@corp.com", ids=["o1"]),
        _fact(value="Alice Smith", ids=["o2"], kind="consistency_note"),
    ]
    out = ground(facts, OBS)
    assert [f.kind for f in out] == ["email", "consistency_note"]
    assert all(isinstance(f, GroundedFact) for f in out)
