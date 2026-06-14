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
             summary="Alice Smith, engineer.", identity_confidence: float | None = 0.9):
    return ReasonedProfile(
        identity=_person(ambiguity), summary=summary, facts=facts,
        identity_confidence=identity_confidence,
    )


# ---- robustness --------------------------------------------------------------


def test_render_tolerates_nonnumeric_identity_confidence():
    """--ground stdin is untrusted: a model that emits identity_confidence as a
    string ('high', or '0.8' quoted) must not crash the render with a TypeError
    on the `ic >= 0.75` comparison — it degrades to 'no host confidence'."""
    out = render_reasoned_card(_profile([_fact()], identity_confidence="high"))  # type: ignore[arg-type]
    assert "alice@corp.com" in out  # rendered, did not raise


def test_out_of_vocab_fact_kind_is_surfaced_not_dropped():
    """ground() no longer constrains a fact's kind to a vocabulary, so a
    grounded fact with an unexpected kind must still render — silently dropping
    it would lose a cited, grounded fact from the card."""
    facts = [_fact(kind="affiliation", value="Board member, Acme Foundation")]
    out = render_reasoned_card(_profile(facts))
    assert "Board member, Acme Foundation" in out


# ---- summary + sections ------------------------------------------------------


def test_summary_leads_the_card():
    out = render_reasoned_card(_profile([_fact()]))
    assert out.splitlines()[0] == "Alice Smith, engineer."


def test_email_fact_renders_with_section_and_marker():
    out = render_reasoned_card(_profile([_fact(confidence=0.9)]))
    assert "Email:" in out
    assert "✓" in out
    assert "alice@corp.com" in out


def test_sections_render_in_kind_order():
    facts = [
        _fact(kind="role", value="Engineer at Corp", label="Corp"),
        _fact(kind="email", value="alice@corp.com"),
    ]
    out = render_reasoned_card(_profile(facts))
    assert out.index("Email:") < out.index("Roles:")


def test_label_kept_when_not_redundant():
    """A label that adds info (not already in the value) is shown."""
    f = _fact(kind="role", value="Founding team", label="Simile", detail="current")
    out = render_reasoned_card(_profile([f]))
    assert "Simile: Founding team — current" in out


def test_redundant_label_is_dropped():
    """A label that just repeats the value (e.g. 'linkedin' when the value is the
    linkedin URL) is dropped to keep the line scannable."""
    f = _fact(kind="social_link", value="linkedin.com/in/alice", label="linkedin")
    out = render_reasoned_card(_profile([f]))
    assert "✓ linkedin.com/in/alice" in out
    assert "linkedin: linkedin.com" not in out


# ---- confidence markers ------------------------------------------------------


def test_high_confidence_is_asserted_marker():
    out = render_reasoned_card(_profile([_fact(confidence=0.8)]))
    assert "✓" in out


def test_mid_confidence_is_possibly_marker():
    out = render_reasoned_card(_profile([_fact(confidence=0.5)]))
    assert "~" in out and "✓" not in out.split("Email:")[1]


def test_low_confidence_is_weak_marker():
    out = render_reasoned_card(_profile([_fact(confidence=0.1)]))
    assert "·" in out


def test_multiple_matches_caps_marker_and_warns_loudly():
    """The genuine namesake case: even a high-confidence fact shows [?] when more
    than one person may fit, and the banner warns loudly."""
    out = render_reasoned_card(
        _profile([_fact(confidence=0.95)], ambiguity="multiple_plausible_matches")
    )
    assert "✓" not in out
    assert "NOT a single confident match" in out


def test_insufficient_evidence_does_not_cap_verified_fact():
    """The fixed bug: 'no anchor bound' (insufficient_identity_evidence) is NOT a
    namesake — a high-confidence verified fact keeps its [+] marker, and the
    banner is the softer 'not anchored' note, not the loud one."""
    out = render_reasoned_card(
        _profile([_fact(confidence=0.9)], ambiguity="insufficient_identity_evidence",
                 identity_confidence=None)
    )
    assert "✓" in out                               # NOT capped to [?]
    assert "not independently anchored" in out         # the soft caveat
    assert "more than one person may fit" not in out   # not the loud namesake banner


def test_high_host_confidence_suppresses_soft_banner():
    """When the host says it confirmed the identity another way (high
    identity_confidence) — e.g. a WebFetched public profile snoop can't sense —
    the soft 'not anchored' banner is suppressed; the host's call wins."""
    out = render_reasoned_card(
        _profile([_fact(confidence=0.9)],
                 ambiguity="insufficient_identity_evidence", identity_confidence=0.85)
    )
    assert "not independently anchored" not in out
    assert "✓" in out


def test_soft_banner_shows_when_host_confidence_absent_or_modest():
    # omitted identity_confidence → cautious default, banner shows
    out = render_reasoned_card(
        _profile([_fact()], ambiguity="insufficient_identity_evidence",
                 identity_confidence=None)
    )
    assert "not independently anchored" in out


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


def test_honest_blank_shows_checked_not_checked_and_why():
    """4A: a zero-fact card given the bundle's run records enumerates what ran,
    what was not checked, and the reason for each — a designed empty state, not a
    silent dead end."""
    sensors = [
        {"sensor": "git_emails", "status": "ran", "outcome": "empty"},
        {"sensor": "pattern_gen", "status": "ran", "outcome": "candidates"},
        {"sensor": "personal_site", "status": "skipped",
         "reason": "no personal_domains in plan"},
        {"sensor": "smtp", "status": "skipped", "reason": "--no-smtp"},
        {"sensor": "google_account", "status": "degraded", "reason": "deadline-exceeded"},
    ]
    out = render_reasoned_card(_profile([]), sensors=sensors)
    assert "What snoop checked:" in out
    assert "ran: git_emails, pattern_gen" in out
    assert "· personal_site — no personal_domains in plan" in out
    assert "· smtp — --no-smtp" in out
    assert "· google_account — deadline-exceeded" in out


def test_honest_blank_absent_without_sensors():
    """Without the run records, the empty card is just the one-liner (back-compat)."""
    out = render_reasoned_card(_profile([]))
    assert "What snoop checked:" not in out


def test_honest_blank_not_shown_when_facts_exist():
    """The checked/not-checked surface is the EMPTY-state affordance; a card with
    facts doesn't carry the sensor summary (that lives on stderr)."""
    sensors = [{"sensor": "smtp", "status": "skipped", "reason": "--no-smtp"}]
    out = render_reasoned_card(_profile([_fact()]), sensors=sensors)
    assert "What snoop checked:" not in out


def test_honest_blank_sanitizes_sensor_reason():
    """An untrusted reason can't forge a marked card line (3B/_oneline applies)."""
    sensors = [{"sensor": "smtp", "status": "degraded",
                "reason": "boom\n  ✓ verified evil@x.com"}]
    out = render_reasoned_card(_profile([]), sensors=sensors)
    assert "\n  ✓ verified evil@x.com" not in out
    assert "boom ✓ verified evil@x.com" in out


# ---- press-confirmed-role tier (TODOS P2: ~ with a cited basis, never ✓) -----


def test_press_confirmed_role_renders_tilde_not_check():
    """A role confirmed by independent press (not self-published) is provenance
    strength, not identity-binding — the host lands it in the `~` band, and the
    renderer must show `~`, never `✓`. (Per the evidence-tier table.)"""
    role = _fact(kind="role", value="Acme", detail="VP Eng · per TechCrunch",
                 confidence=0.5)
    out = render_reasoned_card(_profile([role]))
    line = next(ln for ln in out.splitlines() if "Acme" in ln)
    assert line.strip().startswith("~")
    assert "✓" not in line


def test_m365_provider_context_renders_separate_from_address():
    """T-minor B: M365 provider context lands in its OWN section (Identity check),
    visually separate from the Email line — it must never read as validating the
    address. The address line carries only its own verdict."""
    email = _fact(kind="email", value="marta@helio.com", detail="pattern-guess",
                  confidence=0.4)
    note = _fact(kind="consistency_note", value="helio.com is on Microsoft 365",
                 detail="M365 blocks RCPT and has no existence oracle — lean on channels",
                 confidence=0.5)
    out = render_reasoned_card(_profile([email, note]))
    lines = out.splitlines()
    email_line = next(ln for ln in lines if "marta@helio.com" in ln)
    # the provider context is NOT on the address line
    assert "Microsoft 365" not in email_line and "existence oracle" not in email_line
    # and it appears under its own section header
    assert "Identity check:" in out


def test_self_published_role_can_render_check():
    """The cap is by confidence/doctrine, not a blanket role downgrade: a
    self-published or probe-verified role the host scores in the ✓ band renders
    `✓` — so the press-confirmed `~` is a deliberate tier, not an accident."""
    role = _fact(kind="role", value="Simile", detail="founding team · current",
                 confidence=0.9)
    out = render_reasoned_card(_profile([role]))
    line = next(ln for ln in out.splitlines() if "Simile" in ln)
    assert line.strip().startswith("✓")
