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
        EmailCandidate(address="pete@gmail.com", sources=[src("git_commit")]),
        EmailCandidate(address="pete@openai.com", sources=[src("git_commit")]),
    ]
    targets = snoop._smtp_candidates(cands)
    addresses = [c.address for c in targets]
    assert "pete@gmail.com" not in addresses
    assert "pete@openai.com" in addresses


def test_smtp_candidates_drops_sourceless():
    """A candidate with no sources isn't worth probing."""
    cands = [
        EmailCandidate(address="empty@openai.com"),  # no sources
        EmailCandidate(address="real@openai.com", sources=[src("pattern")]),
    ]
    targets = snoop._smtp_candidates(cands)
    addresses = [c.address for c in targets]
    assert "empty@openai.com" not in addresses


def test_smtp_candidates_orders_observed_before_pattern():
    """_probe_rank: an address actually observed (non-pattern source) probes
    before a pure name×domain guess, and more sources rank higher."""
    pattern_only = EmailCandidate(address="guess@openai.com",
                                  sources=[src("pattern")])
    observed = EmailCandidate(address="seen@openai.com",
                              sources=[src("git_commit")])
    corroborated = EmailCandidate(
        address="strong@openai.com",
        sources=[src("git_commit"), src("gh_profile")])
    targets = snoop._smtp_candidates([pattern_only, observed, corroborated])
    assert [c.address for c in targets] == [
        "strong@openai.com", "seen@openai.com", "guess@openai.com",
    ]


def test_smtp_candidates_respects_top_k():
    cands = [
        EmailCandidate(address=f"x{i}@openai.com", sources=[src("pattern")])
        for i in range(10)
    ]
    targets = snoop._smtp_candidates(cands, top_k=3)
    assert len(targets) == 3


def test_smtp_candidates_handles_malformed_address():
    cands = [
        EmailCandidate(address="not-an-email", sources=[src("git_commit")]),
        EmailCandidate(address="real@openai.com", sources=[src("git_commit")]),
    ]
    targets = snoop._smtp_candidates(cands)
    assert [c.address for c in targets] == ["real@openai.com"]


# ---- ENG-8 Phase-1: _candidate_is_bound (identity binds before probing) -----


def _gh_bound_person(name="Peter Steinberger", handle="steipete", **kw):
    return Person(name=name, handles={"github": handle},
                  ambiguity="single_plausible_match",
                  bound_anchors=[("github_name_match", name),
                                 ("github_handle_exists", handle)], **kw)


def test_pattern_only_on_employer_domain_does_not_bind():
    """A pure name×domain guess on the employer domain scores ONE signal
    (employer_match) and must NOT bind — this is the namesake-mailbox we refuse
    to probe."""
    person = _gh_bound_person(employer=Employer(name="OpenAI", domains=["openai.com"]))
    c = EmailCandidate(address="guess@openai.com", sources=[src("pattern")],
                       employer_match=True)
    assert snoop._candidate_is_bound(c, person) is False


def test_pattern_only_on_rel_me_domain_does_not_bind():
    """A guess on a rel=me-verified personal domain has only the ownership signal
    (the domain is the target's) — not that THIS localpart is real. One signal."""
    person = Person(name="Jane", ambiguity="single_plausible_match",
                    personal_domains=["jane.dev"],
                    bound_anchors=[("personal_domain_verified", "jane.dev")])
    c = EmailCandidate(address="guess@jane.dev", sources=[src("pattern")])
    assert snoop._candidate_is_bound(c, person) is False


def test_git_commit_on_bound_handle_plus_employer_binds():
    """Observed in the bound GitHub account's commits (anchored observation) AND
    on the employer domain — two independent signals → bound."""
    person = _gh_bound_person(employer=Employer(name="OpenAI", domains=["openai.com"]))
    c = EmailCandidate(address="pete@openai.com", sources=[src("git_commit")],
                       employer_match=True)
    assert snoop._candidate_is_bound(c, person) is True


def test_github_surface_does_not_anchor_when_handle_unbound():
    """A gh_profile reading is an anchored observation only when the handle bound.
    With a bare handle_exists hint, the surface does NOT count — so employer_match
    alone is one signal and the candidate stays unbound."""
    person = Person(name="X", handles={"github": "untrusted"},
                    ambiguity="single_plausible_match",
                    employer=Employer(name="OpenAI", domains=["openai.com"]),
                    bound_anchors=[("github_handle_exists", "untrusted")])
    c = EmailCandidate(address="x@openai.com", sources=[src("gh_profile")],
                       employer_match=True)
    assert snoop._candidate_is_bound(c, person) is False


def test_personal_site_on_verified_domain_binds():
    """An address published on a personal_site whose domain is rel=me-verified:
    anchored observation (real reading on a bound surface) + rel=me ownership =
    two signals → bound."""
    person = Person(name="Jane", ambiguity="single_plausible_match",
                    personal_domains=["jane.dev"],
                    bound_anchors=[("personal_domain_verified", "jane.dev")])
    c = EmailCandidate(address="jane@jane.dev", sources=[src("personal_site")])
    assert snoop._candidate_is_bound(c, person) is True


def test_pgp_alone_does_not_bind_but_pgp_plus_employer_does():
    """ENG-6: PGP is email-control evidence (deliverability axis), never
    identity-binding by itself — one signal. PGP + employer_match → two → bound."""
    person = _gh_bound_person(employer=Employer(name="OpenAI", domains=["openai.com"]))
    pgp_only = EmailCandidate(address="pete@proton.me", sources=[src("pgp")])
    assert snoop._candidate_is_bound(pgp_only, person) is False
    pgp_employer = EmailCandidate(address="pete@openai.com",
                                  sources=[src("pgp")], employer_match=True)
    assert snoop._candidate_is_bound(pgp_employer, person) is True


def test_manual_known_short_circuits_to_bound():
    """The --verify / --known subject is supplied by the user; it is always
    probeable regardless of discovered signals."""
    person = Person(name="", ambiguity="insufficient_identity_evidence")
    c = EmailCandidate(address="typed@acme.com", sources=[src("manual_known")])
    assert snoop._candidate_is_bound(c, person) is True


def test_git_commit_gmail_on_bound_handle_is_one_signal():
    """A personal gmail in the bound account's commits is an anchored observation
    (one signal) but nothing else corroborates it — not bound, so no Google
    existence probe fires on it. Surfaced as a candidate, just not probed."""
    person = _gh_bound_person()
    c = EmailCandidate(address="pete@gmail.com", sources=[src("git_commit")],
                       is_personal_provider=True)
    assert snoop._candidate_is_bound(c, person) is False


# ---- ENG-8 Phase-2: the probe gate (no socket to a namesake's mailbox) ------


def _probe_args(**kw):
    import argparse
    base = dict(allow_google_account=True, no_smtp=False,
                google_workspace_domain=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_probe_candidates_no_op_when_zero_bound_and_not_workspace(monkeypatch):
    """Zero bound candidates AND nothing on a Google-hosted domain → Phase 2 is a
    no-op: neither the Google existence probe nor SMTP is invoked. SMTP never sees
    an unbound guess (ENG-8); the Google check only fires on Workspace domains, and
    here the autodetect returns none."""
    person = Person(name="X", ambiguity="single_plausible_match",
                    employer=Employer(name="Acme", domains=["acme.com"]))
    candidates = [
        EmailCandidate(address="guess@acme.com", sources=[src("pattern")],
                       employer_match=True),               # 1 signal → unbound
        EmailCandidate(address="other@gmail.com", sources=[src("pattern")]),
    ]
    # Stub MX autodetect so no real DNS runs and acme.com is NOT a Workspace tenant.
    monkeypatch.setattr(snoop, "_autodetect_workspace_domains",
                        lambda cands, explicit: [])
    smtp_calls, google_calls = [], []
    monkeypatch.setattr(snoop, "verify_candidates",
                        lambda cands, **kw: smtp_calls.append(list(cands)))
    monkeypatch.setattr(snoop, "fetch_google_account",
                        lambda cands, **kw: google_calls.append(list(cands)))
    snoop._probe_candidates(person, candidates, _probe_args(), google_ready=True)
    assert smtp_calls == [] and google_calls == []


def test_probe_candidates_google_probes_unbound_on_workspace(monkeypatch):
    """ENG-9: zero bound candidates, but an unbound pattern guess sits on a
    Google-hosted employer domain → the Google existence check DOES run on it (the
    authed disambiguator), while SMTP stays silent (no socket to an unbound mailbox).
    This is the Workspace-no-footprint gap the split gate closes."""
    person = Person(name="Jibben Hillen", ambiguity="single_plausible_match",
                    employer=Employer(name="Stripe", domains=["stripe.com"]))
    candidates = [
        EmailCandidate(address="jibben@stripe.com", sources=[src("pattern", detail="generic template 'first'")],
                       employer_match=True),               # 1 signal → unbound
        EmailCandidate(address="jhillen@stripe.com", sources=[src("pattern", detail="generic template 'flast'")],
                       employer_match=True),
    ]
    monkeypatch.setattr(snoop, "_autodetect_workspace_domains",
                        lambda cands, explicit: ["stripe.com"])
    smtp_calls, google_calls = [], []
    monkeypatch.setattr(snoop, "verify_candidates",
                        lambda cands, **kw: smtp_calls.extend(c.address for c in cands))
    monkeypatch.setattr(snoop, "fetch_google_account",
                        lambda cands, **kw: google_calls.extend(c.address for c in cands)
                        or ResolverResult(resolver="google_account", candidates=[], status="ok"))
    snoop._probe_candidates(person, candidates, _probe_args(), google_ready=True)
    assert set(google_calls) == {"jibben@stripe.com", "jhillen@stripe.com"}
    assert smtp_calls == []                                # SMTP never touched the unbound guesses


def test_probe_candidates_smtp_stays_bound_only_with_speculative(monkeypatch):
    """ENG-9: when bound and speculative candidates coexist, the Google check covers
    BOTH but SMTP covers only the bound one — the split gate keeps the socket away
    from the unbound Workspace guess."""
    person = Person(name="Jane", ambiguity="single_plausible_match",
                    personal_domains=["jane.dev"],
                    bound_anchors=[("personal_domain_verified", "jane.dev")],
                    employer=Employer(name="Acme", domains=["acme.com"]))
    bound = EmailCandidate(address="jane@jane.dev", sources=[src("personal_site")])
    spec = EmailCandidate(address="jane@acme.com", sources=[src("pattern")],
                          employer_match=True)             # 1 signal → unbound
    monkeypatch.setattr(snoop, "_autodetect_workspace_domains",
                        lambda cands, explicit: ["acme.com"])
    smtp_calls, google_calls = [], []
    monkeypatch.setattr(snoop, "verify_candidates",
                        lambda cands, **kw: smtp_calls.extend(c.address for c in cands))
    monkeypatch.setattr(snoop, "fetch_google_account",
                        lambda cands, **kw: google_calls.extend(c.address for c in cands)
                        or ResolverResult(resolver="google_account", candidates=[], status="ok"))
    snoop._probe_candidates(person, [bound, spec], _probe_args(), google_ready=True)
    assert "jane@acme.com" in google_calls               # speculative reached Google
    assert smtp_calls == ["jane@jane.dev"]               # but only the bound one reached SMTP


def test_probe_candidates_skips_speculative_without_google_flag(monkeypatch):
    """The speculative Google path requires --allow-google-account AND a ready
    session. Without the flag, an unbound Workspace guess is neither Google- nor
    SMTP-probed (back to the ENG-8 honest blank)."""
    person = Person(name="Jibben Hillen", ambiguity="single_plausible_match",
                    employer=Employer(name="Stripe", domains=["stripe.com"]))
    cand = EmailCandidate(address="jibben@stripe.com", sources=[src("pattern")],
                          employer_match=True)
    autodetect_calls = []
    monkeypatch.setattr(snoop, "_autodetect_workspace_domains",
                        lambda cands, explicit: autodetect_calls.append(1) or ["stripe.com"])
    smtp_calls, google_calls = [], []
    monkeypatch.setattr(snoop, "verify_candidates",
                        lambda cands, **kw: smtp_calls.append(1))
    monkeypatch.setattr(snoop, "fetch_google_account",
                        lambda cands, **kw: google_calls.append(1))
    rec = snoop._probe_candidates(person, [cand],
                                  _probe_args(allow_google_account=False), google_ready=False)
    assert rec is None
    assert smtp_calls == [] and google_calls == [] and autodetect_calls == []


def test_probe_candidates_probes_only_the_bound_subset(monkeypatch):
    """A mix of bound and unbound candidates: only the bound one reaches the SMTP
    probe; the unbound guess is never handed to a socket."""
    person = Person(name="Jane", ambiguity="single_plausible_match",
                    personal_domains=["jane.dev"],
                    bound_anchors=[("personal_domain_verified", "jane.dev")])
    bound = EmailCandidate(address="jane@jane.dev", sources=[src("personal_site")])
    unbound = EmailCandidate(address="guess@other.com", sources=[src("pattern")])
    probed = []
    monkeypatch.setattr(snoop, "verify_candidates",
                        lambda cands, **kw: probed.extend(c.address for c in cands))
    snoop._probe_candidates(person, [bound, unbound],
                            _probe_args(allow_google_account=False), google_ready=False)
    assert probed == ["jane@jane.dev"]


def _verified_domain_person():
    return Person(name="Jane", ambiguity="single_plausible_match",
                  personal_domains=["jane.dev"],
                  bound_anchors=[("personal_domain_verified", "jane.dev")])


def test_phase2_abandons_hung_probe_at_deadline(monkeypatch):
    """ENG-8/ENG-5: the shared wall-clock wraps Phase 2. A hung SMTP probe is
    abandoned at the deadline; _probe_candidates reports a deadline-exceeded
    degradation, and the straggler's verdict is NEVER merged (it mutated only the
    staged copies) — the real candidate stays unprobed."""
    import time as _time
    monkeypatch.setattr(snoop, "_PER_SENSOR_FLOOR_SEC", 0.1)
    cand = EmailCandidate(address="jane@jane.dev", sources=[src("personal_site")])

    def hang(cands, **kw):
        _time.sleep(2)
        for c in cands:
            c.smtp_verdict = "verified"   # a late straggler mutating its COPY

    monkeypatch.setattr(snoop, "verify_candidates", hang)
    rec = snoop._probe_candidates(
        _verified_domain_person(), [cand],
        _probe_args(allow_google_account=False), google_ready=False, deadline_sec=0.1)
    assert rec is not None and rec.status == "degraded"
    assert "deadline-exceeded" in (rec.reason or "")
    assert cand.smtp_verdict == "unprobed"   # straggler's verdict not merged


def test_phase2_merges_verdicts_when_completed_in_time(monkeypatch):
    """When Phase 2 finishes before the deadline, the staged probe verdicts are
    merged back onto the real candidates."""
    cand = EmailCandidate(address="jane@jane.dev", sources=[src("personal_site")])

    def quick(cands, **kw):
        for c in cands:
            c.smtp_verdict = "verified"
            c.mx_provider = "google"

    monkeypatch.setattr(snoop, "verify_candidates", quick)
    rec = snoop._probe_candidates(
        _verified_domain_person(), [cand],
        _probe_args(allow_google_account=False), google_ready=False, deadline_sec=5)
    assert rec is None
    assert cand.smtp_verdict == "verified"
    assert cand.mx_provider == "google"


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
    """Smoke test the full pipeline end-to-end: parse args, build plan, resolve,
    fan out, cluster, order, emit the observation bundle. All resolvers
    monkeypatched to canned results so we don't hit the network."""
    import json as _json

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

    def mock_empty(resolver):
        def _fn(*args, **kwargs):
            return ResolverResult(resolver=resolver, candidates=[], status="empty")
        return _fn

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
    monkeypatch.setattr(snoop, "fetch_gh_profile", mock_empty("gh_profile"))
    monkeypatch.setattr(snoop, "fetch_personal_site", mock_empty("personal_site"))
    monkeypatch.setattr(snoop, "fetch_pattern_candidates", mock_pattern)
    monkeypatch.setattr(snoop, "fetch_recent_repos", lambda *a, **kw: [])
    monkeypatch.setattr(snoop, "verify_candidates", lambda cands, **kw: cands)

    rc = snoop.main([
        "Peter Steinberger",
        "--person-plan", '{"handles":{"github":"steipete"}}',
        "--no-smtp",
    ])
    assert rc == 0
    bundle = _json.loads(capsys.readouterr().out)
    assert bundle["person"]["name"] == "Peter Steinberger"
    assert bundle["person"]["ambiguity"] == "single_plausible_match"
    blob = "\n".join(o["content"] for o in bundle["observations"])
    # both the observed git-commit address and the pattern guess are in the bundle
    assert "pete@openai.com" in blob
    assert "peter.steinberger@openai.com" in blob
    # the observed address is ordered ahead of the pure pattern guess
    email_obs = [o for o in bundle["observations"] if o["type"] == "email_candidate"]
    assert email_obs[0]["content"].index("pete@openai.com") >= 0
    assert "pete@openai.com" in email_obs[0]["content"]


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


def test_capability_warnings_name_the_anon_github_60_req_hr_ceiling():
    """The anonymous-GitHub fallback is a named degradation case: both the
    gh-missing and gh-unauthed warnings must name the 60 req/hr ceiling so the
    user knows exactly what degraded."""
    from lib.diagnose import Capability
    for status in ("missing", "degraded"):
        caps = [Capability(name="gh_cli", status=status, detail="x", impact="y")]
        warnings = snoop._capability_warnings(caps, allow_google_account=False)
        assert any("60 req/hr" in w for w in warnings), status


def test_dnspython_missing_emits_typed_smtp_skip(monkeypatch, capsys):
    """First-run degradation: with dnspython missing and SMTP not disabled by the
    user, the bundle's sensors array carries an smtp skip whose reason names the
    missing dependency — not a silent unprobed."""
    import json as _json
    from lib.diagnose import Capability
    monkeypatch.setattr(snoop, "_fast_capability_probe", lambda **kw: [
        Capability(name="dnspython", status="missing",
                   detail="not installed", impact="no SMTP"),
    ])
    _resolver_setup(monkeypatch)  # resolves Alice Smith, no personal domains
    rc = snoop.main(["Alice Smith", "--no-pgp", "--observations"])  # NOT --no-smtp
    assert rc == 0
    bundle = _json.loads(capsys.readouterr().out)
    smtp = next(s for s in bundle["sensors"] if s["sensor"] == "smtp")
    assert smtp["status"] == "skipped"
    assert smtp["reason"] == "dependency dnspython missing"


def test_diagnose_reports_ledger_health_line(monkeypatch, capsys, tmp_path):
    """1A integration: --diagnose prints the ledger health line so a user can see
    where the local ledger lives and whether it's writable."""
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = snoop.main(["--diagnose"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ledger:" in out
    assert "writable=" in out


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


def test_main_empty_pipeline_emits_identity_bundle(monkeypatch, capsys):
    """With no candidates, the bundle still describes the resolved identity and
    its ambiguity — the host reasons over that too."""
    import json as _json

    monkeypatch.setattr(snoop, "resolve_person", lambda name, **kw: Person(
        name=name,
        ambiguity="insufficient_identity_evidence",
    ))
    empty_result = ResolverResult(resolver="x", candidates=[], status="empty")
    monkeypatch.setattr(snoop, "fetch_git_emails", lambda *a, **kw: empty_result)
    monkeypatch.setattr(snoop, "fetch_gh_profile", lambda *a, **kw: empty_result)
    monkeypatch.setattr(snoop, "fetch_personal_site", lambda *a, **kw: empty_result)
    monkeypatch.setattr(snoop, "fetch_pattern_candidates", lambda *a, **kw: empty_result)

    rc = snoop.main(["X", "--no-smtp"])
    assert rc == 0
    bundle = _json.loads(capsys.readouterr().out)
    assert bundle["person"]["name"] == "X"
    assert bundle["person"]["ambiguity"] == "insufficient_identity_evidence"
    assert isinstance(bundle["observations"], list)
    # no email candidates, but the bundle is still well-formed
    assert not any(o["type"] == "email_candidate" for o in bundle["observations"])


# ---- run_pipeline timeout ---------------------------------------------------


# ---- run_pipeline: hn_profile + package_registry wiring ---------------------


def _hn_result(handle):
    return ResolverResult(
        resolver="hn_profile",
        candidates=[EmailCandidate(
            address="pg@ycombinator.com",
            sources=[src("hn_profile",
                         url=f"https://news.ycombinator.com/user?id={handle}")])],
        status="ok",
    )


def _pkg_result(packages):
    return ResolverResult(
        resolver="package_registry",
        candidates=[EmailCandidate(
            address="dev@pkg.io",
            sources=[src("package_registry", url="https://pypi.org/project/foo/")])],
        status="ok",
    )


def test_run_pipeline_fires_hn_profile_when_hn_handle_present(monkeypatch):
    monkeypatch.setattr(snoop, "fetch_hn_profile", _hn_result)
    monkeypatch.setattr(snoop, "fetch_pattern_candidates",
                        lambda p, **kw: ResolverResult(resolver="pattern_gen",
                                                       candidates=[], status="empty"))
    person = Person(name="PG", handles={"hn": "pg"},
                    ambiguity="single_plausible_match")
    results = snoop.run_pipeline(person)
    names = {r.resolver for r in results}
    assert "hn_profile" in names
    assert any(c.address == "pg@ycombinator.com"
               for r in results for c in r.candidates)


def test_run_pipeline_skips_hn_profile_without_hn_handle(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("hn_profile must not run without an hn handle")
    monkeypatch.setattr(snoop, "fetch_hn_profile", boom)
    monkeypatch.setattr(snoop, "fetch_pattern_candidates",
                        lambda p, **kw: ResolverResult(resolver="pattern_gen",
                                                       candidates=[], status="empty"))
    person = Person(name="X", ambiguity="insufficient_identity_evidence")
    results = snoop.run_pipeline(person)
    assert "hn_profile" not in {r.resolver for r in results}


def test_run_pipeline_fires_package_registry_when_packages_supplied(monkeypatch):
    captured = {}
    def fake_pkg(packages):
        captured["packages"] = packages
        return _pkg_result(packages)
    monkeypatch.setattr(snoop, "fetch_package_emails", fake_pkg)
    monkeypatch.setattr(snoop, "fetch_pattern_candidates",
                        lambda p, **kw: ResolverResult(resolver="pattern_gen",
                                                       candidates=[], status="empty"))
    person = Person(name="X", ambiguity="single_plausible_match")
    pkgs = [{"registry": "pypi", "name": "foo"}]
    results = snoop.run_pipeline(person, packages=pkgs)
    assert "package_registry" in {r.resolver for r in results}
    assert captured["packages"] == pkgs


def test_run_pipeline_skips_package_registry_without_packages(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("package_registry must not run without packages")
    monkeypatch.setattr(snoop, "fetch_package_emails", boom)
    monkeypatch.setattr(snoop, "fetch_pattern_candidates",
                        lambda p, **kw: ResolverResult(resolver="pattern_gen",
                                                       candidates=[], status="empty"))
    person = Person(name="X", ambiguity="single_plausible_match")
    results = snoop.run_pipeline(person)
    assert "package_registry" not in {r.resolver for r in results}


def test_main_threads_plan_packages_into_pipeline(monkeypatch, capsys):
    """The host supplies plan['packages']; main threads them to package_registry,
    and the email surfaces in the bundle."""
    import json as _json
    monkeypatch.setattr(snoop, "resolve_person", lambda name, **kw: Person(
        name=name, ambiguity="single_plausible_match",
        bound_anchors=[("github_name_match", name)],
    ))
    empty = ResolverResult(resolver="x", candidates=[], status="empty")
    for fn in ("fetch_git_emails", "fetch_gh_profile",
               "fetch_personal_site", "fetch_pattern_candidates"):
        monkeypatch.setattr(snoop, fn, lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_package_emails", _pkg_result)
    rc = snoop.main(["Dev Person", "--no-smtp", "--person-plan",
                     '{"packages":[{"registry":"pypi","name":"foo"}]}'])
    assert rc == 0
    bundle = _json.loads(capsys.readouterr().out)
    blob = "\n".join(o["content"] for o in bundle["observations"])
    assert "dev@pkg.io" in blob
    assert "package_registry" in blob


# ---- _reassess_identity: Google name_match promotes identity ----------------


def test_reassess_promotes_on_unique_google_name_match():
    """A verified Google account whose display name matches the target is genuine
    identity binding — promote insufficient → single_plausible_match."""
    person = Person(name="Mihika Kapoor", ambiguity="insufficient_identity_evidence")
    cands = [
        EmailCandidate(address="mihika@simile.ai", account_exists="verified",
                       account_display_name="Mihika Kapoor"),
        EmailCandidate(address="kapoor@simile.ai", account_exists="not_found"),
    ]
    snoop._reassess_identity(person, cands)
    assert person.ambiguity == "single_plausible_match"
    assert any(a[0] == "google_name_match" for a in person.bound_anchors)


def test_reassess_does_not_promote_without_display_name():
    """A verified account with NO display name (locked-down tenant) is not enough
    to bind identity by itself — stays insufficient (the render layer keeps the
    verified email honest without overclaiming the person)."""
    person = Person(name="Mihika Kapoor", ambiguity="insufficient_identity_evidence")
    cands = [EmailCandidate(address="mihika@simile.ai", account_exists="verified")]
    snoop._reassess_identity(person, cands)
    assert person.ambiguity == "insufficient_identity_evidence"


def test_reassess_does_not_promote_on_name_mismatch():
    person = Person(name="Mihika Kapoor", ambiguity="insufficient_identity_evidence")
    cands = [EmailCandidate(address="mihika@simile.ai", account_exists="verified",
                            account_display_name="Mihika Sharma")]
    snoop._reassess_identity(person, cands)
    assert person.ambiguity == "insufficient_identity_evidence"


def test_reassess_does_not_touch_multiple_plausible_matches():
    """A declared namesake is never auto-promoted, even if one verified account
    name-matches — the operator flagged genuine ambiguity."""
    person = Person(name="John Smith", ambiguity="multiple_plausible_matches")
    cands = [EmailCandidate(address="john@acme.com", account_exists="verified",
                            account_display_name="John Smith")]
    snoop._reassess_identity(person, cands)
    assert person.ambiguity == "multiple_plausible_matches"


def test_reassess_does_not_promote_when_two_accounts_name_match():
    """Two verified accounts both matching the name is real ambiguity, not a
    confident bind — don't promote."""
    person = Person(name="Mihika Kapoor", ambiguity="insufficient_identity_evidence")
    cands = [
        EmailCandidate(address="mihika@simile.ai", account_exists="verified",
                       account_display_name="Mihika Kapoor"),
        EmailCandidate(address="mkapoor@simile.ai", account_exists="verified",
                       account_display_name="Mihika Kapoor"),
    ]
    snoop._reassess_identity(person, cands)
    assert person.ambiguity == "insufficient_identity_evidence"


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


def test_google_account_candidates_orders_observed_first():
    """Observation-backed candidates probe BEFORE pattern guesses so a
    verified-but-wrong-person pattern hit can't pre-empt an observed candidate
    that hasn't been tried yet (_probe_rank)."""
    observed = EmailCandidate(address="z@google.com", sources=[src("git_commit")])
    pattern = EmailCandidate(address="a@google.com", sources=[src("pattern")])
    sourceless = EmailCandidate(address="m@google.com")  # no sources → last
    out = snoop._google_account_candidates([pattern, sourceless, observed], [])
    assert [c.address for c in out] == ["z@google.com", "a@google.com", "m@google.com"]


def _noname_person():
    """A person with no parseable name → _primary_localparts returns None, so the
    speculative filter applies no local-part restriction. Isolates the domain/rank
    behavior under test from the primary-name-parse filter."""
    return Person(name="", ambiguity="insufficient_identity_evidence")


def test_speculative_google_candidates_picks_unbound_workspace_only():
    """ENG-9: the speculative set is unbound pattern guesses on a Google-hosted
    domain. Bound addresses (probed via the bound path) and non-Workspace domains
    are excluded."""
    cands = [
        EmailCandidate(address="jibben@stripe.com", sources=[src("pattern")]),
        EmailCandidate(address="bound@stripe.com", sources=[src("pattern")]),
        EmailCandidate(address="x@nonworkspace.com", sources=[src("pattern")]),
        EmailCandidate(address="x@google.com", sources=[src("pattern")]),
    ]
    out = snoop._speculative_google_candidates(
        cands, {"bound@stripe.com"}, ["stripe.com"], _noname_person())
    assert {c.address for c in out} == {"jibben@stripe.com", "x@google.com"}


def test_speculative_google_candidates_skips_already_probed():
    """A candidate that already has an account_exists verdict isn't re-probed."""
    cands = [
        EmailCandidate(address="a@stripe.com", sources=[src("pattern")],
                       account_exists="not_found"),
        EmailCandidate(address="b@stripe.com", sources=[src("pattern")]),
    ]
    out = snoop._speculative_google_candidates(
        cands, set(), ["stripe.com"], _noname_person())
    assert [c.address for c in out] == ["b@stripe.com"]


def test_speculative_google_excludes_reversed_name_order():
    """ENG-9 cost control: only the PRIMARY name parse's local-parts probe
    speculatively. For 'Jibben Hillen' that means jibben@/jhillen@/j.hillen@ but NOT
    the reversed-order hillen@/hjibben@ — those roughly double the burst and are
    almost always noise. The bound path still covers every variant."""
    person = Person(name="Jibben Hillen", ambiguity="single_plausible_match")
    cands = [
        EmailCandidate(address="jibben@stripe.com", sources=[src("pattern")]),    # first (primary)
        EmailCandidate(address="j.hillen@stripe.com", sources=[src("pattern")]),  # f.last (primary)
        EmailCandidate(address="hillen@stripe.com", sources=[src("pattern")]),    # first (reversed) → drop
        EmailCandidate(address="hjibben@stripe.com", sources=[src("pattern")]),   # flast (reversed) → drop
    ]
    out = {c.address for c in snoop._speculative_google_candidates(
        cands, set(), ["stripe.com"], person)}
    assert out == {"jibben@stripe.com", "j.hillen@stripe.com"}


def test_speculative_rank_keeps_first_template_within_cap():
    """The whole point of the split gate: a `first@` guess for a rare first name
    must survive the cap. Template-plausibility ordering (not alphabetical) keeps it
    ahead of the long tail even though `first` is a low-popularity corporate pattern."""
    # 25 junk guesses that would alphabetically bury 'jibben@' (j... mid-pack), plus
    # the real first@ candidate. No name filter here (_noname_person) so this isolates
    # the rank/cap behavior; template ranking lifts 'first' over the 'lastf' noise.
    noise = [EmailCandidate(address=f"zzz{i}@stripe.com",
                            sources=[src("pattern", detail="generic template 'lastf'")])
             for i in range(25)]
    first = EmailCandidate(address="jibben@stripe.com",
                           sources=[src("pattern", detail="generic template 'first'")])
    out = snoop._speculative_google_candidates(
        [*noise, first], set(), ["stripe.com"], _noname_person())
    assert len(out) == snoop._SPECULATIVE_GOOGLE_CAP
    assert "jibben@stripe.com" in {c.address for c in out}  # 'first' < 'lastf' in template order


def test_speculative_rank_company_inferred_winner_leads():
    """A candidate matching the company-inferred pattern outranks generic guesses."""
    inferred = EmailCandidate(address="j.hillen@stripe.com",
                              sources=[src("pattern", detail="matches company pattern 'first.last' corroborated by 2 known addresses")])
    generic = EmailCandidate(address="aaa@stripe.com",
                             sources=[src("pattern", detail="generic template 'first.last'")])
    out = snoop._speculative_google_candidates(
        [generic, inferred], set(), ["stripe.com"], _noname_person())
    assert out[0].address == "j.hillen@stripe.com"


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
        name=name, handles={"github": "real"},
        employer=Employer(name="Google", domains=["google.com"]),
        ambiguity="single_plausible_match",
        bound_anchors=[("github_name_match", name)],
    ))
    # ENG-8: both addresses are BOUND (observed in the bound github account's
    # commits + on the employer domain = two signals), so probing them is the
    # legitimate Matt-Smith split the Google probe disambiguates — not a guess.
    observed_result = ResolverResult(
        resolver="git_emails",
        candidates=[
            EmailCandidate(address="real@google.com",
                           sources=[src("git_commit")], employer_match=True),
            EmailCandidate(address="phantom@google.com",
                           sources=[src("git_commit")], employer_match=True),
        ],
        status="ok",
    )
    empty = ResolverResult(resolver="x", candidates=[], status="empty")
    monkeypatch.setattr(snoop, "fetch_git_emails", lambda *a, **kw: observed_result)
    monkeypatch.setattr(snoop, "fetch_gh_profile", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_personal_site", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_pattern_candidates", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_recent_repos", lambda *a, **kw: [])
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

    import json as _json
    rc = snoop.main([
        "Real Person",
        "--allow-google-account",
        "--no-smtp",
    ])
    assert rc == 0
    # google_account was invoked
    assert len(google_calls) == 1
    bundle = _json.loads(capsys.readouterr().out)
    obs = {o["content"] for o in bundle["observations"]}
    blob = "\n".join(obs)
    # the verified account surfaces account_exists=verified + a name_match verdict
    assert "real@google.com" in blob
    assert "account_exists=verified" in blob
    assert 'google_display_name="Real Person"' in blob
    assert "name_match=yes" in blob
    # the phantom's not_found verdict is also surfaced for the host to drop
    assert "phantom@google.com" in blob
    assert "account_exists=not_found" in blob


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
        name=name, handles={"github": "xh"},
        employer=Employer(name="Acme", domains=["acme.com"]),
        ambiguity="single_plausible_match",
        bound_anchors=[("github_name_match", name)],
    ))
    # ENG-8: a BOUND candidate on acme.com (observed in the bound account's commits
    # + employer_match) — the workspace flag still has to broaden TARGETING to
    # acme.com for it to be probed.
    observed_result = ResolverResult(
        resolver="git_emails",
        candidates=[EmailCandidate(address="x@acme.com",
                                    sources=[src("git_commit")],
                                    employer_match=True)],
        status="ok",
    )
    empty = ResolverResult(resolver="x", candidates=[], status="empty")
    monkeypatch.setattr(snoop, "fetch_git_emails", lambda *a, **kw: observed_result)
    monkeypatch.setattr(snoop, "fetch_gh_profile", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_personal_site", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_pattern_candidates", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_recent_repos", lambda *a, **kw: [])
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


def test_pipeline_abandons_hung_resolvers_at_deadline(monkeypatch):
    """The E6 chaos test: a hung resolver is abandoned at the shared deadline,
    reports deadline-exceeded (status=timeout), and the others' results survive —
    the run never blocks on the hung sensor."""
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
    # Tight shared deadline so the test doesn't actually wait 10s. The slow
    # daemon threads keep sleeping in the background; the run abandons them.
    t0 = time.monotonic()
    results = snoop.run_pipeline(person, deadline_sec=0.3, per_sensor_floor_sec=0.2)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"run blocked on the hung resolver ({elapsed:.1f}s)"
    statuses = {r.resolver: r.status for r in results}
    by_name = {r.resolver: r for r in results}
    # The fast one's result survives despite the hung siblings
    assert statuses["pattern_gen"] == "ok"
    # The hung ones are abandoned: status=timeout, reason=deadline-exceeded
    timed_out = [r for r in results if r.status == "timeout"]
    assert timed_out
    assert any("deadline-exceeded" in (r.error_detail or "") for r in timed_out)
    # every result carries elapsed_ms (timing stamped)
    assert by_name["pattern_gen"].elapsed_ms is not None


def test_pipeline_isolates_crashing_resolver(monkeypatch):
    """A resolver that raises is isolated to status=error; siblings survive."""
    def boom(*a, **kw):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(snoop, "fetch_git_emails", boom)
    monkeypatch.setattr(snoop, "fetch_gh_profile",
                        lambda *a, **kw: ResolverResult(resolver="gh_profile",
                                                        candidates=[], status="empty"))
    monkeypatch.setattr(snoop, "fetch_pattern_candidates",
                        lambda *a, **kw: ResolverResult(resolver="pattern_gen",
                                                        candidates=[], status="empty"))
    person = Person(name="X", handles={"github": "x"},
                    ambiguity="single_plausible_match",
                    bound_anchors=[("github_name_match", "X"),
                                   ("github_employer_match", "y")])
    results = snoop.run_pipeline(person, deadline_sec=2.0)
    by_name = {r.resolver: r for r in results}
    assert by_name["git_emails"].status == "error"
    assert "kaboom" in by_name["git_emails"].error_detail
    assert by_name["pattern_gen"].status == "empty"  # sibling survived


def test_bundle_carries_per_sensor_timing(monkeypatch, capsys):
    import json as _json
    _resolver_setup(monkeypatch)
    snoop.main(["Alice Smith", "--no-smtp", "--observations"])
    bundle = _json.loads(capsys.readouterr().out)
    assert "sensors" in bundle
    sensors = {s["sensor"]: s for s in bundle["sensors"]}
    assert sensors  # at least pattern_gen
    assert all("status" in s for s in bundle["sensors"])
    assert all(s["status"] in ("ran", "skipped", "degraded") for s in bundle["sensors"])


def test_bundle_reports_skipped_sensors_with_reasons(monkeypatch, capsys):
    """The typed degradation contract: gated-off sensors appear as skipped with a
    reason, so the bundle says 'didn't check Y because Z' rather than omitting."""
    import json as _json
    _resolver_setup(monkeypatch)  # no hn handle, no personal_domains, no packages
    snoop.main(["Alice Smith", "--no-smtp", "--observations"])
    bundle = _json.loads(capsys.readouterr().out)
    by = {s["sensor"]: s for s in bundle["sensors"]}
    assert by["personal_site"]["status"] == "skipped"
    assert "personal_domains" in by["personal_site"]["reason"]
    assert by["hn_profile"]["status"] == "skipped"
    assert by["smtp"]["status"] == "skipped" and by["smtp"]["reason"] == "--no-smtp"
    assert by["google_account"]["status"] == "skipped"


def test_run_summary_to_stderr(monkeypatch, capsys):
    _resolver_setup(monkeypatch)
    snoop.main(["Alice Smith", "--no-smtp", "--observations"])
    err = capsys.readouterr().err
    assert err.startswith("sensors:")
    assert "skipped" in err


def test_pgp_corroboration_merges_source_onto_candidate(monkeypatch, capsys):
    """E3: a keys.openpgp.org hit on a discovered address adds a `pgp` Source to
    that candidate (owner-verified corroboration)."""
    import json as _json
    _resolver_setup(monkeypatch)  # produces alice@corp.com
    monkeypatch.setattr(snoop, "fetch_pgp_emails",
                        lambda emails, **kw: ResolverResult(
                            resolver="pgp",
                            candidates=[EmailCandidate(
                                address="alice@corp.com",
                                sources=[src("pgp", url="https://keys.openpgp.org/x")])],
                            status="ok"))
    snoop.main(["Alice Smith", "--no-smtp", "--observations"])
    bundle = _json.loads(capsys.readouterr().out)
    email_obs = [o for o in bundle["observations"] if o["type"] == "email_candidate"]
    blob = "\n".join(o["content"] for o in email_obs)
    assert "pgp" in blob  # the pgp source surfaced on the candidate
    sensors = {s["sensor"]: s for s in bundle["sensors"]}
    assert sensors["pgp"]["status"] == "ran"


def test_no_pgp_flag_skips_corroboration(monkeypatch, capsys):
    import json as _json
    _resolver_setup(monkeypatch)
    def boom(emails, **kw):
        raise AssertionError("pgp must not run with --no-pgp")
    monkeypatch.setattr(snoop, "fetch_pgp_emails", boom)
    snoop.main(["Alice Smith", "--no-smtp", "--no-pgp", "--observations"])
    bundle = _json.loads(capsys.readouterr().out)
    sensors = {s["sensor"]: s for s in bundle["sensors"]}
    assert sensors["pgp"]["status"] == "skipped"


def test_rel_me_emits_observation_and_anchor(monkeypatch, capsys):
    """E2: a bidirectional rel=me link emits a rel_me observation and records a
    rel_me_verified anchor the host can weigh."""
    import json as _json
    from lib.rel_me import RelMeLink
    monkeypatch.setattr(snoop, "resolve_person", lambda name, **kw: Person(
        name=name, ambiguity="single_plausible_match",
        personal_domains=["alice.dev"],
        bound_anchors=[("github_name_match", name)],
    ))
    empty = ResolverResult(resolver="x", candidates=[], status="empty")
    for fn in ("fetch_git_emails", "fetch_gh_profile",
               "fetch_personal_site", "fetch_pattern_candidates"):
        monkeypatch.setattr(snoop, fn, lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_recent_repos", lambda *a, **kw: [])
    monkeypatch.setattr(snoop, "verify_rel_me", lambda domain, **kw: [
        RelMeLink(platform="github", url="https://github.com/alice",
                  bidirectional=True, tier="asserted"),
    ])
    snoop.main(["Alice", "--no-smtp", "--no-pgp", "--observations"])
    bundle = _json.loads(capsys.readouterr().out)
    rel = [o for o in bundle["observations"] if o["type"] == "rel_me"]
    assert rel and "github.com/alice" in rel[0]["content"]
    assert "bidirectional" in rel[0]["content"]
    # the verified link is recorded as an anchor (emitted as an anchor obs)
    anchors = "\n".join(o["content"] for o in bundle["observations"]
                        if o["type"] == "anchor")
    assert "rel_me_verified" in anchors
    sensors = {s["sensor"]: s for s in bundle["sensors"]}
    assert sensors["rel_me"]["status"] == "ran"


def test_rel_me_skipped_without_personal_domains(monkeypatch, capsys):
    import json as _json
    _resolver_setup(monkeypatch)  # no personal_domains
    snoop.main(["Alice Smith", "--no-smtp", "--no-pgp", "--observations"])
    bundle = _json.loads(capsys.readouterr().out)
    sensors = {s["sensor"]: s for s in bundle["sensors"]}
    assert sensors["rel_me"]["status"] == "skipped"


def test_run_appends_valid_target_free_ledger_record(monkeypatch, capsys):
    """E1: a real run appends a schema-clean ledger record carrying NO target
    data — only plan shape, mx class, and sensor timing."""
    from lib.ledger import validate_record
    captured = {}
    monkeypatch.setattr(snoop, "append_run",
                        lambda rec, **kw: captured.setdefault("rec", rec) or True)
    monkeypatch.setattr(snoop, "resolve_person", lambda name, **kw: Person(
        name=name, handles={"github": "secrethandle"},
        personal_domains=["secret.dev"],
        ambiguity="single_plausible_match",
        bound_anchors=[("github_name_match", name)],
    ))
    empty = ResolverResult(resolver="x", candidates=[], status="empty")
    for fn in ("fetch_git_emails", "fetch_gh_profile",
               "fetch_personal_site", "fetch_pattern_candidates"):
        monkeypatch.setattr(snoop, fn, lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_recent_repos", lambda *a, **kw: [])
    snoop.main(["Sekret Person", "--no-smtp", "--no-pgp", "--observations"])
    rec = captured["rec"]
    assert validate_record(rec) == []                       # schema-clean
    assert rec["plan_shape"]["github"] is True              # shape, not value
    blob = __import__("json").dumps(rec)
    assert "secrethandle" not in blob and "secret.dev" not in blob
    assert "Sekret Person" not in blob                      # no name


def test_no_ledger_flag_skips_write(monkeypatch, capsys):
    def boom(rec, **kw):
        raise AssertionError("ledger must not be written with --no-ledger")
    monkeypatch.setattr(snoop, "append_run", boom)
    _resolver_setup(monkeypatch)
    rc = snoop.main(["Alice Smith", "--no-smtp", "--no-pgp", "--no-ledger",
                     "--observations"])
    assert rc == 0  # no AssertionError → ledger was not written


# ---- --no-search escape hatch -----------------------------------------------


def _search_setup(monkeypatch):
    """Mock all resolvers empty so only the host-supplied work_search_results can
    produce observations."""
    monkeypatch.setattr(snoop, "resolve_person", lambda name, **kw: Person(
        name=name, ambiguity="single_plausible_match",
        bound_anchors=[("github_name_match", name)],
    ))
    empty = ResolverResult(resolver="x", candidates=[], status="empty")
    for fn in ("fetch_git_emails", "fetch_gh_profile",
               "fetch_personal_site", "fetch_pattern_candidates"):
        monkeypatch.setattr(snoop, fn, lambda *a, **kw: empty)


_PLAN_WITH_SEARCH = (
    '{"work_search_results":[{"title":"T","url":"https://x.example"}]}'
)


def test_no_search_flag_drops_work_search_observations(monkeypatch, capsys):
    """--no-search drops the host-supplied work_search_results from the bundle."""
    import json as _json
    _search_setup(monkeypatch)
    rc = snoop.main(["X", "--no-smtp", "--no-search",
                     "--person-plan", _PLAN_WITH_SEARCH])
    assert rc == 0
    bundle = _json.loads(capsys.readouterr().out)
    assert not any(o["type"] == "web_search" for o in bundle["observations"])


def test_work_search_observations_present_without_no_search(monkeypatch, capsys):
    """Without --no-search, host-model results ARE emitted as web_search
    observations (guards against the flag silently dropping them for everyone)."""
    import json as _json
    _search_setup(monkeypatch)
    rc = snoop.main(["X", "--no-smtp", "--person-plan", _PLAN_WITH_SEARCH])
    assert rc == 0
    bundle = _json.loads(capsys.readouterr().out)
    assert any(o["type"] == "web_search" for o in bundle["observations"])


# ---- shared resolver scaffolding (observations / ground tests) -------------


def _resolver_setup(monkeypatch):
    """Mock resolvers so one candidate exists, and stub out fetchers/SMTP."""
    monkeypatch.setattr(snoop, "resolve_person", lambda name, **kw: Person(
        name=name, ambiguity="single_plausible_match",
        bound_anchors=[("github_name_match", name)],
    ))
    empty = ResolverResult(resolver="x", candidates=[], status="empty")
    monkeypatch.setattr(snoop, "fetch_git_emails", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_gh_profile", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_personal_site", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_pattern_candidates", lambda *a, **kw: ResolverResult(
        resolver="pattern_gen",
        candidates=[EmailCandidate(address="alice@corp.com", sources=[src("pattern")],
                                   employer_match=True)],
        status="ok",
    ))
    monkeypatch.setattr(snoop, "verify_candidates", lambda cands, **kw: cands)


# ---- resolution_gaps: coach a thin plan -------------------------------------


def test_resolution_gaps_flags_thin_plan():
    """A name-only plan leaves the discovery sensors with nothing — coach all
    three high-value inputs."""
    person = Person(
        name="X", ambiguity="insufficient_identity_evidence",
        employer=Employer(name="Acme", domains=["acme.com"]),  # declared, no source_url
    )
    gaps = snoop._resolution_gaps(person)
    blob = " ".join(gaps)
    assert "personal_domains" in blob
    assert "handles" in blob
    assert "source_url" in blob


def test_resolution_gaps_empty_for_rich_plan():
    person = Person(
        name="X", ambiguity="single_plausible_match",
        personal_domains=["x.com"], handles={"github": "x"},
        employer=Employer(name="Acme", domains=["acme.com"], source_url="https://n/acme"),
    )
    assert snoop._resolution_gaps(person) == []


def test_main_thin_plan_emits_resolution_gaps(monkeypatch, capsys):
    import json as _json
    monkeypatch.setattr(snoop, "resolve_person", lambda name, **kw: Person(
        name=name, ambiguity="insufficient_identity_evidence",
        employer=Employer(name="Acme", domains=["acme.com"]),
    ))
    empty = ResolverResult(resolver="x", candidates=[], status="empty")
    for fn in ("fetch_git_emails", "fetch_gh_profile",
               "fetch_personal_site", "fetch_pattern_candidates"):
        monkeypatch.setattr(snoop, fn, lambda *a, **kw: empty)
    rc = snoop.main(["X", "--no-smtp"])
    assert rc == 0
    bundle = _json.loads(capsys.readouterr().out)
    assert "resolution_gaps" in bundle and bundle["resolution_gaps"]


def test_main_rich_plan_has_no_resolution_gaps_key(monkeypatch, capsys):
    import json as _json
    monkeypatch.setattr(snoop, "resolve_person", lambda name, **kw: Person(
        name=name, ambiguity="single_plausible_match",
        personal_domains=["x.com"], handles={"github": "x"},
        bound_anchors=[("github_name_match", name), ("github_employer_match", "Acme")],
        employer=Employer(name="Acme", domains=["acme.com"], source_url="https://n/acme"),
    ))
    empty = ResolverResult(resolver="x", candidates=[], status="empty")
    for fn in ("fetch_git_emails", "fetch_gh_profile", "fetch_personal_site",
               "fetch_pattern_candidates"):
        monkeypatch.setattr(snoop, fn, lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_recent_repos", lambda *a, **kw: [])
    rc = snoop.main(["X", "--no-smtp"])
    assert rc == 0
    bundle = _json.loads(capsys.readouterr().out)
    assert "resolution_gaps" not in bundle


# ---- --verify single-address path -------------------------------------------


def _no_discovery(monkeypatch):
    """Fail loudly if any discovery resolver is called — verify mode must skip
    person discovery entirely."""
    def boom(*a, **kw):
        raise AssertionError("discovery resolver called in --verify mode")
    for fn in ("fetch_git_emails", "fetch_gh_profile",
               "fetch_personal_site", "fetch_pattern_candidates"):
        monkeypatch.setattr(snoop, fn, boom)


def test_verify_flag_probes_single_address(monkeypatch, capsys):
    import json as _json
    _no_discovery(monkeypatch)
    captured = {}
    monkeypatch.setattr(snoop, "verify_candidates",
                        lambda cands, **kw: captured.setdefault("probed", list(cands)))
    rc = snoop.main(["--verify", "jane@acme.com"])
    assert rc == 0
    bundle = _json.loads(capsys.readouterr().out)
    blob = "\n".join(o["content"] for o in bundle["observations"])
    assert "jane@acme.com" in blob
    assert "sources=manual_known" in blob  # marked user-supplied for verification
    # the address was handed to the SMTP sensor
    assert [c.address for c in captured["probed"]] == ["jane@acme.com"]


def test_bare_email_positional_routes_to_verify(monkeypatch, capsys):
    import json as _json
    _no_discovery(monkeypatch)
    monkeypatch.setattr(snoop, "verify_candidates", lambda cands, **kw: cands)
    rc = snoop.main(["bob@example.com", "--no-smtp"])
    assert rc == 0
    bundle = _json.loads(capsys.readouterr().out)
    assert any("bob@example.com" in o["content"] for o in bundle["observations"])


def test_verify_deduplicates_and_lowercases(monkeypatch, capsys):
    import json as _json
    _no_discovery(monkeypatch)
    monkeypatch.setattr(snoop, "verify_candidates", lambda cands, **kw: cands)
    rc = snoop.main(["--verify", "Jane@Acme.com", "--verify", "jane@acme.com", "--no-smtp"])
    assert rc == 0
    bundle = _json.loads(capsys.readouterr().out)
    email_obs = [o for o in bundle["observations"] if o["type"] == "email_candidate"]
    assert len(email_obs) == 1
    assert "jane@acme.com" in email_obs[0]["content"]


def test_allow_google_skipped_when_no_cookies(monkeypatch, capsys):
    """Passing --allow-google-account is always safe: when the capability probe
    finds no Google session, the probe is skipped (not attempted-and-failed)."""
    from lib.diagnose import Capability
    _no_discovery(monkeypatch)
    monkeypatch.setattr(snoop, "verify_candidates", lambda cands, **kw: cands)
    # capability probe reports google missing
    monkeypatch.setattr(snoop.diagnose, "_probe_google_account",
                        lambda: Capability(name="google_account", status="missing",
                                           detail="no cookies", impact="P3"))
    called = {"google": False}
    def fake_google(*a, **kw):
        called["google"] = True
        raise AssertionError("fetch_google_account must not run without a session")
    monkeypatch.setattr(snoop, "fetch_google_account", fake_google)
    rc = snoop.main(["--verify", "jane@acme.com", "--no-smtp", "--allow-google-account"])
    assert rc == 0
    assert called["google"] is False


# ---- sensor mode (--observations) + verifier mode (--ground) -----------------


def test_observations_emits_raw_bundle(monkeypatch, capsys):
    """--observations dumps the typed observation bundle the host reasons over —
    no scoring, no card."""
    import json as _json
    _resolver_setup(monkeypatch)
    rc = snoop.main(["Alice Smith", "--no-smtp", "--observations"])
    assert rc == 0
    bundle = _json.loads(capsys.readouterr().out)
    assert bundle["person"]["name"] == "Alice Smith"
    assert isinstance(bundle["observations"], list) and bundle["observations"]
    # the scored candidate shows up as an observation the host can cite
    blob = "\n".join(o["content"] for o in bundle["observations"])
    assert "alice@corp.com" in blob
    assert all({"id", "type", "content"} <= set(o) for o in bundle["observations"])


def test_observations_includes_work_search(monkeypatch, capsys):
    import json as _json
    _resolver_setup(monkeypatch)
    plan = '{"work_search_results":[{"title":"Talk on widgets","url":"https://c/x"}]}'
    rc = snoop.main(["Alice Smith", "--no-smtp", "--observations", "--person-plan", plan])
    assert rc == 0
    bundle = _json.loads(capsys.readouterr().out)
    assert any(o["type"] == "web_search" for o in bundle["observations"])


def _ground_stdin(monkeypatch, payload):
    import io
    import json as _json
    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps(payload)))


def test_bundle_carries_schema_version(monkeypatch, capsys):
    import json as _json
    _resolver_setup(monkeypatch)
    snoop.main(["Alice Smith", "--no-smtp", "--observations"])
    bundle = _json.loads(capsys.readouterr().out)
    assert bundle["schema"] == 2


def test_ground_rejects_stale_v1_bundle_file(monkeypatch, capsys, tmp_path):
    """--observations-file with a schema-v1 (or unversioned) bundle is rejected
    with a named error pointing at re-running --observations."""
    import io
    import json as _json
    stale = tmp_path / "old.json"
    stale.write_text(_json.dumps({
        "warnings": [], "person": {"name": "X", "ambiguity": "single_plausible_match"},
        "observations": [{"id": "o1", "type": "email_candidate",
                          "content": "candidate email: x@y.com"}],
    }))  # no "schema" key → v1
    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps({"facts": []})))
    rc = snoop.main(["--ground", "--observations-file", str(stale)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "schema" in err and "re-run --observations" in err


def test_ground_honest_blank_from_bundle_sensors(monkeypatch, capsys, tmp_path):
    """4A end-to-end: a zero-fact ground against a bundle carrying run records
    renders the honest blank — checked / not-checked / why — from the bundle's
    `sensors` array, not a bare one-liner."""
    import io
    import json as _json
    bundle = tmp_path / "obs.json"
    bundle.write_text(_json.dumps({
        "schema": snoop.BUNDLE_SCHEMA_VERSION,
        "person": {"name": "Nobody Match", "ambiguity": "insufficient_identity_evidence"},
        "observations": [],
        "sensors": [
            {"sensor": "pattern_gen", "status": "ran", "outcome": "candidates"},
            {"sensor": "personal_site", "status": "skipped",
             "reason": "no personal_domains in plan"},
            {"sensor": "smtp", "status": "skipped", "reason": "--no-smtp"},
        ],
    }))
    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps({"facts": []})))
    rc = snoop.main(["--ground", "--observations-file", str(bundle)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "What snoop checked:" in out
    assert "ran: pattern_gen" in out
    assert "personal_site — no personal_domains in plan" in out


def test_ground_drops_uncited_facts_and_renders(monkeypatch, capsys):
    """--ground keeps facts citing a real observation, drops the rest, renders."""
    payload = {
        "person": {"name": "Alice Smith", "ambiguity": "single_plausible_match"},
        "summary": "Alice Smith, engineer.",
        "observations": [
            {"id": "o1", "type": "email_candidate",
             "content": "candidate email: alice@corp.com (sources=gh_profile)"},
        ],
        "facts": [
            {"kind": "email", "label": "", "value": "alice@corp.com", "detail": "",
             "confidence": 0.9, "evidence_ids": ["o1"], "reasoning": "profile"},
            {"kind": "work_item", "label": "", "value": "ghost talk", "detail": "",
             "confidence": 0.8, "evidence_ids": ["o404"], "reasoning": "hallucinated"},
        ],
    }
    _ground_stdin(monkeypatch, payload)
    rc = snoop.main(["--ground"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Alice Smith, engineer." in out
    assert "alice@corp.com" in out      # cited a real observation -> kept
    assert "ghost talk" not in out      # cited o404 (nonexistent) -> dropped


def test_ground_invalid_json_returns_error(monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    rc = snoop.main(["--ground"])
    assert rc == 2


def test_observations_to_ground_roundtrip(monkeypatch, capsys):
    """The real in-Claude-Code loop: snoop --observations -> (host reasons) ->
    snoop --ground. Here the 'host' is the test, fabricating a cited fact from
    the emitted bundle."""
    import json as _json
    _resolver_setup(monkeypatch)

    # 1. sensor: get the bundle
    snoop.main(["Alice Smith", "--no-smtp", "--observations"])
    bundle = _json.loads(capsys.readouterr().out)
    email_obs = next(o for o in bundle["observations"] if o["type"] == "email_candidate")

    # 2. "host" reasons: produce a fact citing that observation
    payload = {
        "person": bundle["person"],
        "summary": "Alice Smith — best reached at her work address.",
        "observations": bundle["observations"],
        "facts": [{
            "kind": "email", "label": "", "value": "alice@corp.com", "detail": "work",
            "confidence": 0.9, "evidence_ids": [email_obs["id"]], "reasoning": "from bundle",
        }],
    }

    # 3. verifier: ground it
    _ground_stdin(monkeypatch, payload)
    rc = snoop.main(["--ground"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Alice Smith — best reached" in out
    assert "alice@corp.com" in out
    assert "(unverified)" not in out.split("alice@corp.com")[0].splitlines()[-1]


def test_out_writes_bundle_to_file_and_prints_pointer(monkeypatch, capsys, tmp_path):
    """--out writes the bundle to a file (not stdout) and prints the ready-to-run
    --ground command, so the host model reads the bundle from disk."""
    import json as _json
    _resolver_setup(monkeypatch)
    out = tmp_path / "obs.json"
    rc = snoop.main(["Alice Smith", "--no-smtp", "--out", str(out)])
    assert rc == 0
    printed = capsys.readouterr().out
    assert "--ground --observations-file" in printed
    assert str(out) in printed
    # the file is a valid bundle; stdout printed a pointer, not the bundle itself
    bundle = _json.loads(out.read_text())
    assert bundle["person"]["name"] == "Alice Smith"
    assert '"observations"' not in printed  # the observation array stayed in the file


def test_observations_file_supplies_bundle_to_ground(monkeypatch, capsys, tmp_path):
    """--ground --observations-file loads observations from the file, so stdin
    only needs {person, summary, facts} — no re-typing the bundle."""
    import io
    import json as _json
    _resolver_setup(monkeypatch)

    out = tmp_path / "obs.json"
    snoop.main(["Alice Smith", "--no-smtp", "--out", str(out)])
    capsys.readouterr()
    bundle = _json.loads(out.read_text())
    email_id = next(o["id"] for o in bundle["observations"]
                    if o["type"] == "email_candidate")

    # stdin carries ONLY person + summary + facts (no observations array)
    stdin_payload = {
        "person": {"name": "Alice Smith", "ambiguity": "single_plausible_match"},
        "summary": "Alice Smith.",
        "facts": [{
            "kind": "email", "label": "", "value": "alice@corp.com", "detail": "",
            "confidence": 0.9, "evidence_ids": [email_id], "reasoning": "from file",
        }],
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps(stdin_payload)))
    rc = snoop.main(["--ground", "--observations-file", str(out)])
    assert rc == 0
    out_text = capsys.readouterr().out
    assert "alice@corp.com" in out_text  # fact survived because its citation resolved


def test_observations_file_drops_fact_when_citation_absent(monkeypatch, capsys, tmp_path):
    """A fact citing an id NOT in the file's bundle is dropped — the file is the
    authoritative observation set."""
    import io
    import json as _json
    _resolver_setup(monkeypatch)
    out = tmp_path / "obs.json"
    snoop.main(["Alice Smith", "--no-smtp", "--out", str(out)])
    capsys.readouterr()

    stdin_payload = {
        "person": {"name": "Alice Smith", "ambiguity": "single_plausible_match"},
        "summary": "Alice Smith.",
        "facts": [{
            "kind": "email", "label": "", "value": "ghost@corp.com", "detail": "",
            "confidence": 0.9, "evidence_ids": ["o9999"], "reasoning": "hallucinated",
        }],
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(_json.dumps(stdin_payload)))
    rc = snoop.main(["--ground", "--observations-file", str(out)])
    assert rc == 0
    out_text = capsys.readouterr().out
    assert "ghost@corp.com" not in out_text
    assert "No attributable facts" in out_text
