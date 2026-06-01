"""Tests for snoop.py — the entry-point orchestration.

Covers:
- _gh_handle defense against host-hallucinated handles
- cluster_candidates dedup + flag-merge across resolvers
- _smtp_candidates filtering and ordering
- End-to-end pipeline via main() with all resolvers monkeypatched
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

# snoop.py lives at the skill root, not under lib/
_SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

import snoop  # noqa: E402
from lib.schema import EmailCandidate, Employer, Person, ResolverResult, Source  # noqa: E402


NOW = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)


def src(type_, days_ago=10, detail=None, url=None):
    from datetime import timedelta
    return Source(
        type=type_,
        url=url,
        observed_at=NOW - timedelta(days=days_ago),
        detail=detail or f"{type_} source",
    )


# ---- _gh_handle: defense against host hallucination -------------------------


def test_gh_handle_returns_none_when_no_handle():
    p = Person(name="X", ambiguity="insufficient_identity_evidence")
    assert snoop._gh_handle(p) is None


def test_gh_handle_returns_handle_when_anchors_bind():
    p = Person(
        name="Peter Steinberger",
        handles={"github": "steipete"},
        ambiguity="single_plausible_match",
        bound_anchors=[
            ("github_name_match", "Peter Steinberger"),
            ("github_employer_match", "OpenAI"),
            ("github_handle_exists", "steipete"),
        ],
    )
    assert snoop._gh_handle(p) == "steipete"


def test_gh_handle_returns_none_when_only_handle_exists_anchor():
    """Codex c5: handle exists but no validating anchors → treat as untrusted
    hint, don't fan out resolvers."""
    p = Person(
        name="X",
        handles={"github": "untrusted_guess"},
        ambiguity="insufficient_identity_evidence",
        bound_anchors=[("github_handle_exists", "untrusted_guess")],
    )
    assert snoop._gh_handle(p) is None


# ---- cluster_candidates -----------------------------------------------------


def test_cluster_dedupes_same_address_across_resolvers():
    """git_emails finds pete@openai.com (commit) and gh_profile finds the
    same address (profile field). Cluster into ONE candidate with TWO
    sources."""
    git_result = ResolverResult(
        resolver="git_emails",
        candidates=[EmailCandidate(
            address="pete@openai.com",
            sources=[src("git_commit", url="https://github.com/x/y/commit/abc")],
        )],
        status="ok",
    )
    profile_result = ResolverResult(
        resolver="gh_profile",
        candidates=[EmailCandidate(
            address="pete@openai.com",
            sources=[src("gh_profile", url="https://github.com/steipete")],
        )],
        status="ok",
    )
    clustered = snoop.cluster_candidates([git_result, profile_result])
    assert len(clustered) == 1
    assert clustered[0].address == "pete@openai.com"
    source_types = {s.type for s in clustered[0].sources}
    assert source_types == {"git_commit", "gh_profile"}


def test_cluster_drops_exact_duplicate_sources():
    """If the same (type, url) source appears twice across resolvers
    (e.g. profile email matches a commit author email AT the same URL),
    only keep one to avoid inflated corroboration."""
    same_url = "https://example.com/contact"
    r1 = ResolverResult(
        resolver="personal_site",
        candidates=[EmailCandidate(
            address="x@y.com",
            sources=[src("personal_site", url=same_url)],
        )],
        status="ok",
    )
    r2 = ResolverResult(
        resolver="personal_site",
        candidates=[EmailCandidate(
            address="x@y.com",
            sources=[src("personal_site", url=same_url)],
        )],
        status="ok",
    )
    clustered = snoop.cluster_candidates([r1, r2])
    assert len(clustered[0].sources) == 1


def test_cluster_or_merges_domain_level_flags():
    """If resolver A flagged employer_match but B did not, the merged
    candidate keeps employer_match=True."""
    matched = EmailCandidate(address="x@openai.com", employer_match=True,
                             sources=[src("git_commit")])
    unmatched = EmailCandidate(address="x@openai.com", employer_match=False,
                               sources=[src("pattern")])
    clustered = snoop.cluster_candidates([
        ResolverResult(resolver="A", candidates=[unmatched], status="ok"),
        ResolverResult(resolver="B", candidates=[matched], status="ok"),
    ])
    assert clustered[0].employer_match is True


def test_cluster_preserves_verification_fields_when_present():
    """If one input has a verdict on smtp_verdict / account_exists /
    mx_provider / account_display_name and another doesn't, the merged
    candidate keeps the verdict. Defends against silent drop of pre-probed
    inputs from cached or manual_known resolvers."""
    probed = EmailCandidate(
        address="pete@openai.com",
        sources=[src("manual_known")],
        smtp_verdict="verified",
        account_exists="verified",
        mx_provider="google",
        account_display_name="Pete Steinberger",
    )
    blank = EmailCandidate(
        address="pete@openai.com", sources=[src("pattern")],
    )
    # Both orderings: probed first, then blank-second shouldn't downgrade.
    clustered = snoop.cluster_candidates([
        ResolverResult(resolver="A", candidates=[probed], status="ok"),
        ResolverResult(resolver="B", candidates=[blank], status="ok"),
    ])
    merged = clustered[0]
    assert merged.smtp_verdict == "verified"
    assert merged.account_exists == "verified"
    assert merged.mx_provider == "google"
    assert merged.account_display_name == "Pete Steinberger"

    # Reverse order: blank first, probed second should upgrade.
    clustered = snoop.cluster_candidates([
        ResolverResult(resolver="A", candidates=[
            EmailCandidate(address="pete@openai.com", sources=[src("pattern")])
        ], status="ok"),
        ResolverResult(resolver="B", candidates=[
            EmailCandidate(
                address="pete@openai.com", sources=[src("manual_known")],
                smtp_verdict="verified", account_exists="verified",
                mx_provider="google", account_display_name="Pete Steinberger",
            )
        ], status="ok"),
    ])
    merged = clustered[0]
    assert merged.smtp_verdict == "verified"
    assert merged.account_exists == "verified"
    assert merged.mx_provider == "google"
    assert merged.account_display_name == "Pete Steinberger"


def test_cluster_preserves_distinct_addresses():
    r = ResolverResult(
        resolver="gh_profile",
        candidates=[
            EmailCandidate(address="pete@openai.com", sources=[src("gh_profile")]),
            EmailCandidate(address="steipete@gmail.com", sources=[src("git_commit")]),
        ],
        status="ok",
    )
    clustered = snoop.cluster_candidates([r])
    assert {c.address for c in clustered} == {"pete@openai.com", "steipete@gmail.com"}


def test_cluster_lowercases_address_keys():
    """Two addresses differing only in case should merge."""
    upper = EmailCandidate(address="Pete@OpenAI.COM",
                           sources=[src("git_commit")])
    lower = EmailCandidate(address="pete@openai.com",
                           sources=[src("gh_profile")])
    clustered = snoop.cluster_candidates([
        ResolverResult(resolver="A", candidates=[upper], status="ok"),
        ResolverResult(resolver="B", candidates=[lower], status="ok"),
    ])
    assert len(clustered) == 1


def test_cluster_handles_empty_resolver_results():
    """Resolvers that returned status='timeout'/'unavailable'/'error' with
    empty candidates shouldn't break the cluster."""
    r1 = ResolverResult(resolver="x", candidates=[], status="timeout")
    r2 = ResolverResult(resolver="y", candidates=[
        EmailCandidate(address="real@addr.com", sources=[src("git_commit")])
    ], status="ok")
    clustered = snoop.cluster_candidates([r1, r2])
    assert len(clustered) == 1
    assert clustered[0].address == "real@addr.com"


# ---- _smtp_candidates -------------------------------------------------------


def test_smtp_candidates_filters_personal_providers():
    cands = [
        EmailCandidate(address="pete@gmail.com", belongs_to_person=0.9,
                       sources=[src("git_commit")]),
        EmailCandidate(address="pete@openai.com", belongs_to_person=0.85,
                       sources=[src("git_commit")]),
    ]
    targets = snoop._smtp_candidates(cands)
    addresses = [c.address for c in targets]
    assert "pete@gmail.com" not in addresses
    assert "pete@openai.com" in addresses


def test_smtp_candidates_drops_sourceless():
    """A candidate with no sources isn't worth probing."""
    cands = [
        EmailCandidate(address="empty@openai.com"),  # no sources
        EmailCandidate(address="real@openai.com", belongs_to_person=0.5,
                       sources=[src("pattern")]),
    ]
    targets = snoop._smtp_candidates(cands)
    addresses = [c.address for c in targets]
    assert "empty@openai.com" not in addresses


def test_smtp_candidates_sorts_by_belongs_desc():
    cands = [
        EmailCandidate(address=f"x{i}@openai.com", belongs_to_person=0.1 * i,
                       sources=[src("pattern")])
        for i in range(1, 6)
    ]
    targets = snoop._smtp_candidates(cands)
    scores = [c.belongs_to_person or 0 for c in targets]
    assert scores == sorted(scores, reverse=True)


def test_smtp_candidates_respects_top_k():
    cands = [
        EmailCandidate(address=f"x{i}@openai.com", belongs_to_person=0.5,
                       sources=[src("pattern")])
        for i in range(10)
    ]
    targets = snoop._smtp_candidates(cands, top_k=3)
    assert len(targets) == 3


def test_smtp_candidates_handles_malformed_address():
    cands = [
        EmailCandidate(address="not-an-email", belongs_to_person=0.9,
                       sources=[src("git_commit")]),
        EmailCandidate(address="real@openai.com", belongs_to_person=0.5,
                       sources=[src("git_commit")]),
    ]
    targets = snoop._smtp_candidates(cands)
    assert [c.address for c in targets] == ["real@openai.com"]


# ---- end-to-end via main() --------------------------------------------------


def test_version_flag_prints_and_exits_cleanly(capsys):
    """`snoop --version` should print 'snoop X.Y.Z' and exit 0."""
    import pytest
    from lib import __version__
    with pytest.raises(SystemExit) as exc:
        snoop.main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.strip() == f"snoop {__version__}"


def test_main_diagnose_exits_clean(capsys):
    rc = snoop.main(["--diagnose"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "snoop capability probe" in out


def test_main_pipeline_with_mocked_resolvers(monkeypatch, capsys):
    """Smoke test the full pipeline end-to-end: parse args, build plan,
    resolve, fan out, cluster, score, render. All resolvers monkeypatched
    to return canned results so we don't hit the network."""

    # Mock person_resolve to return a Person with the github handle bound
    def mock_resolve(name, plan=None, gh_caller=None):
        return Person(
            name=name,
            handles={"github": "steipete"},
            personal_domains=["steipete.com"],
            employer=Employer(name="OpenAI", domains=["openai.com"]),
            ambiguity="single_plausible_match",
            bound_anchors=[
                ("github_name_match", name),
                ("github_employer_match", "OpenAI"),
                ("github_handle_exists", "steipete"),
            ],
        )
    monkeypatch.setattr(snoop, "resolve_person", mock_resolve)

    # Each resolver returns one canned candidate
    def mock_git(*args, **kwargs):
        return ResolverResult(
            resolver="git_emails",
            candidates=[EmailCandidate(
                address="pete@openai.com",
                sources=[src("git_commit", url="https://github.com/x/y/commit/abc")],
                employer_match=True,
            )],
            status="ok",
        )

    def mock_profile(*args, **kwargs):
        return ResolverResult(
            resolver="gh_profile",
            candidates=[],
            status="empty",
        )

    def mock_site(*args, **kwargs):
        return ResolverResult(
            resolver="personal_site",
            candidates=[],
            status="empty",
        )

    def mock_pattern(*args, **kwargs):
        return ResolverResult(
            resolver="pattern_gen",
            candidates=[EmailCandidate(
                address="peter.steinberger@openai.com",
                sources=[src("pattern", detail="generic template 'first.last'")],
                employer_match=True,
            )],
            status="ok",
        )

    monkeypatch.setattr(snoop, "fetch_git_emails", mock_git)
    monkeypatch.setattr(snoop, "fetch_gh_profile", mock_profile)
    monkeypatch.setattr(snoop, "fetch_personal_site", mock_site)
    monkeypatch.setattr(snoop, "fetch_pattern_candidates", mock_pattern)
    # Dossier enrichment must also be mocked so the test stays hermetic.
    monkeypatch.setattr(snoop, "fetch_recent_repos", lambda *a, **kw: [])

    # SMTP no-op
    monkeypatch.setattr(snoop, "verify_candidates", lambda cands, **kw: cands)

    rc = snoop.main([
        "Peter Steinberger",
        "--person-plan", '{"handles":{"github":"steipete"}}',
        "--no-smtp",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # New compact header: Name → Employer
    assert "Peter Steinberger → OpenAI" in out
    # Pick named in the lead address line
    assert "pete@openai.com" in out
    # Fallback list shows the lower-confidence pattern address
    assert "peter.steinberger@openai.com" in out
    # Pick is the lead, fallback appears after "If it bounces"
    if "If it bounces" in out:
        bounce_section = out.split("If it bounces")[1]
        assert "peter.steinberger@openai.com" in bounce_section


def test_main_requires_name_or_plan():
    """argparse should reject invocation with neither positional name nor
    --person-plan."""
    import pytest
    with pytest.raises(SystemExit):
        snoop.main([])


def test_load_plan_rejects_malformed_json_with_clean_message(capsys):
    """A malformed --person-plan should exit cleanly with a clear message,
    not crash with a Python traceback from json.loads."""
    import pytest
    with pytest.raises(SystemExit) as exc_info:
        snoop._load_plan('{"bad json')
    msg = str(exc_info.value)
    assert "invalid JSON" in msg
    assert "inline" in msg
    # Should name location of the parse failure
    assert "line" in msg and "col" in msg
    # Should show a snippet of what failed
    assert "near:" in msg


def test_load_plan_rejects_malformed_json_file(tmp_path):
    """File-based --person-plan with malformed JSON gets the same clean
    error, with the file path identified as the source."""
    import pytest
    bad = tmp_path / "plan.json"
    bad.write_text('{"name": "X", "handles":}')
    with pytest.raises(SystemExit) as exc_info:
        snoop._load_plan(f"@{bad}")
    msg = str(exc_info.value)
    assert "invalid JSON" in msg
    assert str(bad) in msg


def test_load_plan_accepts_valid_inline_json():
    plan = snoop._load_plan('{"name": "X", "handles": {"github": "y"}}')
    assert plan == {"name": "X", "handles": {"github": "y"}}


# ---- _autodetect_workspace_domains ------------------------------------------


def test_autodetect_adds_google_hosted_candidate_domains():
    """Candidate addresses on Workspace-hosted domains get auto-added so
    the user doesn't need --google-workspace-domain for every YC startup."""
    cands = [
        EmailCandidate(address="dan@formation.bio"),
        EmailCandidate(address="alice@some-other.io"),
    ]
    is_google = lambda d: d == "formation.bio"
    merged = snoop._autodetect_workspace_domains(
        cands, explicit=[], is_google_hosted_fn=is_google,
    )
    assert "formation.bio" in merged
    assert "some-other.io" not in merged


def test_autodetect_preserves_explicit_workspace_list():
    cands = [EmailCandidate(address="x@formation.bio")]
    merged = snoop._autodetect_workspace_domains(
        cands, explicit=["acme.com", "beta.io"],
        is_google_hosted_fn=lambda d: True,
    )
    assert "acme.com" in merged
    assert "beta.io" in merged
    assert "formation.bio" in merged


def test_autodetect_skips_personal_providers():
    """Don't ramp probing into gmail.com etc. just because they're on Google MX."""
    cands = [EmailCandidate(address="someone@gmail.com")]
    calls = []
    def is_google(d):
        calls.append(d)
        return True
    merged = snoop._autodetect_workspace_domains(
        cands, explicit=[], is_google_hosted_fn=is_google,
    )
    assert merged == []
    assert "gmail.com" not in calls  # never even probed


def test_autodetect_skips_explicit_and_native_google():
    """Don't re-probe domains the caller already named or the native google.com."""
    cands = [
        EmailCandidate(address="x@google.com"),
        EmailCandidate(address="x@already-listed.com"),
    ]
    calls = []
    def is_google(d):
        calls.append(d)
        return True
    merged = snoop._autodetect_workspace_domains(
        cands, explicit=["already-listed.com"],
        is_google_hosted_fn=is_google,
    )
    # Neither domain hit the lookup
    assert calls == []
    assert merged == ["already-listed.com"]


def test_plan_from_flags_with_only_name():
    args = snoop._build_parser().parse_args(["Dan Neil"])
    plan = snoop._plan_from_flags(args)
    assert plan == {"name": "Dan Neil"}


def test_plan_from_flags_with_name_and_employer():
    args = snoop._build_parser().parse_args(["Dan Neil", "Formation Bio"])
    plan = snoop._plan_from_flags(args)
    assert plan == {"name": "Dan Neil", "employer": {"name": "Formation Bio"}}


def test_plan_from_flags_with_full_zero_config_set():
    args = snoop._build_parser().parse_args([
        "Dan Neil", "Formation Bio",
        "--domain", "formation.bio",
        "--github", "danielneil",
    ])
    plan = snoop._plan_from_flags(args)
    assert plan == {
        "name": "Dan Neil",
        "employer": {"name": "Formation Bio", "domains": ["formation.bio"]},
        "handles": {"github": "danielneil"},
    }


def test_plan_from_flags_repeated_domain_flag():
    args = snoop._build_parser().parse_args([
        "Jane", "Acme",
        "--domain", "acme.com",
        "--domain", "acme.io",
    ])
    plan = snoop._plan_from_flags(args)
    assert plan["employer"]["domains"] == ["acme.com", "acme.io"]


def test_plan_from_flags_domain_without_employer_name():
    """--domain without a positional employer name still creates an
    employer entry — the resolver can run pattern_gen on the domain
    even without a canonical name."""
    args = snoop._build_parser().parse_args([
        "Jane",
        "--domain", "acme.com",
    ])
    plan = snoop._plan_from_flags(args)
    assert plan["employer"] == {"domains": ["acme.com"]}


def test_plan_from_flags_empty_when_no_input():
    args = snoop._build_parser().parse_args([])
    plan = snoop._plan_from_flags(args)
    assert plan == {}


def test_capability_warnings_surfaces_gh_unauth():
    from lib.diagnose import Capability
    caps = [
        Capability(name="gh_cli", status="degraded",
                   detail="not authed", impact="anon fallback"),
        Capability(name="dnspython", status="ok",
                   detail="installed", impact="SMTP ok"),
    ]
    warnings = snoop._capability_warnings(caps, allow_google_account=False)
    assert any("gh auth login" in w for w in warnings)
    assert not any("dnspython" in w for w in warnings)


def test_capability_warnings_surfaces_dnspython_missing():
    from lib.diagnose import Capability
    caps = [
        Capability(name="gh_cli", status="ok", detail="ok", impact="ok"),
        Capability(name="dnspython", status="missing",
                   detail="not installed", impact="no SMTP"),
    ]
    warnings = snoop._capability_warnings(caps, allow_google_account=False)
    assert any("dnspython" in w and "pip install" in w for w in warnings)


def test_capability_warnings_skips_google_when_flag_off():
    from lib.diagnose import Capability
    caps = [
        Capability(name="google_account", status="missing",
                   detail="no cookies", impact="--allow-google-account inactive"),
    ]
    # Without --allow-google-account, missing cookies isn't actionable.
    warnings = snoop._capability_warnings(caps, allow_google_account=False)
    assert not any("google" in w.lower() for w in warnings)


def test_capability_warnings_surfaces_google_when_flag_on():
    from lib.diagnose import Capability
    caps = [
        Capability(name="google_account", status="missing",
                   detail="no Google cookies found", impact="path inactive"),
    ]
    warnings = snoop._capability_warnings(caps, allow_google_account=True)
    assert any("Google cookies" in w or "Google" in w for w in warnings)


def test_capability_warnings_no_warnings_when_all_ok():
    from lib.diagnose import Capability
    caps = [
        Capability(name="gh_cli", status="ok", detail="ok", impact="ok"),
        Capability(name="dnspython", status="ok", detail="ok", impact="ok"),
        Capability(name="snoop_state_dir", status="ok", detail="ok", impact="ok"),
    ]
    warnings = snoop._capability_warnings(caps, allow_google_account=False)
    assert warnings == []


def test_autodetect_dedupes_within_candidate_set():
    cands = [
        EmailCandidate(address="a@same.com"),
        EmailCandidate(address="b@same.com"),
        EmailCandidate(address="c@same.com"),
    ]
    calls = []
    def is_google(d):
        calls.append(d)
        return True
    merged = snoop._autodetect_workspace_domains(
        cands, explicit=[], is_google_hosted_fn=is_google,
    )
    assert calls == ["same.com"]  # single lookup despite 3 candidates
    assert merged == ["same.com"]


def test_main_with_json_output_emits_valid_json(monkeypatch, capsys):
    """--json mode should produce parseable JSON."""
    import json as _json

    monkeypatch.setattr(snoop, "resolve_person", lambda name, **kw: Person(
        name=name,
        ambiguity="insufficient_identity_evidence",
    ))
    # All resolvers empty
    empty_result = ResolverResult(resolver="x", candidates=[], status="empty")
    monkeypatch.setattr(snoop, "fetch_git_emails", lambda *a, **kw: empty_result)
    monkeypatch.setattr(snoop, "fetch_gh_profile", lambda *a, **kw: empty_result)
    monkeypatch.setattr(snoop, "fetch_personal_site", lambda *a, **kw: empty_result)
    monkeypatch.setattr(snoop, "fetch_pattern_candidates", lambda *a, **kw: empty_result)

    rc = snoop.main(["X", "--json", "--no-smtp"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = _json.loads(out)
    assert parsed["person"]["name"] == "X"
    assert parsed["candidates"] == []
    # Profile-expansion contract: additive `profile` key with stable sections,
    # existing top-level keys unchanged.
    assert "profile" in parsed
    for section in ("social_links", "channels", "work_items", "roles",
                    "consistency_notes"):
        assert section in parsed["profile"]
        assert isinstance(parsed["profile"][section], list)


def test_format_json_report_includes_former_employers():
    """Pipelining --json should surface the former_employers list — the
    scorer uses employer_former_match to cap former-employer addresses
    at low confidence; downstream consumers should be able to see the
    actual list and not just the boolean."""
    import json as _json
    from lib.schema import Employer
    p = Person(
        name="Jane",
        ambiguity="insufficient_identity_evidence",
        former_employers=[
            Employer(name="PSPDFKit", domains=["pspdfkit.com"], until="2023"),
        ],
    )
    raw = snoop._format_json_report(p, [])
    parsed = _json.loads(raw)
    fe = parsed["person"]["former_employers"]
    assert len(fe) == 1
    assert fe[0]["name"] == "PSPDFKit"
    assert fe[0]["domains"] == ["pspdfkit.com"]
    assert fe[0]["until"] == "2023"


def test_format_json_report_includes_account_and_former_employer_fields():
    """The JSON report must surface account_exists, account_display_name,
    and employer_former_match — pipelining `snoop --json` is the supported
    integration boundary, and dropping fields silently breaks downstream
    consumers."""
    import json as _json
    p = Person(name="Jane", ambiguity="insufficient_identity_evidence")
    c = EmailCandidate(
        address="jane@former.com",
        account_exists="verified",
        account_display_name="Jane Doe",
        employer_former_match=True,
        sources=[src("manual_known")],
    )
    raw = snoop._format_json_report(p, [c])
    parsed = _json.loads(raw)
    cand = parsed["candidates"][0]
    assert cand["account_exists"] == "verified"
    assert cand["account_display_name"] == "Jane Doe"
    assert cand["employer_former_match"] is True


# ---- run_pipeline timeout ---------------------------------------------------


# ---- google_account integration ---------------------------------------------


def test_google_account_candidates_filters_to_google_domains():
    cands = [
        EmailCandidate(address="x@google.com"),
        EmailCandidate(address="x@example.com"),  # not Google
        EmailCandidate(address="x@acme.com"),     # workspace domain
    ]
    out = snoop._google_account_candidates(cands, ["acme.com"])
    addresses = {c.address for c in out}
    assert addresses == {"x@google.com", "x@acme.com"}


def test_google_account_candidates_skips_already_probed():
    """Candidate already has a verdict set → don't re-probe."""
    cand1 = EmailCandidate(address="a@google.com", account_exists="verified")
    cand2 = EmailCandidate(address="b@google.com")  # default unprobed
    out = snoop._google_account_candidates([cand1, cand2], [])
    assert [c.address for c in out] == ["b@google.com"]


def test_google_account_candidates_sorts_by_belongs_desc():
    """Observation-backed candidates probe BEFORE pattern guesses so a
    verified-but-wrong-person pattern hit can't pre-empt a higher-confidence
    candidate that hasn't been tried yet."""
    high = EmailCandidate(address="z@google.com", belongs_to_person=0.75)
    low = EmailCandidate(address="a@google.com", belongs_to_person=0.20)
    unscored = EmailCandidate(address="m@google.com")  # belongs=None
    out = snoop._google_account_candidates([low, unscored, high], [])
    assert [c.address for c in out] == ["z@google.com", "a@google.com", "m@google.com"]


def test_google_target_domains_always_includes_literal_google_com():
    assert "google.com" in snoop._google_target_domains([])
    assert snoop._google_target_domains(["acme.com"]) == {"google.com", "acme.com"}


def test_google_target_domains_lowercases_and_strips():
    assert snoop._google_target_domains(["  ACME.COM  ", "Other.Org"]) == {
        "google.com", "acme.com", "other.org",
    }


def test_main_invokes_google_account_when_flag_set(monkeypatch, capsys):
    """With --allow-google-account on and a Google-domain candidate, the
    google_account resolver is invoked and feeds account_exists into the
    final score."""
    monkeypatch.setattr(snoop, "resolve_person", lambda name, **kw: Person(
        name=name,
        employer=Employer(name="Google", domains=["google.com"]),
        ambiguity="single_plausible_match",
        bound_anchors=[("github_name_match", name)],
    ))
    pattern_result = ResolverResult(
        resolver="pattern_gen",
        candidates=[
            EmailCandidate(address="real@google.com",
                           sources=[src("pattern")], employer_match=True),
            EmailCandidate(address="phantom@google.com",
                           sources=[src("pattern")], employer_match=True),
        ],
        status="ok",
    )
    empty = ResolverResult(resolver="x", candidates=[], status="empty")
    monkeypatch.setattr(snoop, "fetch_git_emails", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_gh_profile", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_personal_site", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_pattern_candidates", lambda *a, **kw: pattern_result)
    monkeypatch.setattr(snoop, "verify_candidates", lambda cands, **kw: cands)

    # Capture google_account invocation and apply canned verdicts
    google_calls = []
    def fake_google(candidates, **kw):
        google_calls.append(list(candidates))
        for c in candidates:
            if c.address == "real@google.com":
                c.account_exists = "verified"
                c.account_display_name = "Real Person"
            else:
                c.account_exists = "not_found"
        return ResolverResult(
            resolver="google_account", candidates=list(candidates), status="ok",
        )
    monkeypatch.setattr(snoop, "fetch_google_account", fake_google)

    rc = snoop.main([
        "Real Person",
        "--allow-google-account",
        "--no-smtp",
    ])
    assert rc == 0
    # google_account was invoked
    assert len(google_calls) == 1
    out = capsys.readouterr().out
    # The phantom should be capped low (rendered with very low belongs)
    # and the real one should be the recommendation
    assert "real@google.com" in out
    # The lead address line is one of the first ~3 lines and contains the pick.
    lead = "\n".join(out.split("\n")[:3])
    assert "real@google.com" in lead


def test_main_skips_google_account_when_flag_not_set(monkeypatch, capsys):
    """Without --allow-google-account, google_account is NEVER invoked,
    even if Google-domain candidates exist."""
    monkeypatch.setattr(snoop, "resolve_person", lambda name, **kw: Person(
        name=name,
        employer=Employer(name="Google", domains=["google.com"]),
        ambiguity="single_plausible_match",
    ))
    pattern_result = ResolverResult(
        resolver="pattern_gen",
        candidates=[EmailCandidate(address="x@google.com",
                                    sources=[src("pattern")],
                                    employer_match=True)],
        status="ok",
    )
    empty = ResolverResult(resolver="x", candidates=[], status="empty")
    monkeypatch.setattr(snoop, "fetch_git_emails", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_gh_profile", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_personal_site", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_pattern_candidates", lambda *a, **kw: pattern_result)
    monkeypatch.setattr(snoop, "verify_candidates", lambda cands, **kw: cands)

    google_called = []
    def fake_google(*a, **kw):
        google_called.append(True)
        return ResolverResult(resolver="google_account", candidates=[], status="ok")
    monkeypatch.setattr(snoop, "fetch_google_account", fake_google)

    rc = snoop.main(["X", "--no-smtp"])  # no --allow-google-account
    assert rc == 0
    assert google_called == []


def test_main_workspace_domain_flag_broadens_targeting(monkeypatch, capsys):
    """--google-workspace-domain acme.com tells the pipeline to probe
    candidates on acme.com via the Google API too."""
    monkeypatch.setattr(snoop, "resolve_person", lambda name, **kw: Person(
        name=name,
        employer=Employer(name="Acme", domains=["acme.com"]),
        ambiguity="single_plausible_match",
    ))
    pattern_result = ResolverResult(
        resolver="pattern_gen",
        candidates=[EmailCandidate(address="x@acme.com",
                                    sources=[src("pattern")],
                                    employer_match=True)],
        status="ok",
    )
    empty = ResolverResult(resolver="x", candidates=[], status="empty")
    monkeypatch.setattr(snoop, "fetch_git_emails", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_gh_profile", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_personal_site", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_pattern_candidates", lambda *a, **kw: pattern_result)
    monkeypatch.setattr(snoop, "verify_candidates", lambda cands, **kw: cands)

    captured_targets = []
    def fake_google(candidates, *, target_domains=None, **kw):
        captured_targets.append(set(target_domains or []))
        return ResolverResult(
            resolver="google_account", candidates=list(candidates), status="ok",
        )
    monkeypatch.setattr(snoop, "fetch_google_account", fake_google)

    snoop.main([
        "X", "--allow-google-account", "--no-smtp",
        "--google-workspace-domain", "acme.com",
    ])
    assert captured_targets == [{"google.com", "acme.com"}]


def test_pipeline_marks_timed_out_resolvers(monkeypatch):
    """If a resolver takes longer than the per-resolver timeout, mark it
    as timeout and keep the pipeline running."""
    import time

    def slow_resolver(*args, **kwargs):
        time.sleep(10)
        return ResolverResult(resolver="git_emails", candidates=[], status="ok")

    def fast_resolver(*args, **kwargs):
        return ResolverResult(
            resolver="pattern_gen",
            candidates=[EmailCandidate(
                address="x@y.com", sources=[src("pattern")]
            )],
            status="ok",
        )

    monkeypatch.setattr(snoop, "fetch_git_emails", slow_resolver)
    monkeypatch.setattr(snoop, "fetch_gh_profile", slow_resolver)
    monkeypatch.setattr(snoop, "fetch_personal_site", slow_resolver)
    monkeypatch.setattr(snoop, "fetch_pattern_candidates", fast_resolver)

    person = Person(
        name="X",
        handles={"github": "x"},
        personal_domains=["x.com"],
        ambiguity="single_plausible_match",
        bound_anchors=[
            ("github_name_match", "X"),
            ("github_employer_match", "y"),
        ],
    )
    # Use a very tight timeout so the test doesn't actually wait 10s
    results = snoop.run_pipeline(person, per_resolver_timeout_sec=0.5)
    statuses = {r.resolver: r.status for r in results}
    # The slow ones timed out; the fast one returned ok
    assert "pattern_gen" in statuses
    assert statuses["pattern_gen"] == "ok"
    # At least one of the slow resolvers timed out
    timed_out = [s for s in statuses.values() if s == "timeout"]
    assert timed_out, f"expected at least one timeout, got statuses: {statuses}"
