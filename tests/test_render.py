"""Tests for lib/render.py — the contact decision card.

After the 2026-05-28 redesign, the card is verdict-bucket-driven:
  - verified         (SMTP RCPT 250)
  - google-confirmed (Google People API existence + catch-all/inconclusive SMTP)
  - pattern-guess    (no positive verification, just a guess)
  - dead-end         (nothing usable)

Default output is compact: name → employer, address · verdict, dossier
(About block + recent repos), Why provenance, optional Note for name
disambiguation, asymmetric fallback list.

The old per-section tables and identity-anchor jargon live behind
verbose=True. Tests below split by mode.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lib.render import render_decision_card
from lib.schema import EmailCandidate, Employer, GitHubRepo, Person, Source


NOW = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)


def candidate(
    addr,
    *,
    belongs=None,
    work=None,
    deliv=None,
    smtp="unprobed",
    provider=None,
    account_exists="unprobed",
    account_display_name=None,
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
        account_exists=account_exists,
        account_display_name=account_display_name,
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


# ---- warnings (capability degradations) -------------------------------------


def test_warnings_render_at_top_with_warning_glyph():
    p = make_person()
    out = render_decision_card(
        p, [], warnings=["gh CLI not authenticated — run `gh auth login`"],
    )
    # First non-empty line is the warning
    first_line = out.split("\n")[0]
    assert first_line.startswith("⚠ ")
    assert "gh CLI" in first_line


def test_warnings_appear_before_name_header():
    p = make_person()
    c = candidate("peter@steipete.com", belongs=0.95, smtp="verified")
    out = render_decision_card(
        p, [c], warnings=["dnspython not installed"],
    )
    warning_idx = out.find("⚠")
    name_idx = out.find("Peter Steinberger")
    assert warning_idx < name_idx


def test_multiple_warnings_each_get_a_line():
    p = make_person()
    out = render_decision_card(
        p, [], warnings=[
            "gh CLI not authenticated — run `gh auth login`",
            "dnspython not installed — pip install --user dnspython",
        ],
    )
    warning_lines = [line for line in out.splitlines() if line.startswith("⚠ ")]
    assert len(warning_lines) == 2


def test_no_warnings_means_no_warning_block():
    p = make_person()
    c = candidate("peter@steipete.com", belongs=0.95, smtp="verified")
    out = render_decision_card(p, [c], warnings=None)
    assert "⚠" not in out.split("Peter Steinberger")[0]


def test_empty_warnings_list_means_no_warning_block():
    p = make_person()
    c = candidate("peter@steipete.com", belongs=0.95, smtp="verified")
    out = render_decision_card(p, [c], warnings=[])
    assert "⚠" not in out.split("Peter Steinberger")[0]


# ---- header (default mode) --------------------------------------------------


def test_header_renders_name_arrow_employer():
    out = render_decision_card(make_person(), [])
    # Default header is "Name → Employer"; no markdown H1
    assert "Peter Steinberger → OpenAI" in out
    assert "# Peter Steinberger" not in out  # H1 only in verbose


def test_header_falls_back_to_just_name_without_employer():
    p = make_person(employer=None)
    out = render_decision_card(p, [])
    assert "Peter Steinberger" in out
    assert "→" not in out.split("\n")[0]


def test_header_identity_jargon_hidden_in_default():
    """The 'X identity anchors bound' line is internal jargon — hidden by
    default, surfaced only with verbose=True."""
    p = make_person(bound_anchors=[])
    out = render_decision_card(p, [])
    assert "identity anchor" not in out
    assert "insufficient identity evidence" not in out


def test_header_resolver_notes_hidden_in_default():
    p = make_person(notes=["only 1 identity anchor bound — handle is plausibly..."])
    out = render_decision_card(p, [])
    assert "Resolver notes" not in out
    assert "only 1 identity anchor" not in out


# ---- verdict buckets --------------------------------------------------------


def test_verdict_verified_when_smtp_rcpt_clean():
    p = make_person()
    c = candidate(
        "peter@steipete.com", belongs=0.95, work=0.0, deliv=0.95,
        smtp="verified",
    )
    out = render_decision_card(p, [c], intent="either")
    assert "verified" in out
    assert "google-confirmed" not in out
    assert "pattern-guess" not in out


def test_verdict_google_confirmed_when_gaia_id_catch_all():
    """The Dan Neil case: Google says the account exists, but the domain is
    catch-all so SMTP can't double-check. NOT downgraded to pattern-guess."""
    p = make_person(
        name="Daniel Neil",
        employer=Employer(name="Formation Bio", domains=["formation.bio"]),
    )
    c = candidate(
        "daniel@formation.bio", belongs=0.40, work=0.50, deliv=0.85,
        smtp="catch_all", account_exists="verified",
        account_display_name="Daniel Neil",
        employer_match=True,
    )
    out = render_decision_card(p, [c], intent="work")
    assert "google-confirmed" in out
    assert "catch-all" in out  # The caveat shows up adjacent
    assert "pattern-guess" not in out


def test_verdict_google_confirmed_when_inconclusive_smtp():
    p = make_person()
    c = candidate(
        "pete@openai.com", belongs=0.85, work=0.95, deliv=0.85,
        smtp="inconclusive", provider="microsoft",
        account_exists="verified",
        employer_match=True,
    )
    out = render_decision_card(p, [c], intent="work")
    assert "google-confirmed" in out


def test_verdict_pattern_guess_when_no_existence_signal():
    p = make_person()
    c = candidate(
        "peter@openai.com", belongs=0.20, work=0.30, deliv=None,
        smtp="inconclusive", provider="microsoft",
        employer_match=True,
        sources=[Source(type="pattern", url=None, observed_at=NOW,
                        detail="generic template 'first'")],
    )
    out = render_decision_card(p, [c], intent="work")
    assert "pattern-guess" in out


def test_verdict_dead_end_when_no_candidates():
    p = make_person()
    out = render_decision_card(p, [], intent="work")
    assert "No deliverable address found" in out


# ---- About block (dossier) --------------------------------------------------


def test_about_block_renders_when_dossier_fields_present():
    p = make_person(
        gh_bio="Building stuff",
        gh_blog="https://steipete.com/blog",
        gh_twitter="steipete",
        gh_location="Vienna",
    )
    c = candidate("peter@steipete.com", belongs=0.95, smtp="verified")
    out = render_decision_card(p, [c], intent="either")
    assert "About:" in out
    assert "github.com/steipete" in out
    assert "Building stuff" in out
    # Web entry strips the https:// prefix for display compactness
    assert "steipete.com/blog" in out
    assert "@steipete" in out
    assert "Vienna" in out


def test_about_block_skipped_when_no_fields():
    """If nothing in the dossier, don't render an empty About: header."""
    p = make_person(handles={})
    c = candidate("peter@steipete.com", belongs=0.95, smtp="verified")
    out = render_decision_card(p, [c], intent="either")
    assert "About:" not in out


def test_about_block_collapses_bio_whitespace():
    """Multi-line bios from real profiles (e.g., 'Building things.\\n\\nPreviously
    at X') must collapse to a single line so the dossier stays scannable."""
    p = make_person(gh_bio="Building things.\n\nPreviously at OpenAI.\n\tNow exploring.")
    c = candidate("peter@steipete.com", belongs=0.95, smtp="verified")
    out = render_decision_card(p, [c], intent="either")
    # The bio should appear on a single line with single spaces between words
    assert "Building things. Previously at OpenAI. Now exploring." in out
    # No literal newlines or tabs inside the bio section
    about_start = out.find("About:")
    next_section = out.find("\n\n", about_start + 1)
    about_block = out[about_start:next_section if next_section >= 0 else len(out)]
    # The About block itself has line breaks between rows; the bio row
    # specifically should not contain stray \n or \t
    bio_line = next(line for line in about_block.splitlines() if "Building things" in line)
    assert "\n" not in bio_line and "\t" not in bio_line


def test_about_block_includes_linkedin_from_channel_hints():
    p = make_person(channel_hints={"linkedin": "https://www.linkedin.com/in/steipete"})
    c = candidate("peter@steipete.com", belongs=0.95, smtp="verified")
    out = render_decision_card(p, [c], intent="either")
    assert "LinkedIn" in out
    assert "linkedin.com/in/steipete" in out


def test_about_block_surfaces_gh_company_only_when_differs_from_employer():
    """When GitHub's company text matches the canonical employer name,
    it's redundant. When it differs, it's worth seeing."""
    p_match = make_person(gh_company="OpenAI")
    p_diff = make_person(gh_company="Anthropic")
    c = candidate("peter@openai.com", belongs=0.95, smtp="verified",
                  employer_match=True)
    out_match = render_decision_card(p_match, [c], intent="work")
    out_diff = render_decision_card(p_diff, [c], intent="work")
    assert "GH company" not in out_match  # redundant with header
    assert "GH company" in out_diff
    assert "Anthropic" in out_diff


# ---- recent repos block -----------------------------------------------------


def test_recent_repos_block_renders():
    repos = [
        GitHubRepo(name="steipete/InterposeKit", description="Method swizzling for Swift",
                   html_url="https://github.com/steipete/InterposeKit",
                   pushed_at="2026-05-20T10:00:00Z"),
        GitHubRepo(name="steipete/dotfiles", description=None,
                   html_url="https://github.com/steipete/dotfiles",
                   pushed_at="2026-05-15T10:00:00Z"),
    ]
    p = make_person(gh_recent_repos=repos)
    c = candidate("peter@steipete.com", belongs=0.95, smtp="verified")
    out = render_decision_card(p, [c], intent="either")
    assert "Recent on GitHub:" in out
    assert "steipete/InterposeKit" in out
    assert "Method swizzling for Swift" in out
    assert "steipete/dotfiles" in out


def test_recent_repos_block_skipped_when_empty():
    p = make_person()  # no recent repos
    c = candidate("peter@steipete.com", belongs=0.95, smtp="verified")
    out = render_decision_card(p, [c], intent="either")
    assert "Recent on GitHub" not in out


def test_recent_repos_block_caps_at_three_by_default():
    repos = [
        GitHubRepo(name=f"steipete/repo{i}", description=f"desc {i}",
                   html_url=f"https://github.com/steipete/repo{i}",
                   pushed_at="2026-05-20T10:00:00Z")
        for i in range(10)
    ]
    p = make_person(gh_recent_repos=repos)
    c = candidate("peter@steipete.com", belongs=0.95, smtp="verified")
    out = render_decision_card(p, [c], intent="either")
    # Top 3 shown; #4+ omitted
    assert "steipete/repo0" in out
    assert "steipete/repo2" in out
    assert "steipete/repo3" not in out


# ---- name disambiguation ----------------------------------------------------


def test_name_disambiguation_note_when_diminutive_used():
    """Target 'Dan Neil', GitHub profile says 'Daniel Neil'. Surface that."""
    p = make_person(name="Dan Neil", gh_name="Daniel Neil")
    c = candidate("daniel@formation.bio", belongs=0.40, smtp="catch_all",
                  account_exists="verified")
    out = render_decision_card(p, [c], intent="either")
    assert "Note:" in out
    assert "Dan" in out and "Daniel" in out


def test_no_disambiguation_note_when_names_match():
    p = make_person(gh_name="Peter Steinberger")
    c = candidate("peter@steipete.com", belongs=0.95, smtp="verified")
    out = render_decision_card(p, [c], intent="either")
    # No name-disambiguation note (Why line still uses Note: in some scenarios
    # but the disambiguation-specific one is absent)
    assert "differs" not in out
    assert 'you said' not in out


# ---- caveats ----------------------------------------------------------------


def test_inconclusive_smtp_caveat_suppressed_when_google_confirmed():
    """Don't duplicate caveats already in the verdict label."""
    p = make_person()
    c = candidate(
        "pete@openai.com", belongs=0.85, work=0.95, deliv=0.85,
        smtp="inconclusive", provider="microsoft",
        account_exists="verified",
        employer_match=True,
    )
    out = render_decision_card(p, [c], intent="work")
    # Verdict label says SMTP blocked, no need for a separate ⚠ row
    assert out.count("blocks RCPT") <= 1


def test_smtp_invalid_caveat_shows():
    p = make_person()
    c = candidate("wrong@openai.com", belongs=0.30, smtp="invalid",
                  employer_match=True)
    out = render_decision_card(p, [c], intent="work")
    assert "rejected" in out.lower() or "bad" in out.lower()


def test_former_employer_caveat_shows():
    p = make_person()
    c = candidate("pete@oldcorp.com", belongs=0.50, smtp="unprobed",
                  former_match=True)
    out = render_decision_card(p, [c], intent="work")
    assert "former-employer" in out.lower() or "inactive" in out.lower()


# ---- fallback list (D1-C asymmetry) -----------------------------------------


def test_fallback_list_hidden_when_verified():
    """SMTP-verified pick means no bounce expected — fallbacks would be noise."""
    p = make_person()
    pick = candidate("peter@openai.com", belongs=0.95, smtp="verified",
                     employer_match=True)
    other = candidate("p@openai.com", belongs=0.30, employer_match=True,
                      sources=[Source(type="pattern", url=None, observed_at=NOW,
                                      detail="pattern")])
    out = render_decision_card(p, [pick, other], intent="work")
    assert "If it bounces" not in out


def test_fallback_list_shown_when_google_confirmed():
    """Real-but-not-double-verified: show backup order cheaply."""
    p = make_person()
    pick = candidate("pete@openai.com", belongs=0.85, smtp="catch_all",
                     account_exists="verified", employer_match=True)
    backup = candidate("p.steinberger@openai.com", belongs=0.30,
                       employer_match=True,
                       sources=[Source(type="pattern", url=None, observed_at=NOW,
                                       detail="pattern")])
    out = render_decision_card(p, [pick, backup], intent="work")
    assert "If it bounces" in out
    assert "p.steinberger@openai.com" in out


def test_fallback_list_shown_when_pattern_guess():
    p = make_person()
    pick = candidate("peter@openai.com", belongs=0.30, smtp="inconclusive",
                     employer_match=True,
                     sources=[Source(type="pattern", url=None, observed_at=NOW,
                                     detail="pattern")])
    backup = candidate("p@openai.com", belongs=0.20, smtp="inconclusive",
                       employer_match=True,
                       sources=[Source(type="pattern", url=None, observed_at=NOW,
                                       detail="pattern")])
    out = render_decision_card(p, [pick, backup], intent="work")
    assert "If it bounces" in out


def test_fallback_list_excludes_pick():
    p = make_person()
    pick = candidate("pete@openai.com", belongs=0.85, smtp="catch_all",
                     account_exists="verified", employer_match=True)
    backup = candidate("p@openai.com", belongs=0.30, employer_match=True,
                       sources=[Source(type="pattern", url=None, observed_at=NOW,
                                       detail="pattern")])
    out = render_decision_card(p, [pick, backup], intent="work")
    # Pick appears once in the lead (with backticks). Fallback list shows others.
    fallback_section = out.split("If it bounces")[1] if "If it bounces" in out else ""
    assert "pete@openai.com" not in fallback_section


# ---- intent handling --------------------------------------------------------


def test_intent_personal_picks_personal_address():
    p = make_person()
    work = candidate("pete@openai.com", belongs=0.85, employer_match=True,
                     smtp="verified")
    pers = candidate("steipete@gmail.com", belongs=0.92, personal_provider=True,
                     smtp="verified")
    out = render_decision_card(p, [work, pers], intent="personal")
    # The lead address line should be the personal one
    first_lines = "\n".join(out.split("\n")[:3])
    assert "steipete@gmail.com" in first_lines
    assert "pete@openai.com" not in first_lines


def test_intent_personal_warns_when_for_cold_business():
    p = make_person()
    pers = candidate("steipete@gmail.com", belongs=0.92, personal_provider=True,
                     smtp="verified")
    out = render_decision_card(p, [pers], intent="personal")
    assert "warm outreach" in out or "not cold business" in out


def test_work_intent_falls_back_to_personal_with_warning():
    p = make_person()
    pers = candidate("steipete@gmail.com", belongs=0.92, personal_provider=True,
                     smtp="verified")
    out = render_decision_card(p, [pers], intent="work")
    assert "no current-employer" in out.lower() or "fallback" in out.lower()


# ---- dead-end ---------------------------------------------------------------


def test_dead_end_explains_what_was_tried():
    p = make_person()
    out = render_decision_card(p, [], intent="work")
    assert "No deliverable address found" in out


def test_dead_end_suggests_channel_hints():
    p = make_person(channel_hints={"linkedin": "linkedin.com/in/steipete",
                                    "x_dms_open": True})
    out = render_decision_card(p, [], intent="work")
    assert "Try:" in out
    assert "linkedin" in out.lower()


# ---- verbose mode -----------------------------------------------------------


def test_verbose_surfaces_identity_block():
    p = make_person()
    out = render_decision_card(p, [], verbose=True)
    assert "identity anchors bound" in out
    assert "single plausible match" in out


def test_verbose_surfaces_resolver_notes():
    p = make_person(notes=[
        "plan claimed employer='OpenAI'; github profile company='Anthropic' — differs",
    ])
    out = render_decision_card(p, [], verbose=True)
    assert "Resolver notes" in out
    assert "Anthropic" in out


def test_verbose_includes_per_section_tables():
    p = make_person()
    work = candidate("pete@openai.com", belongs=0.85, employer_match=True)
    pers = candidate("steipete@gmail.com", belongs=0.92, personal_provider=True)
    out = render_decision_card(p, [work, pers], intent="work", verbose=True)
    assert "## Work" in out
    assert "## Personal" in out


def test_verbose_intent_either_uses_combined_table():
    p = make_person()
    cands = [
        candidate("pete@openai.com", belongs=0.85, employer_match=True),
        candidate("steipete@gmail.com", belongs=0.92, personal_provider=True),
    ]
    out = render_decision_card(p, cands, intent="either", verbose=True)
    assert "## All candidates" in out


def test_verbose_intent_personal_reorders_sections():
    p = make_person()
    work = candidate("pete@openai.com", belongs=0.85, employer_match=True)
    pers = candidate("steipete@gmail.com", belongs=0.92, personal_provider=True)
    out = render_decision_card(p, [work, pers], intent="personal", verbose=True)
    work_idx = out.find("## Work")
    pers_idx = out.find("## Personal")
    assert 0 < pers_idx < work_idx


def test_verbose_table_renders_three_axis_scores():
    p = make_person()
    c = candidate("x@openai.com", belongs=0.847, work=0.522, deliv=0.123,
                  employer_match=True)
    out = render_decision_card(p, [c], intent="work", verbose=True)
    assert "0.85" in out
    assert "0.52" in out
    assert "0.12" in out


def test_verbose_table_renders_none_score_as_em_dash():
    p = make_person()
    c = candidate("x@openai.com", belongs=None, work=None, deliv=None,
                  employer_match=True)
    out = render_decision_card(p, [c], intent="work", verbose=True)
    assert "—" in out


def test_verbose_max_per_section_respected():
    """max_per_section caps rows in the verbose table only. The default-mode
    fallback list has its own bound. Count addresses inside the '## Work'
    table block specifically."""
    p = make_person()
    cands = [
        candidate(f"p{i}@openai.com", belongs=0.5 - i * 0.05, employer_match=True)
        for i in range(10)
    ]
    out = render_decision_card(p, cands, intent="work", verbose=True,
                                max_per_section=3)
    # Slice out the verbose ## Work section
    assert "## Work" in out
    work_start = out.find("## Work")
    after_work = out[work_start:]
    work_end = after_work.find("\n##", 5)  # next section header
    work_section = after_work if work_end < 0 else after_work[:work_end]
    # Count table rows (lines that start with "| `")
    table_rows = sum(1 for line in work_section.splitlines()
                     if line.startswith("| `"))
    assert table_rows == 3


def test_verbose_ambiguity_states_render():
    for state, label in [
        ("single_plausible_match", "single plausible match"),
        ("multiple_plausible_matches", "multiple plausible matches"),
        ("insufficient_identity_evidence", "insufficient identity evidence"),
    ]:
        p = make_person(ambiguity=state)
        out = render_decision_card(p, [], verbose=True)
        assert label in out


def test_verbose_counts_only_validating_anchors():
    """github_handle_exists is the trivial 'we found the user' anchor; it
    doesn't independently bind identity, so it's excluded from the count."""
    p = make_person(
        bound_anchors=[
            ("github_name_match", "Peter Steinberger"),
            ("github_employer_match", "OpenAI"),
            ("github_handle_exists", "steipete"),
        ]
    )
    out = render_decision_card(p, [], verbose=True)
    assert "2 identity anchors bound" in out
