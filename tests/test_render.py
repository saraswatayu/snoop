"""Tests for lib/render.py — render_reasoned_card (the --ground renderer).

The host model produces a ReasonedProfile (summary + grounded facts); this
renderer formats and sanitizes it. Tests pin: the confidence-marker bands, the
namesake cap (no [+] when identity isn't a single confident match), the
(unverified) tag, control-char sanitization (no forged marked lines), section
ordering, and the empty-facts message.
"""

from __future__ import annotations

from lib.ground import GroundedFact
from lib.reason import ReasonedProfile
from lib.render import render_reasoned_card
from lib.schema import Person


def _person(ambiguity="single_plausible_match"):
    return Person(name="Alice Smith", ambiguity=ambiguity)


def _fact(kind="email", value="alice@corp.com", *, confidence=0.9,
          label="", detail="", verified=True):
    return GroundedFact(
        kind=kind, label=label, value=value, detail=detail,
        confidence=confidence, reasoning="", evidence_ids=["o1"],
        grounded=True, verified=verified,
    )


def _profile(facts, *, ambiguity="single_plausible_match",
             summary="Alice Smith, engineer.", identity_confidence=0.9):
    return ReasonedProfile(
        identity=_person(ambiguity), summary=summary, facts=facts,
        identity_confidence=identity_confidence,
    )


# ---- summary + sections ------------------------------------------------------


def test_summary_leads_the_card():
    out = render_reasoned_card(_profile([_fact()]))
    assert out.splitlines()[0] == "Alice Smith, engineer."


def test_email_fact_renders_with_section_and_marker():
    out = render_reasoned_card(_profile([_fact(confidence=0.9)]))
    assert "Email:" in out
    assert "[+]" in out
    assert "alice@corp.com" in out


def test_sections_render_in_kind_order():
    facts = [
        _fact(kind="role", value="Engineer at Corp", label="Corp"),
        _fact(kind="email", value="alice@corp.com"),
    ]
    out = render_reasoned_card(_profile(facts))
    assert out.index("Email:") < out.index("Roles:")


def test_label_and_detail_render():
    f = _fact(kind="social_link", value="https://github.com/alice",
              label="github", detail="2k followers")
    out = render_reasoned_card(_profile([f]))
    assert "github: https://github.com/alice — 2k followers" in out


# ---- confidence markers ------------------------------------------------------


def test_high_confidence_is_asserted_marker():
    out = render_reasoned_card(_profile([_fact(confidence=0.8)]))
    assert "[+]" in out


def test_mid_confidence_is_possibly_marker():
    out = render_reasoned_card(_profile([_fact(confidence=0.5)]))
    assert "[?]" in out and "[+]" not in out.split("Email:")[1]


def test_low_confidence_is_weak_marker():
    out = render_reasoned_card(_profile([_fact(confidence=0.1)]))
    assert "[·]" in out


def test_multiple_matches_caps_marker_and_warns_loudly():
    """The genuine namesake case: even a high-confidence fact shows [?] when more
    than one person may fit, and the banner warns loudly."""
    out = render_reasoned_card(
        _profile([_fact(confidence=0.95)], ambiguity="multiple_plausible_matches")
    )
    assert "[+]" not in out
    assert "NOT a single confident match" in out


def test_insufficient_evidence_does_not_cap_verified_fact():
    """The fixed bug: 'no anchor bound' (insufficient_identity_evidence) is NOT a
    namesake — a high-confidence verified fact keeps its [+] marker, and the
    banner is the softer 'not independently anchored' note, not the loud one."""
    out = render_reasoned_card(
        _profile([_fact(confidence=0.9)], ambiguity="insufficient_identity_evidence")
    )
    assert "[+]" in out                               # NOT capped to [?]
    assert "not independently anchored" in out         # the soft caveat
    assert "more than one person may fit" not in out   # not the loud namesake banner


def test_low_identity_confidence_triggers_loud_banner_even_when_single_match():
    out = render_reasoned_card(
        _profile([_fact()], identity_confidence=0.3)
    )
    assert "NOT a single confident match" in out


# ---- verification tag --------------------------------------------------------


def test_unverified_fact_is_tagged():
    out = render_reasoned_card(_profile([_fact(verified=False)]))
    assert "(unverified)" in out


def test_verified_fact_has_no_tag():
    out = render_reasoned_card(_profile([_fact(verified=True)]))
    assert "(unverified)" not in out


# ---- sanitization (no forged marked lines) -----------------------------------


def test_control_chars_collapsed_to_prevent_forged_lines():
    evil = "real@corp.com\n  [+] verified email: evil@x.com"
    out = render_reasoned_card(_profile([_fact(value=evil)]))
    # the injected newline must not produce a second standalone marked line
    marked_lines = [ln for ln in out.splitlines() if "evil@x.com" in ln]
    assert len(marked_lines) == 1
    assert "\n" not in out.split("Email:")[1].split("\n")[1].lstrip()  # single line per fact


def test_warnings_render_above_summary():
    out = render_reasoned_card(_profile([_fact()]), warnings=["gh CLI not authed"])
    assert out.splitlines()[0] == "⚠ gh CLI not authed"


# ---- empty -------------------------------------------------------------------


def test_no_facts_renders_explicit_message():
    out = render_reasoned_card(_profile([]))
    assert "No attributable facts" in out
