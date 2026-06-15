"""Spec-driven sensor wiring for the real snoop pipeline — one source of truth.

Both the ENG-7 acceptance test (tests/test_acceptance.py) and the eval fixture
builder (tests/evals/make_fixture.py) need the same thing: drive the REAL
`snoop.main()` Step-2 path with every sensor monkeypatched to canned readings, so
the produced bundle is schema-true by construction. `wire_pipeline(patcher, spec)`
is that wiring, generalized from the acceptance test's steipete-only helper.

Contract notes (load-bearing — read before writing a spec):

- Patch targets are attributes ON THE `snoop` MODULE, never `lib.*`: snoop.py
  rebinds every sensor via from-imports, so patching the lib modules silently
  does nothing.
- `patcher` is anything with a pytest-style `.setattr(obj, name, value)` — the
  `monkeypatch` fixture inside pytest, or a `pytest.MonkeyPatch()` instantiated
  directly by make_fixture.py outside pytest (call `.undo()` yourself there).
- The wiring is self-sufficient outside pytest: it also stubs what conftest's
  autouse `_stub_unconditional_network` fixture covers (`fetch_pgp_emails`,
  `verify_rel_me`, `append_run`) plus the sensors the acceptance helper never
  needed (`fetch_hn_profile`, `fetch_package_emails`) — a spec with an `hn`
  handle or plan `packages` must not hit the network.
- Determinism stubs for byte-stable committed bundles: the capability probe is
  canned (no gh subprocess, no Chrome cookie reads — `bundle["warnings"]` stops
  being machine-dependent) and every `bundle["sensors"][*].elapsed_ms` is
  normalized to 0 (wall-clock noise otherwise differs per run/machine).
- Probe-merge contract: Phase 2 probes run on staged COPIES and
  `snoop._merge_probe_verdicts` copies back ONLY smtp_verdict, account_exists,
  account_display_name, account_photo_url, mx_provider, and google_account
  sources. `ProbeOutcome` carries exactly the mergeable fields. In particular:
    * `gaia_id` is NOT merged back — a fixture needing Gaia clustering must set
      `gaia_id` on the sensor-emitted EmailCandidate itself (Phase 2 doesn't
      overwrite it);
    * `name_match` is not settable anywhere — lib/reason.py derives it from
      `account_display_name` vs `person.name`; control it via the display name.
- Each fetcher returns a deep copy of its canned reading per call, so one spec
  can drive `snoop.main()` repeatedly without runs contaminating each other
  (probes mutate candidates in place).
"""

from __future__ import annotations

import copy
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SKILL_ROOT = Path(__file__).resolve().parents[2]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

import snoop  # noqa: E402
from lib.diagnose import Capability  # noqa: E402
from lib.schema import (  # noqa: E402
    EmailCandidate,
    Employer,
    Person,
    ResolverResult,
    Source,
)

# Pinned observation time for canned Sources, so spec-built bundles never carry
# build-machine wall-clock. (observed_at is dropped from the bundle's data
# mirror today, but pinning keeps any future serialization byte-stable too.)
NOW = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)


@dataclass
class ProbeOutcome:
    """Per-address Phase-2 probe verdicts — exactly the fields
    `snoop._merge_probe_verdicts` copies back from the staged probe copies.
    `None` means "the probe didn't touch this field" (the candidate keeps
    whatever the sensors set)."""
    smtp_verdict: str | None = None          # SmtpVerdict literal, e.g. "verified"
    mx_provider: str | None = None           # "google" | "microsoft" | "other"
    account_exists: str | None = None        # AccountExistsVerdict literal
    account_display_name: str | None = None  # drives reason.py's name_match field
    account_photo_url: str | None = None


@dataclass
class PipelineSpec:
    """Everything wire_pipeline needs to canned-wire one pipeline run.

    Sensor fields left at None default to an honest-empty ResolverResult named
    after the sensor. Sensors only fire when the person/plan gates them in
    (hn_profile needs `handles["hn"]`, package_emails needs plan `packages`,
    personal_site/rel_me need `personal_domains` — see snoop.run_pipeline)."""
    # Step-1 resolution output: name/handles/personal_domains/employer/
    # ambiguity/bound_anchors all live on the Person.
    person: Person

    # Per-sensor canned ResolverResults (Phase-1 fan-out + corroboration).
    git_emails: ResolverResult | None = None
    gh_profile: ResolverResult | None = None
    personal_site: ResolverResult | None = None
    pattern_candidates: ResolverResult | None = None
    hn_profile: ResolverResult | None = None
    package_emails: ResolverResult | None = None
    pgp: ResolverResult | None = None
    recent_repos: list = field(default_factory=list)      # list[GitHubRepo]
    rel_me: dict[str, list] = field(default_factory=dict)  # domain -> rel=me links

    # Per-address Phase-2 verdicts, keyed by lowercased address. Addresses the
    # probe phase never stages (unbound, non-Workspace) are simply not touched.
    probes: dict[str, ProbeOutcome] = field(default_factory=dict)

    # When True the canned capability probe reports a usable Google session, so
    # `--allow-google-account` actually arms the (mocked) existence check.
    google_ready: bool = False

    # Domains the mocked MX classifier should treat as Google-hosted. The real
    # `_autodetect_workspace_domains` calls `is_google_hosted` (a live MX lookup)
    # on every candidate domain whenever the Google check is armed — left
    # unstubbed, a Google fixture would hit the network on .example domains. The
    # mock stubs it unconditionally (empty set ⇒ always-False), so even the
    # non-Google fixtures can never reach a real resolver.
    google_hosted_domains: set[str] = field(default_factory=set)


def _empty(name: str) -> ResolverResult:
    return ResolverResult(resolver=name, candidates=[], status="empty")


def _canned(result: ResolverResult | None, name: str):
    """A fetcher returning a fresh deep copy of the canned result per call."""
    def fetch(*a: Any, **kw: Any) -> ResolverResult:
        return copy.deepcopy(result) if result is not None else _empty(name)
    return fetch


def wire_pipeline(patcher, spec: PipelineSpec) -> None:
    """Monkeypatch every sensor + probe + determinism seam on the snoop module
    to the spec's canned readings. After this, `snoop.main([...])` runs the real
    pipeline (clustering, binding, probe staging/merging, bundle emission)
    against the spec's world with zero network and zero machine-dependence."""
    # -- Step 1: resolution. Deep-copied per call: main() mutates the Person
    # (notes, gh_recent_repos, ambiguity promotion), and the spec must stay
    # pristine across runs.
    patcher.setattr(snoop, "resolve_person",
                    lambda name, **kw: copy.deepcopy(spec.person))

    # -- Phase-1 sensor fan-out (gated in run_pipeline by handles/domains/plan).
    patcher.setattr(snoop, "fetch_git_emails", _canned(spec.git_emails, "git_emails"))
    patcher.setattr(snoop, "fetch_gh_profile", _canned(spec.gh_profile, "gh_profile"))
    patcher.setattr(snoop, "fetch_personal_site",
                    _canned(spec.personal_site, "personal_site"))
    patcher.setattr(snoop, "fetch_pattern_candidates",
                    _canned(spec.pattern_candidates, "pattern_gen"))
    patcher.setattr(snoop, "fetch_hn_profile", _canned(spec.hn_profile, "hn_profile"))
    patcher.setattr(snoop, "fetch_package_emails",
                    _canned(spec.package_emails, "package_registry"))
    patcher.setattr(snoop, "fetch_recent_repos",
                    lambda *a, **kw: copy.deepcopy(spec.recent_repos))

    # -- Corroboration + ledger: the stubs conftest's autouse fixture provides
    # under pytest, folded in here because make_fixture.py runs outside pytest
    # (without these it would hit keys.openpgp.org / the rel=me fetch and write
    # the real ~/.snoop/ledger.jsonl).
    patcher.setattr(snoop, "fetch_pgp_emails", _canned(spec.pgp, "pgp"))
    patcher.setattr(snoop, "verify_rel_me",
                    lambda domain, **kw: copy.deepcopy(spec.rel_me.get(domain, [])))
    patcher.setattr(snoop, "append_run", lambda rec, **kw: True)

    # -- Phase-2 probes. Both mutate candidates IN PLACE (the real contract);
    # the pipeline stages copies and merges the mergeable fields back itself.
    def verify(cands, **kw):
        for c in cands:
            outcome = spec.probes.get(c.address.lower())
            if outcome is None or outcome.smtp_verdict is None:
                continue
            c.smtp_verdict = outcome.smtp_verdict
            if outcome.mx_provider is not None:
                c.mx_provider = outcome.mx_provider
    patcher.setattr(snoop, "verify_candidates", verify)

    def google_account(cands, **kw):
        touched = 0
        for c in cands:
            outcome = spec.probes.get(c.address.lower())
            if outcome is None or outcome.account_exists is None:
                continue
            c.account_exists = outcome.account_exists
            if outcome.account_display_name is not None:
                c.account_display_name = outcome.account_display_name
            if outcome.account_photo_url is not None:
                c.account_photo_url = outcome.account_photo_url
            touched += 1
        return ResolverResult(resolver="google_account", candidates=[],
                              status="ok" if touched else "empty")
    patcher.setattr(snoop, "fetch_google_account", google_account)

    # -- Determinism seams. The real capability probe shells out to gh, imports
    # dnspython, writes a ~/.snoop probe file, and (with --allow-google-account)
    # reads the Chrome cookie DB — all of which make bundle["warnings"] depend
    # on the build machine. Can it: everything "ok" yields zero warnings, and
    # google_ready decides whether _google_ready() arms the existence check.
    caps = [
        Capability(name="gh_cli", status="ok", detail="canned", impact=""),
        Capability(name="dnspython", status="ok", detail="canned", impact=""),
        Capability(name="snoop_state_dir", status="ok", detail="canned", impact=""),
    ]
    if spec.google_ready:
        caps.append(Capability(name="google_account", status="ok",
                               detail="canned", impact=""))
    patcher.setattr(snoop, "_fast_capability_probe",
                    lambda *, allow_google_account=False: list(caps))

    # MX classification seam. _autodetect_workspace_domains shells out to a live
    # MX lookup for every candidate domain once the Google check is armed. We
    # CANNOT stub that by patching `snoop.is_google_hosted`: the function binds it
    # as a default parameter at import time, so the rebinding never takes effect
    # (and the real lookup hits the network on .example domains). Stub the whole
    # autodetect to classify against the spec instead — empty set ⇒ no Workspace
    # domains ⇒ the Google burst never arms, keeping every other fixture hermetic.
    hosted = {d.lower() for d in spec.google_hosted_domains}

    def autodetect_workspace(candidates, explicit, **kw):
        explicit_set = {d.strip().lower() for d in explicit or [] if isinstance(d, str)}
        seen: set[str] = set()
        additions: list[str] = []
        for c in candidates:
            if "@" not in c.address:
                continue
            d = c.address.rsplit("@", 1)[1].lower()
            if d in seen or d in explicit_set:
                continue
            seen.add(d)
            if d in hosted:
                additions.append(d)
        return list(explicit or []) + additions
    patcher.setattr(snoop, "_autodetect_workspace_domains", autodetect_workspace)

    # elapsed_ms is wall-clock noise; zero it in the emitted bundle so committed
    # fixtures are byte-stable across machines and reruns.
    real_build_bundle = snoop._build_bundle

    def build_bundle(*a: Any, **kw: Any) -> dict[str, Any]:
        bundle = real_build_bundle(*a, **kw)
        for rec in bundle.get("sensors", []):
            if "elapsed_ms" in rec:
                rec["elapsed_ms"] = 0
        return bundle
    patcher.setattr(snoop, "_build_bundle", build_bundle)


def documented_spec() -> PipelineSpec:
    """The SKILL.md Tier-1 worked example, exactly as the acceptance test has
    always wired it: Peter Steinberger, github-bound, pete@openai.com observed
    in git commits on the employer domain (so it BINDS under ENG-8) and
    SMTP-verified. The real-person identity stays HERE — the acceptance test's
    job is pinning the documented example; eval fixtures are fully fictional.

    Quirk kept for fidelity with the original wiring: the empty fan-out sensors
    share one ResolverResult named "x" (so bundle sensor records read "x", as
    they always have under the acceptance mocks)."""
    empty = _empty("x")
    return PipelineSpec(
        person=Person(
            name="Peter Steinberger", handles={"github": "steipete"},
            personal_domains=["steipete.com"],
            employer=Employer(name="OpenAI", domains=["openai.com"]),
            ambiguity="single_plausible_match",
            bound_anchors=[("github_name_match", "Peter Steinberger"),
                           ("github_handle_exists", "steipete")],
        ),
        git_emails=ResolverResult(
            resolver="git_emails",
            candidates=[EmailCandidate(
                address="pete@openai.com", employer_match=True,
                sources=[Source(type="git_commit", url="https://github.com/steipete",
                                observed_at=NOW, detail="commit author email")])],
            status="ok"),
        gh_profile=empty,
        personal_site=empty,
        pattern_candidates=empty,
        # The documented flow is the SMTP-verified path; the Google existence
        # check stays disarmed (google_ready=False) so --allow-google-account
        # in the documented command can't reach a probe.
        probes={"pete@openai.com": ProbeOutcome(smtp_verdict="verified",
                                                mx_provider="other")},
    )
