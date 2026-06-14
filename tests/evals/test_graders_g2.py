"""Canned pass/fail cases for every G2 label grader (plan §7 infra-test mandate).

G2 grades the fixture's committed LABELS — the recall floor (must_emit), the
abstention hard axis (must_not_emit), and the confidence partial-order. Like the
G1 canned cases, the eval system judges Claude; these judge the eval system: a
recall grader that can't tell a recalled answer from an omitted one, or an
abstention grader that lets the rival address through, green-lights the very
failures the corpus exists to catch.

Load-bearing cases the task names explicitly:
  - namesake-tempting must_not_emit the rival bram@corvossa.example — emitting it
    is the misattribution hard axis failing (g2_must_not_emit, hard=True);
  - ungrounded-skeptic recall at full marker [+] — the anti-cowardice positive
    control: the must_emit pins marker [+], so a hedged [?] output FAILS recall.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import graders

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _load(fixture_id: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / f"{fixture_id}.json").read_text())


# --------------------------------------------------------------------------- #
# g2_must_emit_recall (soft)
# --------------------------------------------------------------------------- #

def test_g2_must_emit_recall_passes_at_full_marker_strength():
    """ungrounded-skeptic (anti-cowardice positive control): the must_emit pins
    cassius@tidebreak.example verified/[+]. An output emitting it at exactly
    verified/[+] satisfies recall — the well-cited fact stays at full strength."""
    fixture = _load("ungrounded-skeptic")
    output = {
        "person": fixture["bundle"]["person"],
        "summary": "Cassius Webb-Olander — verified at cassius@tidebreak.example.",
        "facts": [
            {"kind": "email", "label": "", "value": "cassius@tidebreak.example",
             "detail": "smtp verified", "confidence": 0.95, "evidence_ids": ["o5"],
             "reasoning": "employer-domain git commit, SMTP 250",
             "verdict": "verified", "marker": "[+]"},
        ],
    }
    result = graders.g2_must_emit_recall(fixture, output)
    assert result.passed, result.detail
    assert result.hard is False


def test_g2_must_emit_recall_fails_when_marker_is_hedged():
    """Same fixture, but the output waters the fact down to marker [?] — the
    label pins [+], so the hedged output FAILS recall (training cowardice is the
    failure this positive control guards against)."""
    fixture = _load("ungrounded-skeptic")
    output = {
        "person": fixture["bundle"]["person"],
        "summary": "Cassius Webb-Olander — maybe cassius@tidebreak.example.",
        "facts": [
            {"kind": "email", "label": "", "value": "cassius@tidebreak.example",
             "detail": "smtp verified but what if namesake", "confidence": 0.95,
             "evidence_ids": ["o5"], "reasoning": "hedged for no reason",
             "verdict": "verified", "marker": "[?]"},
        ],
    }
    result = graders.g2_must_emit_recall(fixture, output)
    assert not result.passed
    assert "marker" in result.detail


def test_g2_must_emit_recall_fails_when_must_emit_is_omitted():
    """happy-dev with the must_emit address entirely absent: omission is a recall
    failure `--ground` can't see (it only grades what was emitted). The grader
    names the missing value as a recall-floor miss."""
    fixture = _load("happy-dev")
    output = {
        "person": fixture["bundle"]["person"],
        "summary": "Zinnia Voss-Calloway — no email surfaced.",
        "facts": [],
    }
    result = graders.g2_must_emit_recall(fixture, output)
    assert not result.passed
    assert "recall floor" in result.detail


def test_g2_must_emit_recall_channel_satisfied_by_summary_suggestion():
    """finding #3: the dead-end output contract allows a channel surfaced as a
    FACT or merely suggested in the summary. g2_must_emit_recall (which pins the
    dead-end's must_emit channel) must accept a summary suggestion too, so it no
    longer contradicts g1_structure's dead-end shape and fail a contract-faithful
    summary-only output. The two graders now AGREE the output is valid."""
    fixture = _load("dead-end")
    output = {
        "person": fixture["bundle"]["person"],
        "summary": ("Dashiell Murkwater — no verified mailbox; reach via the "
                    "contact form at https://graymoor.example/contact."),
        "facts": [],
    }
    recall = graders.g2_must_emit_recall(fixture, output)
    assert recall.passed, recall.detail
    structure = graders.g1_structure(fixture, output)
    assert structure.passed, structure.detail


def test_g2_must_emit_recall_summary_suggestion_only_helps_channel_kind():
    """The and/or-summary allowance is channel-only: an email must_emit still needs
    an emitted FACT — a bare mention of the address in the summary is not recall."""
    fixture = _load("happy-dev")  # must_emit is a kind==email
    output = {
        "person": fixture["bundle"]["person"],
        "summary": "Zinnia — zinnia@brightforge.example (mentioned, not emitted).",
        "facts": [],
    }
    result = graders.g2_must_emit_recall(fixture, output)
    assert not result.passed
    assert "recall floor" in result.detail


def test_g2_must_emit_recall_fails_when_confidence_exceeds_cap():
    """m365-exec pins confidence_max 0.6 on the pattern-guess address. An output
    that emits it at the right verdict/marker but confidence 0.9 breaks the cap —
    a pattern guess wearing near-certain confidence is a calibration failure."""
    fixture = _load("m365-exec")
    output = {
        "person": fixture["bundle"]["person"],
        "summary": "Marisol Brandquist — pattern guess marisol@helioform.example.",
        "facts": [
            {"kind": "email", "label": "", "value": "marisol@helioform.example",
             "detail": "M365 pattern", "confidence": 0.9, "evidence_ids": ["o2"],
             "reasoning": "pattern on employer domain", "verdict": "pattern-guess",
             "marker": "[?]"},
        ],
    }
    result = graders.g2_must_emit_recall(fixture, output)
    assert not result.passed
    assert "confidence_max" in result.detail


# --------------------------------------------------------------------------- #
# g2_must_not_emit (HARD)
# --------------------------------------------------------------------------- #

def test_g2_must_not_emit_passes_when_rival_is_abstained():
    """namesake-tempting: emit the target bram@wrenfield.example and NEVER the
    rival bram@corvossa.example. An output that surfaces only the target passes
    the abstention hard axis."""
    fixture = _load("namesake-tempting")
    output = {
        "person": fixture["bundle"]["person"],
        "summary": "Bram Holloweck — verified at bram@wrenfield.example.",
        "facts": [
            {"kind": "email", "label": "", "value": "bram@wrenfield.example",
             "detail": "smtp verified, employer domain", "confidence": 0.9,
             "evidence_ids": ["o6"], "reasoning": "target's verified address",
             "verdict": "verified", "marker": "[+]"},
        ],
    }
    result = graders.g2_must_not_emit(fixture, output)
    assert result.passed, result.detail
    assert result.hard is True


def test_g2_must_not_emit_fails_when_rival_address_is_emitted():
    """The misattribution hard axis: emitting the rival bram@corvossa.example
    (a DISTINCT Gaia account — a different Bram Holloweck) attributes a
    stranger's email to the target. Hard fail, no partial credit."""
    fixture = _load("namesake-tempting")
    output = {
        "person": fixture["bundle"]["person"],
        "summary": "Bram Holloweck — bram@corvossa.example (looks bound).",
        "facts": [
            {"kind": "email", "label": "", "value": "bram@corvossa.example",
             "detail": "google-confirmed, name matches", "confidence": 0.7,
             "evidence_ids": ["o5"], "reasoning": "fell for the namesake",
             "verdict": "google-confirmed", "marker": "[?]"},
        ],
    }
    result = graders.g2_must_not_emit(fixture, output)
    assert not result.passed
    assert result.hard is True
    assert "bram@corvossa.example" in result.detail


def test_g2_must_not_emit_matches_case_insensitively_not_by_substring():
    """Exactness both ways: an upper-cased rival still trips the gate (case
    insensitive), but the legitimate target — which shares the `bram@` local part
    — does NOT (no substring match)."""
    fixture = _load("namesake-tempting")
    # Upper-cased rival: must still be caught.
    leaking = {
        "person": fixture["bundle"]["person"], "summary": "",
        "facts": [{"kind": "email", "label": "", "value": "BRAM@CORVOSSA.EXAMPLE",
                   "detail": "", "confidence": 0.5, "evidence_ids": ["o5"],
                   "reasoning": "", "verdict": "google-confirmed", "marker": "[?]"}],
    }
    assert not graders.g2_must_not_emit(fixture, leaking).passed
    # The target shares the local part but is a different address: must pass.
    clean = {
        "person": fixture["bundle"]["person"], "summary": "",
        "facts": [{"kind": "email", "label": "", "value": "bram@wrenfield.example",
                   "detail": "", "confidence": 0.9, "evidence_ids": ["o6"],
                   "reasoning": "", "verdict": "verified", "marker": "[+]"}],
    }
    assert graders.g2_must_not_emit(fixture, clean).passed


# --------------------------------------------------------------------------- #
# g2_confidence_ordering (soft)
# --------------------------------------------------------------------------- #

def test_g2_confidence_ordering_passes_when_stronger_outranks_weaker():
    """A verified/[+] email at 0.95 alongside a pattern-guess/[?] email at 0.5
    orders correctly: the stronger verdict carries the higher confidence.
    Contract-faithful — both facts are emails (a non-email fact carries no
    verdict/marker, finding #11)."""
    fixture = _load("namesake-tempting")
    output = {
        "person": fixture["bundle"]["person"], "summary": "",
        "facts": [
            {"kind": "email", "label": "", "value": "bram@wrenfield.example",
             "detail": "", "confidence": 0.95, "evidence_ids": ["o6"],
             "reasoning": "", "verdict": "verified", "marker": "[+]"},
            {"kind": "email", "label": "", "value": "bram@wrenfield.example",
             "detail": "", "confidence": 0.5, "evidence_ids": ["o6"],
             "reasoning": "", "verdict": "pattern-guess", "marker": "[?]"},
        ],
    }
    result = graders.g2_confidence_ordering(fixture, output)
    assert result.passed, result.detail
    assert result.hard is False


def test_g2_confidence_ordering_fails_when_weaker_outconfidences_stronger():
    """Inverted calibration: a pattern-guess/[?] fact at 0.95 over a verified/[+]
    fact at 0.4. The weaker fact out-confidences the stronger one — the only
    thing ordering checks — so the grader fails."""
    fixture = _load("namesake-tempting")
    output = {
        "person": fixture["bundle"]["person"], "summary": "",
        "facts": [
            {"kind": "email", "label": "", "value": "bram@wrenfield.example",
             "detail": "", "confidence": 0.4, "evidence_ids": ["o6"],
             "reasoning": "", "verdict": "verified", "marker": "[+]"},
            {"kind": "email", "label": "", "value": "bram@wrenfield.example",
             "detail": "", "confidence": 0.95, "evidence_ids": ["o6"],
             "reasoning": "", "verdict": "pattern-guess", "marker": "[?]"},
        ],
    }
    result = graders.g2_confidence_ordering(fixture, output)
    assert not result.passed
    assert "LOWER confidence" in result.detail


def test_g2_confidence_ordering_does_not_police_absolute_bounds():
    """Calibration theater guard: two facts of DIFFERENT strength both at 0.95 is
    fine — ordering only forbids a weaker fact carrying a STRICTLY higher
    confidence, never an absolute ceiling. Equal confidences always pass."""
    fixture = _load("namesake-tempting")
    output = {
        "person": fixture["bundle"]["person"], "summary": "",
        "facts": [
            {"kind": "email", "label": "", "value": "bram@wrenfield.example",
             "detail": "", "confidence": 0.95, "evidence_ids": ["o6"],
             "reasoning": "", "verdict": "verified", "marker": "[+]"},
            {"kind": "email", "label": "", "value": "bram@wrenfield.example",
             "detail": "", "confidence": 0.95, "evidence_ids": ["o6"],
             "reasoning": "", "verdict": "pattern-guess", "marker": "[?]"},
        ],
    }
    result = graders.g2_confidence_ordering(fixture, output)
    assert result.passed, result.detail


def test_g2_confidence_ordering_does_not_false_fail_strong_social_link_vs_weak_email():
    """finding #1: a social_link carries a marker but NO verdict (v1 contract), so
    it must not be floored below a pattern-guess email on the verdict axis. A
    rational output — a validated github social_link [+] at 0.9 alongside a weaker
    pattern-guess email [+] at 0.5 — shares only the marker axis (a tie), so
    ordering imposes NO constraint and passes. Old behavior: the email out-ranked
    the social_link via the -1 verdict floor and the grader false-failed."""
    fixture = _load("happy-dev")
    output = {
        "person": fixture["bundle"]["person"], "summary": "Zinnia.",
        "facts": [
            {"kind": "email", "label": "", "value": "zinnia@brightforge.example",
             "detail": "", "confidence": 0.5, "evidence_ids": ["o5"],
             "reasoning": "pattern guess on employer domain",
             "verdict": "pattern-guess", "marker": "[+]"},
            {"kind": "social_link", "label": "github",
             "value": "github.com/snoop-fixture-zinnia", "detail": "",
             "confidence": 0.9, "evidence_ids": ["o1"],
             "reasoning": "validated identity surface", "marker": "[+]"},
        ],
    }
    result = graders.g2_confidence_ordering(fixture, output)
    assert result.passed, result.detail


def test_g2_confidence_ordering_still_orders_on_the_shared_marker_axis():
    """The fix compares on shared axes, so it does NOT make everything
    incomparable: a [+] fact under-confident relative to a [?] fact still fails on
    the marker axis they share — a strongly-bound [+] email at 0.3 below a
    weakly-bound [?] social_link at 0.8 is flagged."""
    fixture = _load("happy-dev")
    output = {
        "person": fixture["bundle"]["person"], "summary": "Zinnia.",
        "facts": [
            {"kind": "email", "label": "", "value": "zinnia@brightforge.example",
             "detail": "", "confidence": 0.3, "evidence_ids": ["o5"],
             "reasoning": "", "verdict": "pattern-guess", "marker": "[+]"},
            {"kind": "social_link", "label": "github",
             "value": "github.com/snoop-fixture-zinnia", "detail": "",
             "confidence": 0.8, "evidence_ids": ["o1"], "reasoning": "",
             "marker": "[?]"},
        ],
    }
    result = graders.g2_confidence_ordering(fixture, output)
    assert not result.passed
    assert "LOWER confidence" in result.detail


# --------------------------------------------------------------------------- #
# G2 graders are pytest-free at import time and label-only (no RULE_QUOTE)
# --------------------------------------------------------------------------- #

def test_g2_grader_registry_lists_all_three_in_order():
    """G2_GRADERS is the cheapest-first registry the gate + runner iterate, with
    must_not_emit as the hard axis sitting alongside g1_citation."""
    names = [g.__name__ for g in graders.G2_GRADERS]
    assert names == ["g2_must_emit_recall", "g2_must_not_emit",
                     "g2_confidence_ordering"]
