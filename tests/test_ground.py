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


def test_url_value_not_verified_by_shared_host_token():
    """A profile URL must NOT verify against a DIFFERENT profile on the same
    host: 'github.com/janedoe' shares only the generic host token 'github.com'
    with 'github.com/someoneelse', so the longest-token fallback used to stamp
    it verified — laundering a namesake URL as source-confirmed."""
    obs = [_obs("o1", "see https://github.com/someoneelse for code")]
    out = ground([_fact(value="https://github.com/janedoe", ids=["o1"],
                        kind="social_link")], obs)
    assert out[0].verified is False


def test_url_value_verified_when_distinctive_segment_present():
    obs = [_obs("o1", "their account is github.com/janedoe (confirmed)")]
    out = ground([_fact(value="https://github.com/janedoe", ids=["o1"],
                        kind="social_link")], obs)
    assert out[0].verified is True


def test_ground_preserves_verdict_and_marker():
    """The analyst emits a per-fact verdict (email deliverability) and marker
    ([+]/[?] belonging) per SKILL.md; --ground must PRESERVE them, not strip
    them — otherwise the machine output the evals grade omits fields production
    was told to produce."""
    facts = [{
        "kind": "email", "label": "", "value": "alice@corp.com", "detail": "",
        "confidence": 0.9, "evidence_ids": ["o1"], "reasoning": "",
        "verdict": "verified", "marker": "[+]",
    }]
    out = ground(facts, OBS)
    assert out[0].verdict == "verified"
    assert out[0].marker == "[+]"


def test_ground_defaults_verdict_marker_to_none():
    out = ground([_fact(value="alice@corp.com", ids=["o1"])], OBS)
    assert out[0].verdict is None
    assert out[0].marker is None


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


def test_string_evidence_ids_is_one_citation_not_char_iteration():
    """A model that emits evidence_ids as a bare string must be treated as ONE
    citation id, not iterated character-by-character (which silently drops a real
    multi-char id, or char-matches a single-char one)."""
    fact = _fact(value="alice@corp.com")
    fact["evidence_ids"] = "o1"  # a bare string, not a list
    out = ground([fact], OBS)
    assert len(out) == 1
    assert out[0].evidence_ids == ["o1"]


def test_non_list_evidence_ids_drops_the_fact():
    """A non-list, non-string evidence_ids (dict/number) is malformed: no valid
    citation, so the fact is dropped (fail closed) rather than grounding via key
    iteration."""
    fact = _fact()
    fact["evidence_ids"] = {"o1": True}
    assert ground([fact], OBS) == []
