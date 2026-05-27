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

    # SMTP no-op
    monkeypatch.setattr(snoop, "verify_candidates", lambda cands, **kw: cands)

    rc = snoop.main([
        "Peter Steinberger",
        "--person-plan", '{"handles":{"github":"steipete"}}',
        "--no-smtp",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # Header
    assert "# Peter Steinberger" in out
    assert "OpenAI" in out
    # Both candidates surfaced in the work section
    assert "pete@openai.com" in out
    assert "peter.steinberger@openai.com" in out
    # Decision line names the high-belief one
    assert "pete@openai.com" in out.split("##")[1]  # decision section


def test_main_requires_name_or_plan():
    """argparse should reject invocation with neither positional name nor
    --person-plan."""
    import pytest
    with pytest.raises(SystemExit):
        snoop.main([])


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


# ---- run_pipeline timeout ---------------------------------------------------


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
