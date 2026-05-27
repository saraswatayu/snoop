"""Tests for lib/render.py — the contact decision card.

Focus on the structural commitments:
- 3-state ambiguity surfaces in the header
- Decision line is opinionated AND honest about confidence
- Work / Personal split is enforced and consistent with intent
- SMTP inconclusive shows the provider hint, never a deliverability score
- Resolver notes (plan-vs-observed deltas) get surfaced
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lib.render import render_decision_card
from lib.schema import EmailCandidate, Employer, Person, Source


NOW = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)


def candidate(
    addr,
    *,
    belongs=None,
    work=None,
    deliv=None,
    smtp="unprobed",
    provider=None,
    employer_match=False,
    personal_provider=False,
    former_match=False,
    sources=None,
):
    return EmailCandidate(
        address=addr,
        sources=sources or [
            Source(type="git_commit", url=None, observed_at=NOW - timedelta(days=10),
                   detail="commit in repo")
        ],
        smtp_verdict=smtp,
        mx_provider=provider,
        employer_match=employer_match,
        is_personal_provider=personal_provider,
        employer_former_match=former_match,
        belongs_to_person=belongs,
        current_work_address=work,
        deliverable=deliv,
    )


def make_person(**overrides):
    defaults = dict(
        name="Peter Steinberger",
        handles={"github": "steipete"},
        personal_domains=["steipete.com"],
        employer=Employer(name="OpenAI", domains=["openai.com"]),
        ambiguity="single_plausible_match",
        bound_anchors=[
            ("github_name_match", "Peter Steinberger"),
            ("github_employer_match", "OpenAI"),
            ("github_handle_exists", "steipete"),
        ],
    )
    defaults.update(overrides)
    return Person(**defaults)


# ---- header / identity block ------------------------------------------------


def test_header_includes_name_and_employer():
    out = render_decision_card(make_person(), [])
    assert "# Peter Steinberger" in out
    assert "OpenAI" in out
    assert "openai.com" in out


def test_header_includes_ambiguity_state():
    for state, label in [
        ("single_plausible_match", "single plausible match"),
        ("multiple_plausible_matches", "multiple plausible matches"),
        ("insufficient_identity_evidence", "insufficient identity evidence"),
    ]:
        p = make_person(ambiguity=state)
        out = render_decision_card(p, [])
        assert label in out


def test_header_counts_only_validating_anchors():
    """Codex c5: github_handle_exists doesn't count as a validating anchor."""
    p = make_person(
        bound_anchors=[
            ("github_name_match", "Peter Steinberger"),
            ("github_employer_match", "OpenAI"),
            ("github_handle_exists", "steipete"),  # trivial — exclude from count
        ]
    )
    out = render_decision_card(p, [])
    assert "2 identity anchors bound" in out


def test_header_handles_zero_anchors_gracefully():
    p = make_person(bound_anchors=[], ambiguity="insufficient_identity_evidence")
    out = render_decision_card(p, [])
    assert "0 identity anchors bound" in out


def test_header_surfaces_resolver_notes():
    p = make_person(notes=[
        "plan claimed employer='OpenAI'; github profile company='Anthropic' — differs",
        "only 1 identity anchor bound",
    ])
    out = render_decision_card(p, [])
    assert "Resolver notes" in out
    assert "Anthropic" in out
    assert "only 1 identity anchor" in out


# ---- decision line ----------------------------------------------------------


def test_decision_recommends_best_work_address_when_high_confidence():
    p = make_person()
    work_strong = candidate(
        "pete@openai.com", belongs=0.85, work=0.95, deliv=None,
        smtp="inconclusive", provider="microsoft",
        employer_match=True,
    )
    pattern_weak = candidate(
        "peter@openai.com", belongs=0.20, work=0.30, deliv=None,
        smtp="inconclusive", provider="microsoft",
        employer_match=True,
        sources=[Source(type="pattern", url=None, observed_at=NOW, detail="pattern only")],
    )
    out = render_decision_card(p, [pattern_weak, work_strong], intent="work")
    # The high-confidence one is the explicit recommendation
    assert "Best for cold work outreach" in out
    assert "pete@openai.com" in out
    # The low-confidence pattern is in the table, not the decision line
    decision_section = out.split("##")[1]  # everything before next ##
    assert "peter@openai.com" not in decision_section


def test_decision_caveats_inconclusive_smtp():
    p = make_person()
    c = candidate(
        "pete@openai.com", belongs=0.85, work=0.95, deliv=None,
        smtp="inconclusive", provider="microsoft", employer_match=True,
    )
    out = render_decision_card(p, [c], intent="work")
    assert "SMTP inconclusive" in out
    assert "microsoft" in out
    assert "blocks RCPT" in out


def test_decision_caveats_catch_all():
    p = make_person()
    c = candidate(
        "pete@openai.com", belongs=0.85, work=0.85, deliv=0.4,
        smtp="catch_all", provider="other", employer_match=True,
    )
    out = render_decision_card(p, [c], intent="work")
    assert "catch-all" in out.lower()


def test_decision_caveats_smtp_invalid():
    p = make_person()
    c = candidate(
        "wrong@openai.com", belongs=0.30, work=None, deliv=0.05,
        smtp="invalid", employer_match=True,
    )
    out = render_decision_card(p, [c], intent="work")
    assert "rejected" in out.lower() or "bad" in out.lower()


def test_decision_caveats_former_employer_domain():
    p = make_person()
    c = candidate(
        "pete@oldcorp.com", belongs=0.50, work=0.30, deliv=None,
        smtp="unprobed", former_match=True,
    )
    out = render_decision_card(p, [c], intent="work")
    assert "former-employer" in out.lower() or "inactive" in out.lower()


def test_decision_low_confidence_says_so():
    p = make_person()
    weak = candidate(
        "peter@openai.com", belongs=0.20, work=0.30, deliv=None,
        smtp="inconclusive", provider="microsoft", employer_match=True,
        sources=[Source(type="pattern", url=None, observed_at=NOW, detail="pattern only")],
    )
    out = render_decision_card(p, [weak], intent="work")
    # Should say "low confidence" or "verify before sending", not "best for outreach"
    assert "verify before sending" in out.lower() or "low confidence" in out.lower()
    assert "best for cold work outreach" not in out.lower()


def test_decision_intent_personal_picks_personal():
    p = make_person()
    work = candidate(
        "pete@openai.com", belongs=0.85, work=0.95, deliv=None,
        smtp="inconclusive", employer_match=True,
    )
    pers = candidate(
        "steipete@gmail.com", belongs=0.92, work=0.0, deliv=None,
        smtp="unprobed", personal_provider=True,
    )
    out = render_decision_card(p, [work, pers], intent="personal")
    assert "Best personal address" in out
    assert "steipete@gmail.com" in out


def test_decision_warns_when_intent_personal_for_cold_business():
    p = make_person()
    pers = candidate(
        "steipete@gmail.com", belongs=0.92, work=0.0,
        personal_provider=True,
    )
    out = render_decision_card(p, [pers], intent="personal")
    assert "warm outreach" in out or "not cold business" in out


# ---- work / personal split --------------------------------------------------


def test_work_personal_split_is_enforced():
    p = make_person()
    work = candidate(
        "pete@openai.com", belongs=0.85, work=0.95,
        employer_match=True,
    )
    pers = candidate(
        "steipete@gmail.com", belongs=0.92, work=0.0,
        personal_provider=True,
    )
    out = render_decision_card(p, [work, pers], intent="work")
    # Both sections present
    assert "## Work" in out
    assert "## Personal" in out
    # Work section appears first (intent=work)
    work_idx = out.find("## Work")
    pers_idx = out.find("## Personal")
    assert 0 < work_idx < pers_idx


def test_intent_personal_reorders_sections():
    p = make_person()
    work = candidate("pete@openai.com", belongs=0.85, employer_match=True)
    pers = candidate("steipete@gmail.com", belongs=0.92, personal_provider=True)
    out = render_decision_card(p, [work, pers], intent="personal")
    work_idx = out.find("## Work")
    pers_idx = out.find("## Personal")
    assert 0 < pers_idx < work_idx


def test_intent_either_uses_single_combined_table():
    p = make_person()
    cands = [
        candidate("pete@openai.com", belongs=0.85, employer_match=True),
        candidate("steipete@gmail.com", belongs=0.92, personal_provider=True),
    ]
    out = render_decision_card(p, cands, intent="either")
    assert "## All candidates" in out
    assert "## Work" not in out
    assert "## Personal" not in out


# ---- score field rendering --------------------------------------------------


def test_none_scores_render_as_em_dash():
    """Abstention is rendered as '—', not '0.00'."""
    p = make_person()
    c = candidate("x@openai.com", belongs=None, work=None, deliv=None,
                  employer_match=True)
    out = render_decision_card(p, [c], intent="work")
    # The candidate row should contain em-dashes for abstained fields
    assert "—" in out


def test_scores_render_with_two_decimal_places():
    p = make_person()
    c = candidate("x@openai.com", belongs=0.847, work=0.522, deliv=0.123,
                  employer_match=True)
    out = render_decision_card(p, [c], intent="work")
    assert "0.85" in out  # belongs rounded for display
    assert "0.52" in out
    assert "0.12" in out


# ---- channel hints ----------------------------------------------------------


def test_channel_hints_surface_when_present():
    p = make_person(channel_hints={"x_dms_open": True, "prefers": "x"})
    out = render_decision_card(p, [])
    assert "Channel hints" in out
    assert "x_dms_open" in out
    assert "prefers: x" in out


def test_no_channel_hints_means_no_section():
    p = make_person(channel_hints={})
    out = render_decision_card(p, [])
    assert "Channel hints" not in out


# ---- no candidates path -----------------------------------------------------


def test_empty_candidates_decision_explains_no_options():
    p = make_person()
    out = render_decision_card(p, [], intent="work")
    assert "No usable candidates" in out or "channel hints" in out.lower()


def test_work_intent_with_only_personal_addresses():
    """Intent=work but only personal candidates exist — fall back gracefully."""
    p = make_person()
    pers = candidate("steipete@gmail.com", belongs=0.92, personal_provider=True)
    out = render_decision_card(p, [pers], intent="work")
    # Should explain that no current-employer address was found
    assert "no current-employer" in out.lower() or "fallback" in out.lower()


# ---- table truncation -------------------------------------------------------


def test_max_per_section_respected():
    p = make_person()
    cands = [
        candidate(f"p{i}@openai.com", belongs=0.5 - i*0.05, employer_match=True)
        for i in range(10)
    ]
    out = render_decision_card(p, cands, intent="work", max_per_section=3)
    # Count occurrences of "@openai.com" in the work section
    work_section = out.split("## Personal")[0]
    count = work_section.count("@openai.com")
    # 3 in the table + possibly 1 in the decision line = ≤ 4
    assert count <= 4
