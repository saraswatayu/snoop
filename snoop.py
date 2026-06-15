#!/usr/bin/env python3
"""snoop.py — the sensor entry point for the /snoop skill.

snoop does the I/O the host model can't, and emits a typed observation bundle.
The host model (already running in Claude Code) is the analyst: it reasons over
the bundle, then runs `--ground` to deterministically check its citations.

Pipeline:

    parse args → load --person-plan JSON
              ↓
    person_resolve.resolve_person  (validate identity anchors, surface deltas)
              ↓
    fan-out (ThreadPoolExecutor, per-resolver timeout):
       git_emails | gh_profile | personal_site | pattern_gen
              ↓
    cluster  (dedupe by lowercased address; merge source lists)
              ↓
    order  (_probe_rank: observed addresses before pure name×domain guesses)
              ↓
    google_account probe  (bound candidates + unbound Workspace pattern guesses,
                           --allow-google-account; the authed disambiguator)
              ↓
    verify_smtp top-K BOUND candidates  (skip personal-provider / known-dead)
              ↓
    build_evidence → emit the observation bundle as JSON

Modes:
    (default / --observations) emit the observation bundle
    --ground                   read {observations, facts} on stdin, drop uncited
                               facts, render the grounded card
    --diagnose                 capability probe and exit
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from lib import __version__, diagnose, reason, render
from lib.diagnose import Capability
from lib.git_emails import fetch_git_emails
from lib.gh_profile import fetch_gh_profile, fetch_recent_repos
from lib.google_account import fetch_google_account
from lib.hn_profile import fetch_hn_profile
from lib.ledger import append_run, build_record, ledger_health
from lib.normalize import (
    is_personal_provider,
    localpart_templates,
    name_match,
    parse_name,
)
from lib.package_registry import fetch_package_emails
from lib.pattern_gen import _DEFAULT_TEMPLATE_ORDER, fetch_pattern_candidates
from lib.person_resolve import resolve_person
from lib.pgp_keyserver import fetch_pgp_emails
from lib.rel_me import verify_rel_me
from lib.personal_site import fetch_personal_site
from lib.schema import (
    BUNDLE_SCHEMA_VERSION,
    EmailCandidate,
    Person,
    ResolverResult,
    RunRecord,
    Source,
)
from lib.verify_smtp import ProbeBudget, default_budget, is_google_hosted, verify_candidates


# Shared wall-clock budget for the whole sensor fan-out (E6). Sensors run
# concurrently, so each gets up to the full budget; a sensor still running at the
# deadline is abandoned (its daemon thread + its own socket timeout are the real
# backstops — Python can't kill a blocking thread). The floor is the minimum the
# budget can be clamped to, so a tiny --deadline never starves every sensor.
_DEFAULT_DEADLINE_SEC = 60.0
_PER_SENSOR_FLOOR_SEC = 2.0


# ---- capability warnings (top-of-card error surface) ------------------------


def _capability_warnings(
    capabilities: list[Capability],
    *,
    allow_google_account: bool,
) -> list[str]:
    """Translate diagnose results into one-line, user-actionable warnings
    surfaced at the top of the compact card.

    Each warning follows the three-tier model: what's broken, why it
    matters for this lookup, how to fix it. Only surfaces degradations
    that materially affect the result — the user doesn't need to see
    "idna package missing" on a typical lookup since stdlib idna handles
    99% of real domains.

    `allow_google_account=False` suppresses google_account warnings;
    the user opted out of that path, so a missing cookie isn't actionable.
    """
    by_name = {c.name: c for c in capabilities}
    out: list[str] = []

    gh = by_name.get("gh_cli")
    if gh and gh.status == "missing":
        out.append(
            "gh CLI not installed — install GitHub CLI for full rate "
            "limit (5000 req/hr); falling back to 60 req/hr anonymous"
        )
    elif gh and gh.status == "degraded":
        out.append(
            "gh CLI not authenticated — run `gh auth login` to unlock "
            "5000 req/hr; falling back to 60 req/hr anonymous"
        )

    dns = by_name.get("dnspython")
    if dns and dns.status != "ok":
        out.append(
            "dnspython not installed — `pip install --user dnspython` "
            "to enable SMTP verification (currently disabled)"
        )

    if allow_google_account:
        google = by_name.get("google_account")
        if google and google.status == "missing":
            out.append(
                "--allow-google-account requested but no Google cookies "
                "found — sign into Google in Chrome and retry"
            )
        elif google and google.status == "degraded":
            out.append(
                "--allow-google-account requested but Google session is "
                "partial — sign out and back into Google to refresh"
            )

    state = by_name.get("snoop_state_dir")
    if state and state.status == "missing":
        out.append(
            f"~/.snoop unwritable ({state.detail}) — daily probe budget "
            f"will not persist between invocations"
        )

    return out


def _fast_capability_probe(*, allow_google_account: bool) -> list[Capability]:
    """Run only the probes that feed _capability_warnings. Skips the
    anon_github HTTP fetch (saves ~300ms) and idna/whois checks (not
    in the warning surface). Called once per main() invocation."""
    caps = [
        diagnose._probe_gh_cli(),
        diagnose._probe_dnspython(),
        diagnose._probe_snoop_state_dir(),
    ]
    if allow_google_account:
        caps.append(diagnose._probe_google_account())
    return caps


# ---- CLI plumbing -----------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="snoop",
        description="Resolve a person's email addresses across public sources.",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"snoop {__version__}",
    )
    p.add_argument(
        "name",
        nargs="?",
        help="Full name of the target (e.g. 'Peter Steinberger')",
    )
    p.add_argument(
        "employer",
        nargs="?",
        help=(
            "Optional employer name (e.g. 'Formation Bio'). Combined with "
            "--domain to form a minimal plan when --person-plan is not given."
        ),
    )
    p.add_argument(
        "--domain",
        action="append",
        default=[],
        metavar="DOMAIN",
        help=(
            "Repeatable. Email domain(s) the employer uses (e.g. 'formation.bio'). "
            "Used by pattern_gen and SMTP probing. Without it, the employer's "
            "email format can't be inferred and most candidates collapse to "
            "low-confidence pattern guesses."
        ),
    )
    p.add_argument(
        "--github",
        metavar="HANDLE",
        help=(
            "Target's GitHub username. Enables git_emails, gh_profile, "
            "gh_search, and the recent-repos dossier. Anchor binding still "
            "applies — a wrong handle gets caught and skipped."
        ),
    )
    p.add_argument(
        "--person-plan",
        help=(
            "JSON file path OR @file OR inline JSON string. Schema: "
            "{name, handles{github,x,hn}, personal_domains[], "
            "employer{name,domains[]}, former_employers[], channel_hints{}}. "
            "Wins over positional employer / --domain / --github for any "
            "field it specifies. Host model produces this upstream when it "
            "has richer context; person_resolve re-derives and surfaces deltas."
        ),
    )
    p.add_argument(
        "--known",
        action="append",
        metavar="EMAIL=Full Name",
        help=(
            "Repeatable. Same-company known address with name, used by "
            "pattern_gen to infer the company's email template."
        ),
    )
    p.add_argument(
        "--verify",
        action="append",
        default=[],
        metavar="EMAIL",
        help=(
            "Repeatable. Verify a specific address: skip person discovery and "
            "run the sensors (MX, SMTP, Google account) on EMAIL only, emitting "
            "the observation bundle for it. `snoop --verify jane@acme.com`. A "
            "bare positional that contains '@' is treated the same way."
        ),
    )
    p.add_argument(
        "--no-smtp",
        action="store_true",
        help="Skip SMTP probing entirely (faster; loses deliverable signal).",
    )
    p.add_argument(
        "--no-pgp",
        action="store_true",
        help="Skip the keys.openpgp.org corroboration of discovered addresses.",
    )
    p.add_argument(
        "--no-ledger",
        action="store_true",
        help=(
            "Don't append this run's yield metadata to the local ledger "
            "(~/.snoop/ledger.jsonl). The ledger stores sensor timing + plan "
            "shape only — never names, addresses, handles, or domains."
        ),
    )
    p.add_argument(
        "--deadline",
        type=float,
        default=_DEFAULT_DEADLINE_SEC,
        metavar="SEC",
        help=(
            f"Shared wall-clock budget for the sensor fan-out (default "
            f"{_DEFAULT_DEADLINE_SEC:g}s). Sensors run concurrently; one still "
            f"running at the deadline is abandoned and reports deadline-exceeded. "
            f"Clamped to a {_PER_SENSOR_FLOOR_SEC:g}s floor."
        ),
    )
    p.add_argument(
        "--no-search",
        action="store_true",
        help="Escape hatch: drop the host-supplied work_search_results from the "
             "bundle (the anchored sensor observations still emit).",
    )
    p.add_argument(
        "--observations",
        action="store_true",
        help="Emit the raw observation bundle as JSON — the I/O the host model "
             "can't do (git, GitHub, SMTP, Google, MX), typed and cited. This is "
             "the default and only non-ground output; the flag is accepted for "
             "explicitness and back-compat.",
    )
    p.add_argument(
        "--out",
        metavar="PATH",
        help="Write the observation bundle to PATH (JSON) instead of stdout, and "
             "print the ready-to-run --ground command. Lets the host model read "
             "the bundle from a file and pass only {person, summary, facts} to "
             "--ground, instead of re-typing the whole bundle through stdin.",
    )
    p.add_argument(
        "--ground",
        action="store_true",
        help="Verifier mode: read {observations, facts, ...} JSON on stdin and "
             "drop any fact whose citations don't reference a real observation, "
             "then render the grounded card. The deterministic citation check.",
    )
    p.add_argument(
        "--observations-file",
        metavar="PATH",
        help="For --ground: load the observation bundle from PATH (as written by "
             "--out) so stdin only needs {person, summary, facts}. Observations "
             "from the file are merged with whatever stdin also carries.",
    )
    p.add_argument(
        "--allow-google-account",
        action="store_true",
        help=(
            "Use Google's People API to verify candidate existence on Google-"
            "hosted domains. Reads cookies from your logged-in Chrome session. "
            "Default OFF — high volume can get your Google account flagged."
        ),
    )
    p.add_argument(
        "--google-workspace-domain",
        action="append",
        default=[],
        metavar="DOMAIN",
        help=(
            "Repeatable. Force DOMAIN into the Google-API probe set. Usually "
            "unnecessary: candidate addresses whose MX is Google-hosted are "
            "auto-detected. Use only to probe a domain that isn't already a "
            "candidate address. e.g. --google-workspace-domain acme.com"
        ),
    )
    p.add_argument(
        "--diagnose",
        action="store_true",
        help="Print a capability probe report and exit (no lookup).",
    )
    return p


def _plan_from_flags(args: argparse.Namespace) -> dict[str, Any]:
    """Build a minimal --person-plan-shaped dict from positional / flag args.

    Lets `snoop "Dan Neil" "Formation Bio" --domain formation.bio --github danielneil`
    work without forcing the user to construct JSON. Returns {} when nothing
    is provided. The result is merged UNDER any --person-plan dict in main(),
    so an explicit plan still wins for fields it specifies.
    """
    plan: dict[str, Any] = {}
    if args.name:
        plan["name"] = args.name
    if args.employer or args.domain:
        emp: dict[str, Any] = {}
        if args.employer:
            emp["name"] = args.employer
        if args.domain:
            emp["domains"] = list(args.domain)
        plan["employer"] = emp
    if args.github:
        plan["handles"] = {"github": args.github}
    return plan


def _load_plan(arg: str | None) -> dict[str, Any]:
    """Resolve --person-plan to a dict.

    Three forms accepted:
      - "@path/to/file.json" (literal @-prefix → file path)
      - "path/to/file.json"  (heuristic: if it looks like a path and exists)
      - "{... json ...}"     (inline)

    JSON parse errors are caught and re-raised as SystemExit with a clean
    one-line message naming where the parse came from. A raw json traceback
    here was the most common confusing error during dogfooding because the
    arg is usually 100+ chars of JSON and the user has no obvious entry point
    to fix the syntax.
    """
    if not arg:
        return {}
    arg = arg.strip()
    if not arg:
        return {}

    def _parse(text: str, source: str) -> dict[str, Any]:
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            snippet = text[:60].replace("\n", " ")
            if len(text) > 60:
                snippet += "..."
            raise SystemExit(
                f"--person-plan: invalid JSON ({source}): {e.msg} "
                f"at line {e.lineno} col {e.colno}\n"
                f"  near: {snippet}"
            ) from None

    if arg.startswith("@"):
        path = Path(arg[1:]).expanduser()
        try:
            text = path.read_text()
        except OSError as e:
            raise SystemExit(f"--person-plan: cannot read {path}: {e}") from None
        return _parse(text, f"file {path}")
    if arg.startswith("{") or arg.startswith("["):
        return _parse(arg, "inline")
    # Treat as file path if it exists
    p = Path(arg).expanduser()
    if p.exists():
        try:
            text = p.read_text()
        except OSError as e:
            raise SystemExit(f"--person-plan: cannot read {p}: {e}") from None
        return _parse(text, f"file {p}")
    raise SystemExit(f"--person-plan: cannot interpret {arg!r} as file path or JSON")


def _parse_knowns(values: list[str] | None) -> list[tuple[str, str | None]]:
    if not values:
        return []
    out: list[tuple[str, str | None]] = []
    for raw in values:
        email, _, name = raw.partition("=")
        email = email.strip()
        if not email:
            continue
        out.append((email, name.strip() or None))
    return out


# ---- pipeline orchestration -------------------------------------------------


def _gh_handle(person: Person) -> str | None:
    """Return the github handle ONLY if person_resolve actually bound it.
    A handle that exists but didn't bind anchors is an untrusted hint;
    don't fan out resolvers on it (defense against host hallucination)."""
    handle = person.handles.get("github")
    if not handle:
        return None
    validating = [a for a in person.bound_anchors if a[0] != "github_handle_exists"]
    if len(validating) == 0:
        # Handle exists but no validating anchors. Use it for resolvers
        # only when ambiguity is single_plausible_match; otherwise treat
        # as untrusted hint and skip.
        if person.ambiguity != "single_plausible_match":
            return None
    return handle


def _run_resolver(
    name: str,
    fn: Callable[[], ResolverResult],
) -> ResolverResult:
    """Wrap a resolver call with a uniform error → ResolverResult contract, so a
    crashing sensor degrades to status="error" instead of taking down the run
    (crash isolation)."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return ResolverResult(
            resolver=name,
            candidates=[],
            status="error",
            error_detail=f"{type(e).__name__}: {e}",
        )


def run_pipeline(
    person: Person,
    *,
    manual_known: list[tuple[str, str | None]] | None = None,
    packages: list[dict] | None = None,
    deadline_sec: float = _DEFAULT_DEADLINE_SEC,
    per_sensor_floor_sec: float = _PER_SENSOR_FLOOR_SEC,
) -> list[ResolverResult]:
    """Fan out all enabled sensors concurrently under one shared wall-clock
    deadline.

    Each sensor runs in a daemon thread, stamping its own elapsed_ms. We wait for
    them collectively up to `deadline_sec` (clamped to at least the per-sensor
    floor). A sensor that finishes in time keeps its result; one still running at
    the deadline is **abandoned** — its daemon thread is left to die with the
    process (Python can't kill a blocking thread; the sensor's own socket timeout
    is the true backstop) and it reports `status="timeout"` with a
    `deadline-exceeded` reason, distinct from a sensor's internal timeout. A
    crashing sensor is isolated to `status="error"`. The run never blocks on a
    hung socket — the defect the old per-future ThreadPoolExecutor join had.
    """
    manual_known = manual_known or []
    packages = packages or []
    gh_handle = _gh_handle(person)

    tasks: list[tuple[str, Callable[[], ResolverResult]]] = []
    if gh_handle:
        tasks.append(("git_emails", lambda: fetch_git_emails(gh_handle)))
        tasks.append(("gh_profile", lambda: fetch_gh_profile(gh_handle)))
    # HN handle is an untrusted hint (not anchor-validated like github), so an
    # address found here is weakly bound — the host marks it [?]. One fetch.
    hn_handle = person.handles.get("hn")
    if hn_handle:
        tasks.append(("hn_profile", lambda: fetch_hn_profile(hn_handle)))
    if person.personal_domains:
        tasks.append((
            "personal_site",
            lambda: fetch_personal_site(person.personal_domains),
        ))
    # Package-registry publisher emails when the host supplied known packages.
    if packages:
        tasks.append(("package_registry", lambda: fetch_package_emails(packages)))
    # pattern_gen runs even with no other inputs (it's the explicit fallback)
    tasks.append((
        "pattern_gen",
        lambda: fetch_pattern_candidates(person, manual_known=manual_known),
    ))

    results_by_name: dict[str, ResolverResult] = {}
    lock = threading.Lock()

    def worker(name: str, fn: Callable[[], ResolverResult]) -> None:
        t0 = time.monotonic()
        rr = _run_resolver(name, fn)
        rr.elapsed_ms = int((time.monotonic() - t0) * 1000)
        with lock:
            results_by_name[name] = rr

    threads: list[tuple[str, threading.Thread]] = []
    for name, fn in tasks:
        th = threading.Thread(target=worker, args=(name, fn),
                              name=f"snoop:{name}", daemon=True)
        th.start()
        threads.append((name, th))

    budget = max(deadline_sec, per_sensor_floor_sec)
    deadline = time.monotonic() + budget
    for _name, th in threads:
        th.join(timeout=max(0.0, deadline - time.monotonic()))

    results: list[ResolverResult] = []
    for name, _fn in tasks:
        with lock:
            rr = results_by_name.get(name)
        if rr is not None:
            results.append(rr)
        else:
            results.append(ResolverResult(
                resolver=name,
                candidates=[],
                status="timeout",
                elapsed_ms=int(budget * 1000),
                error_detail=f"deadline-exceeded: abandoned after {budget:g}s shared budget",
            ))
    return results


def _sensor_skips(
    person: Person, *, packages: list[dict], no_smtp: bool, no_pgp: bool,
    allow_google_account: bool, dns_available: bool = True,
) -> list[RunRecord]:
    """Synthesize 'skipped' RunRecords for sensors that COULD run but didn't, with
    a reason — the typed-degradation half of the contract. Pairs with the
    ran/degraded records from the resolver fan-out so the bundle reads 'checked X,
    didn't check Y because Z' instead of silently omitting a sensor."""
    skips: list[RunRecord] = []

    def skip(name: str, reason: str) -> None:
        skips.append(RunRecord(sensor=name, status="skipped", reason=reason))

    if not _gh_handle(person):
        skip("git_emails", "no bound github handle")
        skip("gh_profile", "no bound github handle")
    if not person.handles.get("hn"):
        skip("hn_profile", "no hn handle in plan")
    if not person.personal_domains:
        skip("personal_site", "no personal_domains in plan")
        skip("rel_me", "no personal_domains in plan")
    if not packages:
        skip("package_registry", "no packages in plan")
    if not allow_google_account:
        skip("google_account", "--allow-google-account not set")
    if no_smtp:
        skip("smtp", "--no-smtp")
    elif not dns_available:
        # SMTP wasn't disabled by the user — it can't run because MX lookups need
        # dnspython, which isn't installed. Surface that as a typed sensor skip
        # (the first-run degradation case), not just a top-of-bundle warning.
        skip("smtp", "dependency dnspython missing")
    if no_pgp:
        skip("pgp", "--no-pgp")
    return skips


def _pgp_corroborate(
    candidates: list[EmailCandidate],
) -> tuple[RunRecord, list[tuple[str, Source]]]:
    """Corroborate discovered addresses against keys.openpgp.org (E3). A hit is an
    OWNER-VERIFIED positive by construction (the keyserver only publishes a UID
    after the owner confirmed it) — especially valuable on M365-inconclusive
    addresses. Returns the RunRecord and the `pgp` Source merges to apply (address
    lowercased -> Source); the caller commits them so an abandoned slow probe
    leaks nothing. Best-effort: never raises, never blocks the run on a failure."""
    addrs = [c.address for c in candidates if "@" in c.address]
    if not addrs:
        return (RunRecord(sensor="pgp", status="skipped",
                          reason="no candidate addresses to check"), [])
    t0 = time.monotonic()
    result = fetch_pgp_emails(addrs)
    result.elapsed_ms = int((time.monotonic() - t0) * 1000)
    merges = [(pc.address.lower(), s) for pc in result.candidates for s in pc.sources]
    return RunRecord.from_resolver(result), merges


def _apply_pgp_merges(candidates: list[EmailCandidate],
                      merges: list[tuple[str, Source]]) -> None:
    """Merge the pgp Sources from _pgp_corroborate onto the matching candidates,
    deduping on (type, url) so a re-run never doubles a source."""
    by_addr = {c.address.lower(): c for c in candidates}
    for addr, s in merges:
        c = by_addr.get(addr)
        if c is None:
            continue
        if (s.type, s.url) not in {(x.type, x.url) for x in c.sources}:
            c.sources.append(s)


def _rel_me_collect(
    person: Person,
) -> tuple[list, RunRecord, list[tuple[str, str]]]:
    """Run the rel=me / Bluesky identity sensor (E2) over the personal domains.
    A bidirectional rel="me" is self-attested identity binding (the IndieAuth
    model) — asserted ones become bound_anchors the host can weigh (it binds
    domain↔profile; the host still judges it's the target). Returns (links,
    RunRecord, anchors-to-add). Does NOT mutate person.bound_anchors itself — the
    caller commits the anchors so an abandoned slow probe leaks nothing.
    Best-effort; never raises."""
    links: list = []
    anchors: list[tuple[str, str]] = []
    t0 = time.monotonic()
    for dom in person.personal_domains[:3]:
        try:
            dom_links = verify_rel_me(dom)
        except Exception:  # noqa: BLE001 — sensor must never sink the run
            continue
        links.extend(dom_links)
        # A bidirectional link proves THIS domain belongs to the target — record a
        # domain-bearing anchor (distinct from the profile-side rel_me_verified
        # anchor below) so the ENG-8 binding gate knows which personal domain is
        # owned, not just that some profile cross-linked.
        if any(getattr(link, "bidirectional", False) for link in dom_links):
            anchor = ("personal_domain_verified", dom.lower())
            if anchor not in anchors:
                anchors.append(anchor)
    for link in links:
        if getattr(link, "bidirectional", False):
            anchor = ("rel_me_verified", link.url)
            if anchor not in anchors:
                anchors.append(anchor)
    rec = RunRecord(sensor="rel_me", status="ran",
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    outcome=f"{len(links)} link(s)")
    return links, rec, anchors


def _run_slow_probes(
    person: Person, candidates: list[EmailCandidate], run_records: list[RunRecord],
    *, budget_sec: float, no_pgp: bool, floor_sec: float = _PER_SENSOR_FLOOR_SEC,
) -> list:
    """Run the rel=me (E2) and PGP (E3) network probes under the SHARED wall-clock
    deadline. A daemon thread does the fetches and ACCUMULATES its results; if it
    finishes within the budget we commit them (rel=me anchors, pgp source merges),
    if it is abandoned at the deadline we commit nothing — the straggler keeps
    running against its own returns, never against person/candidates, so the
    bundle can't race a partial mutation — and append a deadline-exceeded
    RunRecord. Returns the rel=me links (empty when nothing ran or it was
    abandoned). This keeps the documented ≤deadline promise over the slow probes,
    which previously ran unbounded before phase2_budget was even measured."""
    want_rel_me = bool(person.personal_domains)
    want_pgp = bool(candidates) and not no_pgp
    if not (want_rel_me or want_pgp):
        return []

    box: dict = {}
    done = threading.Event()

    def _worker() -> None:
        try:
            if want_rel_me:
                box["rel_me"] = _rel_me_collect(person)
            if want_pgp:
                box["pgp"] = _pgp_corroborate(candidates)
        finally:
            done.set()

    t0 = time.monotonic()
    th = threading.Thread(target=_worker, name="snoop:slow-probes", daemon=True)
    th.start()
    budget = max(budget_sec, floor_sec)
    th.join(timeout=budget)
    if not done.is_set():
        run_records.append(RunRecord(
            sensor="rel_me/pgp", status="degraded",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            outcome="timeout",
            reason=(f"deadline-exceeded: slow probes abandoned after "
                    f"{budget:g}s shared budget")))
        return []

    rel_me_links: list = []
    if "rel_me" in box:
        rel_me_links, rel_me_rec, rel_me_anchors = box["rel_me"]
        for anchor in rel_me_anchors:
            if anchor not in person.bound_anchors:
                person.bound_anchors.append(anchor)
        run_records.append(rel_me_rec)
    if "pgp" in box:
        pgp_rec, pgp_merges = box["pgp"]
        _apply_pgp_merges(candidates, pgp_merges)
        run_records.append(pgp_rec)
    return rel_me_links


def _rel_me_observations(links: list) -> list[reason.Observation]:
    """Shape rel=me links into observations the host reasons over (ids assigned
    by the bundle builder)."""
    obs: list[reason.Observation] = []
    for link in links:
        verdict = ("bidirectional (self-attested both ways)"
                   if getattr(link, "bidirectional", False)
                   else "one-way (site→profile only)")
        content = f"rel=me {link.platform}: {link.url} — {verdict}"
        if getattr(link, "detail", ""):
            content += f"; {link.detail}"
        obs.append(reason.Observation(id="", type="rel_me", content=content,
                                      source_url=link.url))
    return obs


def _mx_class(candidates: list[EmailCandidate]) -> str:
    """Coarse MX class for the ledger — no domains, just the bucket."""
    provs = {c.mx_provider for c in candidates if c.mx_provider}
    if "google" in provs:
        return "google"
    if "microsoft" in provs:
        return "microsoft"
    if provs:
        return "other"
    return "none"


def _write_ledger(person: Person, candidates: list[EmailCandidate],
                  packages: list[dict], run_records: list[RunRecord]) -> None:
    """Append this run's yield metadata to the local ledger (E1), best-effort.
    Plan SHAPE (booleans) + MX class + per-sensor RunRecords only — never target
    data. A write failure is a one-line stderr warning, nothing else."""
    rec = build_record(
        plan_shape={
            "github": bool(person.handles.get("github")),
            "hn": bool(person.handles.get("hn")),
            "personal_domains": bool(person.personal_domains),
            "packages": bool(packages),
            "employer": bool(person.employer and person.employer.name),
        },
        mx_class=_mx_class(candidates),
        candidates=len(candidates),
        sensors=[r.to_dict() for r in run_records],
    )
    if not append_run(rec):
        sys.stderr.write("note: could not write the local ledger (~/.snoop/ledger.jsonl)\n")


def _run_summary(run_records: list[RunRecord]) -> str:
    """One-line human run summary (8A) — rendered to stderr so the bundle on
    stdout stays clean. e.g. 'sensors: git_emails ran 120ms · personal_site
    skipped (no personal_domains) · pattern_gen ran 5ms'."""
    parts: list[str] = []
    for r in run_records:
        if r.status == "ran":
            ms = f" {r.elapsed_ms}ms" if r.elapsed_ms is not None else ""
            parts.append(f"{r.sensor} ran{ms}")
        elif r.status == "degraded":
            parts.append(f"{r.sensor} degraded ({r.reason or r.outcome})")
        else:
            parts.append(f"{r.sensor} skipped ({r.reason})")
    return "sensors: " + " · ".join(parts)


def cluster_candidates(results: list[ResolverResult]) -> list[EmailCandidate]:
    """Merge candidates across resolvers by lowercased address.

    Source lists are concatenated; later-arriving sources are appended.
    Per Codex finding #8 (source independence over-counted), we DO NOT
    cluster by family in v1 — multiple appearances of the same address
    in profile README + personal_site /about may both reflect the same
    contact-info block. That's a v2 problem.
    """
    by_addr: dict[str, EmailCandidate] = {}
    for r in results:
        for c in r.candidates:
            addr_key = c.address.lower()
            if addr_key not in by_addr:
                # Canonicalize to the lowercase form for downstream rendering
                # and copy-paste. Without this, a candidate first seen as
                # 'Pete@OpenAI.COM' would surface in the decision card verbatim
                # while the SMTP probe internally lowercased it — verdict and
                # displayed address disagree by case.
                c.address = addr_key
                by_addr[addr_key] = c
            else:
                merged = by_addr[addr_key]
                # Merge sources, dropping exact duplicates by (type, url)
                existing_keys = {(s.type, s.url) for s in merged.sources}
                for s in c.sources:
                    if (s.type, s.url) not in existing_keys:
                        merged.sources.append(s)
                # Combine domain-level flags (or them together)
                merged.employer_match = merged.employer_match or c.employer_match
                merged.employer_former_match = (
                    merged.employer_former_match or c.employer_former_match
                )
                merged.is_personal_provider = (
                    merged.is_personal_provider or c.is_personal_provider
                )
                # Verification-layer fields: today no pre-cluster resolver sets
                # these, but defense-in-depth — if a future cached/manual_known
                # resolver returns candidates with prior verdicts, don't silently
                # drop them. First-seen-non-default wins; explicit verdicts are
                # never overwritten by "unprobed".
                if merged.smtp_verdict == "unprobed" and c.smtp_verdict != "unprobed":
                    merged.smtp_verdict = c.smtp_verdict
                if merged.account_exists == "unprobed" and c.account_exists != "unprobed":
                    merged.account_exists = c.account_exists
                if merged.mx_provider is None and c.mx_provider is not None:
                    merged.mx_provider = c.mx_provider
                if (merged.account_display_name is None
                        and c.account_display_name is not None):
                    merged.account_display_name = c.account_display_name
                if (merged.account_photo_url is None
                        and c.account_photo_url is not None):
                    merged.account_photo_url = c.account_photo_url
    return list(by_addr.values())


_GOOGLE_NATIVE_DOMAIN = "google.com"


def _google_target_domains(workspace_domains: list[str]) -> set[str]:
    """Domains worth probing via the Google People API. Always includes the
    literal google.com; user can add Workspace tenant domains explicitly."""
    domains = {_GOOGLE_NATIVE_DOMAIN}
    for d in workspace_domains or []:
        if isinstance(d, str) and d.strip():
            domains.add(d.strip().lower())
    return domains


def _autodetect_workspace_domains(
    candidates: list[EmailCandidate],
    explicit: list[str],
    *,
    is_google_hosted_fn: Callable[[str], bool] = is_google_hosted,
) -> list[str]:
    """Find candidate domains whose MX is Google Workspace and add them to
    the explicit list. Skips google.com (already included) and personal
    providers (gmail.com etc. — these ARE Google MX but probing arbitrary
    personal addresses is invasive and wrong-scope for this tool).

    One DNS lookup per unique non-skip candidate domain. Returns the merged
    list; the caller threads it to _google_target_domains.

    Function injection on is_google_hosted_fn lets tests stub the MX
    lookup without monkeypatching the verify_smtp module globally.
    """
    explicit_set = {d.strip().lower() for d in explicit or [] if isinstance(d, str)}
    seen_candidate_domains: set[str] = set()
    additions: list[str] = []
    for c in candidates:
        if "@" not in c.address:
            continue
        d = c.address.rsplit("@", 1)[1].lower()
        if d in seen_candidate_domains or d in explicit_set or d == _GOOGLE_NATIVE_DOMAIN:
            continue
        seen_candidate_domains.add(d)
        if is_personal_provider(d):
            continue
        if is_google_hosted_fn(d):
            additions.append(d)
    return list(explicit or []) + additions


def _probe_rank(c: EmailCandidate) -> tuple:
    """Tiebreak ORDERING within the bound set (ENG-8): rank by whether the address
    was actually observed (any non-pattern source) over a pure name×domain guess,
    then by how many sources corroborate it, then address for determinism. Since
    the ENG-8 gate now decides WHICH candidates are eligible to probe at all, this
    only sequences the survivors so an observed address is tried first; it is no
    longer a ranking that could let an unbound guess be probed."""
    observed = any(s.type != "pattern" for s in c.sources)
    return (0 if observed else 1, -len(c.sources), c.address)


# ENG-8 Phase-1 — candidate binding (identity binds before deliverability spends).
# Surfaces whose identity is the target's GitHub account: an address observed on
# one of these is an anchored observation ONLY when the handle itself bound.
_GITHUB_SURFACES = frozenset({"git_commit", "gh_profile", "gh_readme", "github_repo"})
# Surfaces tied to a personal domain the target owns.
_DOMAIN_SURFACES = frozenset({"personal_site", "whois"})


def _github_identity_bound(person: Person) -> bool:
    """True when a VALIDATING github anchor bound the handle (name/employer/
    personal-domain match) — not merely that the handle exists. A bare
    `github_handle_exists` is an untrusted hint and does not anchor an address."""
    return any(t.startswith("github") and t != "github_handle_exists"
               for t, _ in person.bound_anchors)


def _verified_personal_domains(person: Person) -> set[str]:
    """Personal domains proven to belong to the target by a bidirectional rel=me
    (the IndieAuth self-attestation). This is the rel=me identity signal."""
    return {str(v).lower() for t, v in person.bound_anchors
            if t == "personal_domain_verified"}


def _anchored_surface_domains(person: Person) -> set[str]:
    """Personal domains trusted enough that a reading observed ON them is an
    anchored observation — rel=me-verified domains plus a github blog/domain
    that matched a declared personal domain."""
    doms = _verified_personal_domains(person)
    doms |= {str(v).lower() for t, v in person.bound_anchors
             if t == "github_personal_domain_match"}
    return doms


def _bind_context(person: Person) -> tuple[set[str], set[str], bool]:
    """The three person-level invariants _candidate_is_bound reads — the rel=me
    domains, the anchored-surface domains, and whether the GitHub identity is
    bound. Computed once and reused across a batch of candidates (each scans
    person.bound_anchors, which doesn't change per candidate)."""
    return (_verified_personal_domains(person),
            _anchored_surface_domains(person),
            _github_identity_bound(person))


def _candidate_is_bound(c: EmailCandidate, person: Person,
                        *, ctx: tuple[set[str], set[str], bool] | None = None) -> bool:
    """ENG-8 Phase-1: does THIS ADDRESS belong to the target? (Distinct from
    Person.bound_anchors, which only says 'we found the right person at all.')

    A candidate binds when ≥2 INDEPENDENT evidence classes agree on it:
      1. anchored observation — a real (non-pattern) source on a surface whose
         identity is bound: a GitHub surface when the handle bound, or a
         personal_site/whois reading on a bound personal domain;
      2. employer_match — the address domain is the resolved current employer's;
      3. rel=me ownership — the address domain is a bidirectionally-verified
         personal domain;
      4. PGP owner-UID — keys.openpgp.org returned a key whose UID is this address.

    A `manual_known` source (the --verify / --known lane: the user supplied the
    address AS the subject) short-circuits to bound. Binding requires ≥2 signals
    AND at least one IDENTITY-BEARING signal — an anchored observation (1) or
    rel=me ownership (3) — because those alone tie the address to THIS person.
    employer_match (2) and PGP owner-UID (4) are CORROBORATING but target-
    agnostic: a domain belongs to the employer, a key proves someone controls
    the inbox — neither says it is the target. So two corroborating signals
    (employer + PGP) never bind, and snoop will not open a socket to a possible
    namesake's mailbox on the strength of a name×domain template that merely
    landed on the employer domain and carried a published key.
    """
    if "@" not in c.address:
        return False
    source_types = {s.type for s in c.sources}
    if "manual_known" in source_types:
        return True
    domain = c.address.rsplit("@", 1)[1].lower()
    # Person-level invariants don't vary across candidates; when binding a whole
    # batch the caller hoists them once via _bind_context and passes them in so
    # they aren't rescanned per candidate.
    rel_me_domains, surface_domains, github_bound = ctx or _bind_context(person)

    on_github_surface = github_bound and bool(source_types & _GITHUB_SURFACES)
    on_owned_domain_surface = (
        bool(source_types & _DOMAIN_SURFACES) and domain in surface_domains
    )
    anchored = on_github_surface or on_owned_domain_surface  # 1. anchored obs
    rel_me_owned = domain in rel_me_domains                  # 3. rel=me ownership
    identity_bearing = anchored or rel_me_owned

    signals = 0
    if anchored:
        signals += 1
    if c.employer_match:
        signals += 1                                  # 2. employer (corroborating)
    if rel_me_owned:
        signals += 1
    if "pgp" in source_types:
        signals += 1                                  # 4. PGP UID (corroborating)
    return signals >= 2 and identity_bearing


def _google_account_candidates(
    candidates: list[EmailCandidate],
    workspace_domains: list[str],
) -> list[EmailCandidate]:
    """Filter candidates to those on Google-hosted domains worth probing.
    Skip candidates that already have an account_exists verdict (don't
    re-probe within one invocation). Ordered by _probe_rank so observation-
    backed candidates probe first — on a multi-user Workspace tenant a pattern
    guess that hits someone else's real account shouldn't short-circuit probing
    of an observed address that hasn't been tried yet.
    """
    domains = _google_target_domains(workspace_domains)
    out: list[EmailCandidate] = []
    for c in candidates:
        if not c.address or "@" not in c.address:
            continue
        if c.account_exists != "unprobed":
            continue
        domain = c.address.rsplit("@", 1)[1].lower()
        if domain in domains:
            out.append(c)
    out.sort(key=_probe_rank)
    return out


# ENG-9 — split the Phase-2 gate by probe class. SMTP opens a socket to the
# target's mailbox and can't disambiguate on a catch-all Workspace domain, so it
# stays bind-gated (ENG-8). The Google People API existence check is categorically
# different: it's an authed call through the user's OWN cookies (no socket to the
# mailbox) and is the ONLY disambiguator when SMTP returns catch_all — its whole
# value is collapsing pattern guesses to the one account that exists. So it MAY
# run on UNBOUND pattern candidates on a Google-hosted domain. A verified hit then
# becomes deliverability signal the host reasons over; a verified+name_match hit is
# promoted to identity binding by _reassess_identity. Cost is bounded by the
# per-domain daily ProbeBudget plus this cap (a name-variant blowup can't spend the
# whole budget on one target); on an unlocked tenant the name-match short-circuit
# in fetch_google_account stops it early.
_SPECULATIVE_GOOGLE_CAP = 12
_TEMPLATE_RANK = {t: i for i, t in enumerate(_DEFAULT_TEMPLATE_ORDER)}


def _primary_localparts(name: str) -> set[str] | None:
    """The local-parts for the PRIMARY name parse (e.g. 'jibben', 'jhillen',
    'j.hillen' for 'Jibben Hillen'). The speculative Google burst is restricted to
    these: the reversed-order guesses (last-as-first, 'hillen@') roughly double the
    probe count, are almost always noise for a Western name, and can hit unrelated
    employees — dropping them keeps the burst small enough to fit the Phase-2
    deadline and cheap against the daily budget. Returns None when the name can't be
    parsed (then no restriction is applied). The bound path still covers every name
    variant; this trims only the speculative fan-out."""
    parsed = parse_name(name) if name else None
    if not parsed:
        return None
    return {lp.lower() for lp in localpart_templates(parsed.first, parsed.last).values()}


def _pattern_template(c: EmailCandidate) -> str | None:
    """Extract the pattern template name (e.g. 'first', 'flast') from a candidate's
    pattern Source detail, so the speculative set can be ranked by template
    plausibility rather than alphabetically — `first@` for a rare first name must
    stay reachable within the cap, even though `first` is a low-popularity template."""
    for s in c.sources:
        m = re.search(r"(?:template|pattern) '([^']+)'", s.detail or "")
        if m:
            return m.group(1)
    return None


def _speculative_rank(c: EmailCandidate) -> tuple:
    """Order the unbound Google probe set: company-inferred winners first, then by
    template popularity, then address for determinism. Ensures the cap keeps the
    plausible patterns rather than slicing by alphabet."""
    detail = " ".join(s.detail or "" for s in c.sources)
    inferred = 0 if "matches company pattern" in detail else 1
    tmpl = _pattern_template(c)
    rank = _TEMPLATE_RANK[tmpl] if tmpl in _TEMPLATE_RANK else len(_DEFAULT_TEMPLATE_ORDER)
    return (inferred, rank, c.address)


def _speculative_google_candidates(
    candidates: list[EmailCandidate],
    bound_addrs: set[str],
    workspace_domains: list[str],
    person: Person,
) -> list[EmailCandidate]:
    """ENG-9: unbound candidates on a Google-hosted domain, eligible for the Google
    existence check (but NOT SMTP). Excludes already-bound addresses (those probe
    via the bound path), anything already carrying an account_exists verdict, and —
    to keep the burst within the Phase-2 deadline — the reversed-order name guesses
    (only the primary name parse's local-parts probe speculatively). Ordered by
    template plausibility, capped at _SPECULATIVE_GOOGLE_CAP."""
    domains = _google_target_domains(workspace_domains)
    allowed_localparts = _primary_localparts(person.name)
    out: list[EmailCandidate] = []
    seen: set[str] = set()
    for c in candidates:
        if not c.address or "@" not in c.address:
            continue
        if c.address in bound_addrs or c.address in seen:
            continue
        if c.account_exists != "unprobed":
            continue
        local, _, domain = c.address.partition("@")
        domain = domain.lower()
        if domain not in domains:
            continue
        if allowed_localparts is not None and local.lower() not in allowed_localparts:
            continue
        seen.add(c.address)
        out.append(c)
    out.sort(key=_speculative_rank)
    return out[:_SPECULATIVE_GOOGLE_CAP]


def _smtp_candidates(candidates: list[EmailCandidate], top_k: int = 5) -> list[EmailCandidate]:
    """Pick the top candidates worth SMTP-probing: non-personal-provider, at
    least one source, not already known-dead via Google. Ordered by _probe_rank
    (observed addresses before pure guesses).

    Google's 'not_found' verdict is authoritative — re-probing those addresses
    over SMTP burns the per-domain daily budget and risks the user's MAIL FROM
    getting rate-limited on a mailbox we already know doesn't exist.
    """
    eligible = []
    for c in candidates:
        if "@" not in c.address:
            continue
        domain = c.address.rsplit("@", 1)[1].lower()
        if is_personal_provider(domain):
            continue
        if not c.sources:
            continue
        if c.account_exists == "not_found":
            continue
        eligible.append(c)
    eligible.sort(key=_probe_rank)
    return eligible[:top_k]


# ---- main -------------------------------------------------------------------


def _work_search_observations(plan: dict[str, Any]) -> list[reason.Observation]:
    """Shape host-model WebSearch results into raw web_search observations for the
    bundle. Untrusted: each is just text the host model weighs, and grounding
    keeps it honest. Non-string fields are coerced/dropped."""
    raw = plan.get("work_search_results")
    if not isinstance(raw, list):
        return []
    obs: list[reason.Observation] = []
    for i, r in enumerate(raw, start=1):
        if not isinstance(r, dict):
            continue
        title = r.get("title") if isinstance(r.get("title"), str) else ""
        summary = r.get("summary") if isinstance(r.get("summary"), str) else ""
        url = r.get("url") if isinstance(r.get("url"), str) else None
        crosslink = r.get("crosslink_url") if isinstance(r.get("crosslink_url"), str) else ""
        content = " ".join(p for p in (
            f"web-search result: {title}".strip(),
            summary,
            f"(page cross-links to {crosslink})" if crosslink else "",
        ) if p).strip()
        if not content:
            continue
        obs.append(reason.Observation(
            id=f"w{i}", type="web_search", content=content, source_url=url,
        ))
    return obs


def _format_reasoned_json(profile: reason.ReasonedProfile, *,
                          warnings: list[str] | None = None) -> str:
    """Machine-readable form of the LLM-native profile. Every fact carries its
    citations, confidence, and the deterministic `verified` flag."""
    return json.dumps({
        "warnings": warnings or [],
        "person": {"name": profile.identity.name,
                   "ambiguity": profile.identity.ambiguity},
        "summary": profile.summary,
        "identity_confidence": profile.identity_confidence,
        "facts": [
            {"kind": f.kind, "label": f.label, "value": f.value, "detail": f.detail,
             "confidence": f.confidence, "verified": f.verified,
             "evidence_ids": f.evidence_ids, "reasoning": f.reasoning,
             # The analyst's contract fields, preserved through --ground (only
             # emitted when present, so a fact without them stays clean).
             **({"verdict": f.verdict} if f.verdict is not None else {}),
             **({"marker": f.marker} if f.marker is not None else {})}
            for f in profile.facts
        ],
        "usage": profile.usage,
    }, indent=2, default=str)


def _resolution_gaps(person: Person) -> list[str]:
    """Coaching tips for high-value resolution inputs the host didn't supply.

    snoop finds only what the host feeds it, so a thin plan leaves the sensors
    pattern-guessing — and nearly every weak result traces to an under-resolved
    Step 1. Rather than rely on the host remembering to resolve richly, surface
    the gaps in the bundle so it can do another resolution pass and re-run.
    Empty when the plan is already rich."""
    gaps: list[str] = []
    if not person.personal_domains:
        gaps.append(
            "no personal_domains — the personal_site mailto sensor did not run. "
            "Finding their site is the highest-yield discovery step (often a "
            "direct mailbox + a strong identity anchor); resolve it and re-run."
        )
    if not person.handles:
        gaps.append(
            "no handles (github/hn) — the git/profile/HN sensors did not run, so "
            "discovery is name×domain pattern-guessing only. Resolve their "
            "GitHub/HN/socials and re-run."
        )
    if person.employer and person.employer.name and not person.employer.source_url:
        gaps.append(
            "employer is plan-declared only (no source_url) — role/employer facts "
            "will be uncited. Set employer.source_url to where you confirmed it."
        )
    return gaps


def _build_bundle(person: Person, candidates: list[EmailCandidate],
                  plan: dict[str, Any], warnings: list[str],
                  *, resolution_gaps: list[str] | None = None,
                  run_records: list[RunRecord] | None = None,
                  rel_me_links: list | None = None) -> dict[str, Any]:
    """Build the raw observation bundle the host model reasons over.

    snoop's irreducible job is the I/O the host can't do (git/GitHub/SMTP/Google/
    MX); this dumps what those sensors saw, typed and cited, plus any host-model
    web-search observations and rel=me identity cross-links. No binding, no
    rendering — the host is the analyst. `resolution_gaps` (when present) coaches
    a richer Step-1 resolution + re-run; `run_records` stamp per-sensor status +
    elapsed_ms into the bundle (E6)."""
    observations = reason.build_evidence(person, candidates)
    extra = _work_search_observations(plan) + _rel_me_observations(rel_me_links or [])
    for i, o in enumerate(extra, start=len(observations) + 1):
        observations.append(reason.Observation(
            id=f"o{i}", type=o.type, content=o.content, source_url=o.source_url,
        ))
    def _obs_dict(o: reason.Observation) -> dict[str, Any]:
        d: dict[str, Any] = {"id": o.id, "type": o.type, "content": o.content,
                             "source_url": o.source_url}
        if o.data is not None:
            d["data"] = o.data  # structured mirror (email_candidate fields, etc.)
        return d

    bundle: dict[str, Any] = {
        "schema": BUNDLE_SCHEMA_VERSION,
        "warnings": warnings or [],
        "person": {"name": person.name, "ambiguity": person.ambiguity},
        "observations": [_obs_dict(o) for o in observations],
    }
    if run_records:
        # Per-sensor timing + status (E6) so the host can reason about what its
        # sensors did and how long they took.
        bundle["sensors"] = [r.to_dict() for r in run_records]
    if resolution_gaps:
        # A thin plan: surface what a richer resolution pass would add. Placed
        # near the top so the host sees it before reasoning over a weak bundle.
        bundle["resolution_gaps"] = resolution_gaps
    return bundle


def _emit_observations(person: Person, candidates: list[EmailCandidate],
                       plan: dict[str, Any], warnings: list[str],
                       *, out_path: str | None = None,
                       resolution_gaps: list[str] | None = None,
                       run_records: list[RunRecord] | None = None,
                       rel_me_links: list | None = None) -> None:
    """Write the observation bundle to stdout, or to `out_path` (with a printed
    pointer + the ready-to-run --ground command) when --out is given."""
    bundle = _build_bundle(person, candidates, plan, warnings,
                           resolution_gaps=resolution_gaps,
                           run_records=run_records,
                           rel_me_links=rel_me_links)
    text = json.dumps(bundle, indent=2, default=str)
    if not out_path:
        sys.stdout.write(text)
        return
    path = Path(out_path).expanduser()
    try:
        path.write_text(text)
    except OSError as exc:
        # Don't lose the bundle on a bad path — fall back to stdout with a note.
        sys.stderr.write(f"--out: cannot write {path}: {exc}; emitting to stdout\n")
        sys.stdout.write(text)
        return
    n = len(bundle["observations"])
    sys.stdout.write(
        f"Wrote {n} observation(s) to {path}.\n"
        f"Reason over them, then ground your facts with:\n"
        f'  echo \'{{"person": …, "summary": …, "facts": […]}}\' | '
        f'python3 "{Path(__file__).resolve()}" --ground --observations-file "{path}"\n'
    )


def _run_ground(observations_file: str | None = None) -> int:
    """Verifier mode: read {observations, facts, summary, ...} on stdin, drop
    facts whose citations don't reference a real observation, render the card.
    Pure + offline — the deterministic check the host model can't self-certify.

    When `observations_file` is given (the --out bundle), observations load from
    it and stdin only needs {person, summary, facts}."""
    from lib.ground import ground

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"--ground: invalid JSON on stdin: {exc}\n")
        return 2

    # In --observations-file mode the FILE is the authoritative observation set
    # (as written by --out); stdin supplies only {person, summary, facts}. Without
    # a file, the whole bundle (incl. observations) arrives on stdin. The two are
    # NEVER merged: merging would let stdin inject or shadow an observation id that
    # ground() then accepts, defeating the guarantee that a fact can only cite
    # evidence the sensors actually produced.
    obs_dicts: list[dict] = []
    if observations_file:
        try:
            file_bundle = json.loads(Path(observations_file).expanduser().read_text())
        except (OSError, json.JSONDecodeError) as exc:
            sys.stderr.write(f"--observations-file: cannot read {observations_file}: {exc}\n")
            return 2
        # Schema gate: reject a stale bundle written by an older --observations.
        ver = file_bundle.get("schema")
        if ver != BUNDLE_SCHEMA_VERSION:
            sys.stderr.write(
                f"--observations-file: bundle is schema {ver!r} (expected "
                f"{BUNDLE_SCHEMA_VERSION}); re-run --observations to regenerate it.\n"
            )
            return 2
        obs_dicts.extend(o for o in file_bundle.get("observations", []) if isinstance(o, dict))
        # Let the file supply person/warnings if stdin didn't.
        if not isinstance(payload.get("person"), dict) and isinstance(file_bundle.get("person"), dict):
            payload["person"] = file_bundle["person"]
        if "warnings" not in payload and "warnings" in file_bundle:
            payload["warnings"] = file_bundle["warnings"]
        # Carry the per-sensor run records so a zero-fact card can render the 4A
        # honest blank (checked / not-checked / why) from what the sensors did.
        if "sensors" not in payload and isinstance(file_bundle.get("sensors"), list):
            payload["sensors"] = file_bundle["sensors"]
    else:
        # Pure-stdin bundle. If it is a FULL bundle (carries a schema version),
        # gate it exactly like a file bundle so a stale v1 can't ground silently
        # just because it was piped instead of passed as --observations-file.
        stdin_schema = payload.get("schema")
        if stdin_schema is not None and stdin_schema != BUNDLE_SCHEMA_VERSION:
            sys.stderr.write(
                f"--ground: bundle is schema {stdin_schema!r} (expected "
                f"{BUNDLE_SCHEMA_VERSION}); re-run --observations to regenerate it.\n"
            )
            return 2
        obs_dicts.extend(o for o in payload.get("observations", []) if isinstance(o, dict))

    observations = [
        reason.Observation(
            id=str(o.get("id", "")), type=str(o.get("type", "")),
            content=str(o.get("content", "")), source_url=o.get("source_url"),
            data=o.get("data") if isinstance(o.get("data"), dict) else None,
        )
        for o in obs_dicts
    ]
    facts = [f for f in payload.get("facts", []) if isinstance(f, dict)]
    grounded = ground(facts, observations)

    p = payload.get("person", {}) if isinstance(payload.get("person"), dict) else {}
    identity = Person(
        name=str(p.get("name", "(unknown)")),
        ambiguity=p.get("ambiguity", "insufficient_identity_evidence"),
    )
    rp = reason.ReasonedProfile(
        identity=identity,
        summary=str(payload.get("summary", "")),
        facts=grounded,
        identity_confidence=payload.get("identity_confidence"),
        observations=observations,
    )
    sensors = payload.get("sensors") if isinstance(payload.get("sensors"), list) else None
    if payload.get("json"):
        sys.stdout.write(_format_reasoned_json(rp, warnings=payload.get("warnings")))
    else:
        sys.stdout.write(render.render_reasoned_card(
            rp, warnings=payload.get("warnings"), sensors=sensors))
    return 0


def _run_phase2_probes(
    person: Person, google_set: list[EmailCandidate],
    smtp_set: list[EmailCandidate], args: argparse.Namespace,
    *, merged_workspace: list[str], google_ready: bool, notes: list[str],
) -> None:
    """The actual Phase-2 work, mutating the passed candidates in place. ENG-9: the
    two probes have DIFFERENT eligibility. The Google existence check runs over
    `google_set` (the bound candidates PLUS unbound pattern guesses on a Workspace
    domain) — an authed API call, the only disambiguator on a catch-all tenant. SMTP
    runs over `smtp_set` (bound candidates only) — it opens a socket, so it never
    touches an unbound namesake's mailbox. Google runs first so a not_found verdict
    short-circuits the SMTP probe on a dead candidate. Non-ok google outcomes go to
    the passed `notes` sink (NOT person.notes directly), so a straggler abandoned at
    the deadline can't leak its notes into the bundle."""
    if google_set and args.allow_google_account and google_ready:
        google_targets = _google_account_candidates(google_set, merged_workspace)
        if google_targets:
            google_result = fetch_google_account(
                google_targets,
                target_domains=_google_target_domains(merged_workspace),
                target_name=person.name,
                budget=ProbeBudget(
                    per_domain=30,
                    state_path=Path.home() / ".snoop" / "google-budget.json",
                ),
            )
            # Surface non-ok resolver outcomes (rate limit, cookies missing,
            # auth failure, all-probes-errored) so the host can tell 'didn't
            # find' apart from 'didn't ask'. Silent swallowing lets a stale
            # browser session masquerade as a clean lookup.
            if (google_result.status in ("error", "unavailable", "empty")
                    and google_result.error_detail):
                notes.append(
                    f"google_account {google_result.status}: {google_result.error_detail}"
                )

    # SMTP probe the bound candidates (top-K within the bound set, _probe_rank
    # ordering). Unbound candidates are never SMTP-probed (ENG-8).
    if not args.no_smtp:
        smtp_targets = _smtp_candidates(smtp_set)
        if smtp_targets:
            verify_candidates(smtp_targets, budget=default_budget())


def _merge_probe_verdicts(real: EmailCandidate, staged: EmailCandidate) -> None:
    """Copy the Phase-2 probe outputs from a staged copy back onto the real
    candidate: SMTP + Google verdict fields, plus any new google_account Source
    (deduped by (type, url))."""
    real.smtp_verdict = staged.smtp_verdict
    real.account_exists = staged.account_exists
    real.account_display_name = staged.account_display_name
    real.account_photo_url = staged.account_photo_url
    if staged.mx_provider:
        real.mx_provider = staged.mx_provider
    existing = {(s.type, s.url) for s in real.sources}
    for s in staged.sources:
        if (s.type, s.url) not in existing:
            real.sources.append(s)


def _probe_candidates(
    person: Person, candidates: list[EmailCandidate], args: argparse.Namespace,
    *, google_ready: bool, deadline_sec: float = _DEFAULT_DEADLINE_SEC,
) -> RunRecord | None:
    """Phase-2 deliverability probing, wrapped in the shared wall-clock deadline.

    ENG-8: SMTP fires ONLY on candidates that bound to the target in Phase 1
    (_candidate_is_bound) — snoop opens no socket to a namesake's (or a pure
    pattern guess's) mailbox. ENG-9: the Google existence check ALSO runs on
    unbound pattern guesses that sit on a Google-hosted domain (an authed API call,
    not a socket, and the only disambiguator on a catch-all Workspace tenant). When
    nothing binds AND no candidate is on a Workspace domain, this is a no-op and the
    card renders the honest blank.

    ENG-5/E6: the probes run in a daemon thread over deep COPIES of the probe set;
    their verdicts are merged onto the real candidates ONLY if the thread finishes
    before the deadline. A straggler abandoned at the deadline keeps running against
    its own copies, which we discard — its verdicts are never merged (the generation
    guarantee), and the probe phase reports a `deadline-exceeded` degradation.
    Returns that degraded RunRecord on abandonment, else None."""
    _ctx = _bind_context(person)  # hoist the person-level invariants once
    bound = [c for c in candidates if _candidate_is_bound(c, person, ctx=_ctx)]
    bound_addrs = {c.address for c in bound}

    # ENG-9 speculative set: unbound Workspace candidates eligible for the Google
    # existence check only. Autodetect Workspace MX from ALL candidates (not just
    # bound) so a target with nothing bound still gets its employer tenant probed.
    speculative: list[EmailCandidate] = []
    merged_workspace: list[str] = []
    if args.allow_google_account and google_ready:
        merged_workspace = _autodetect_workspace_domains(
            candidates, args.google_workspace_domain,
        )
        speculative = _speculative_google_candidates(
            candidates, bound_addrs, merged_workspace, person,
        )

    probe_targets = bound + speculative
    if not probe_targets:
        return None

    staged = [copy.deepcopy(c) for c in probe_targets]
    staged_google = staged
    staged_smtp = [s for s in staged if s.address in bound_addrs]
    staged_notes: list[str] = []
    done = threading.Event()

    def worker() -> None:
        try:
            _run_phase2_probes(person, staged_google, staged_smtp, args,
                               merged_workspace=merged_workspace,
                               google_ready=google_ready, notes=staged_notes)
        finally:
            done.set()

    t0 = time.monotonic()
    th = threading.Thread(target=worker, name="snoop:phase2", daemon=True)
    th.start()
    budget = max(deadline_sec, _PER_SENSOR_FLOOR_SEC)
    th.join(timeout=budget)
    if not done.is_set():
        # Deadline exceeded — abandon the straggler, merge nothing.
        return RunRecord(
            sensor="probe", status="degraded",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            outcome="timeout",
            reason=f"deadline-exceeded: probe phase abandoned after {budget:g}s shared budget",
        )
    by_addr = {c.address: c for c in probe_targets}
    for s in staged:
        c = by_addr.get(s.address)
        if c is not None:
            _merge_probe_verdicts(c, s)
    person.notes.extend(staged_notes)
    return None


def _reassess_identity(person: Person, candidates: list[EmailCandidate]) -> None:
    """Promote identity confidence using the probe verdicts.

    person_resolve runs BEFORE the Google/SMTP probes and only knows how to bind
    identity from a validated GitHub handle — so without a handle it defaults to
    `insufficient_identity_evidence` and never sees the strongest identity signal
    snoop can produce: a Google account that is `verified` AND whose display name
    matches the target. That is genuine identity binding (existence + name), so
    when exactly one verified candidate name-matches, promote to
    `single_plausible_match` and record the anchor. Only acts on the
    not-yet-bound state; a declared `multiple_plausible_matches` (real namesake)
    is never auto-promoted."""
    if person.ambiguity != "insufficient_identity_evidence" or not person.name:
        return
    name_matched = [
        c for c in candidates
        if c.account_exists == "verified" and c.account_display_name
        and name_match(c.account_display_name, person.name)
    ]
    if len(name_matched) == 1:
        c = name_matched[0]
        person.ambiguity = "single_plausible_match"
        person.bound_anchors.append(("google_name_match", str(c.account_display_name)))
        person.notes.append(
            f"identity promoted to single_plausible_match: Google account "
            f"{c.address} is verified with display name "
            f"'{c.account_display_name}' matching the target"
        )


def _google_ready(capabilities: list[Capability]) -> bool:
    """True when the capability probe found a usable Google session. Lets the
    caller skip the probe (and its failed-cookie-read attempt) when none exists,
    so passing --allow-google-account is always safe — present cookies arm it,
    absent cookies just warn."""
    return any(c.name == "google_account" and c.status == "ok" for c in capabilities)


def _verify_addresses(args: argparse.Namespace) -> list[str]:
    """Collect addresses to verify: every --verify EMAIL, plus a bare positional
    that is itself an email (`snoop jane@acme.com`)."""
    addrs = [a for a in (args.verify or []) if isinstance(a, str) and "@" in a]
    if args.name and "@" in args.name and "." in args.name.rsplit("@", 1)[-1]:
        addrs.append(args.name)
    return addrs


def _verify_setup(
    addresses: list[str], args: argparse.Namespace,
) -> tuple[Person, list[EmailCandidate]]:
    """Build a minimal person + the user-supplied candidates for --verify. No
    discovery fan-out: the address IS the subject. A name (via --person-plan)
    only feeds the Google name_match cross-check."""
    plan = _load_plan(args.person_plan) if args.person_plan else {}
    name = plan.get("name") or ""
    person = resolve_person(name, plan=plan) if name else Person(name="")
    now = datetime.now(timezone.utc)
    seen: dict[str, EmailCandidate] = {}
    for raw in addresses:
        addr = raw.strip().lower()
        if "@" not in addr or addr in seen:
            continue
        seen[addr] = EmailCandidate(
            address=addr,
            sources=[Source(type="manual_known", url=None, observed_at=now,
                            detail="address supplied for verification")],
        )
    return person, list(seen.values())


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.diagnose:
        print(diagnose.format_report(diagnose.diagnose()))
        h = ledger_health()
        ok = "OK" if h["writable"] else "!! "
        print(f"\n[{ok}] ledger: {h['path']} — {h['records']} record(s), "
              f"{h['malformed']} malformed, writable={h['writable']}")
        return 0

    # Verifier mode reads its bundle from stdin; no name/fetch needed.
    if args.ground:
        return _run_ground(observations_file=args.observations_file)

    # Capability probe runs once per invocation. Cheap (no network, no cookie
    # reads unless --allow-google-account). Feeds the warning surface at the top
    # of the bundle so degraded environments show up adjacent to the readings
    # instead of silently weakening them.
    capabilities = _fast_capability_probe(
        allow_google_account=args.allow_google_account,
    )
    warnings = _capability_warnings(
        capabilities, allow_google_account=args.allow_google_account,
    )

    # --verify (or a bare email positional): probe one or more specific addresses,
    # skipping person discovery entirely.
    verify_addresses = _verify_addresses(args)
    if verify_addresses:
        person, candidates = _verify_setup(verify_addresses, args)
        candidates.sort(key=_probe_rank)
        _probe_candidates(person, candidates, args,
                          google_ready=_google_ready(capabilities),
                          deadline_sec=args.deadline)
        _reassess_identity(person, candidates)
        _emit_observations(person, candidates, {}, warnings, out_path=args.out)
        return 0

    if not args.name and not args.person_plan:
        parser.error("must provide a name, an email to --verify, or --person-plan")

    # --person-plan wins over positional/flag-derived plan for any field
    # it specifies. The flag-derived plan fills gaps for everything else
    # so `snoop "Dan Neil" "Formation Bio" --domain formation.bio` works
    # without anyone constructing JSON.
    flag_plan = _plan_from_flags(args)
    file_plan = _load_plan(args.person_plan)
    plan = {**flag_plan, **file_plan}
    name = args.name or plan.get("name")
    if not name:
        parser.error("name required (positional or in --person-plan)")

    person = resolve_person(name, plan=plan)
    manual_known = _parse_knowns(args.known)
    # Package-registry inputs: the host supplies known packages the person
    # published as plan["packages"] = [{"registry": "npm"|"pypi", "name": ...}].
    raw_packages = plan.get("packages")
    packages = [p for p in raw_packages if isinstance(p, dict)] if isinstance(raw_packages, list) else []

    # Dossier enrichment: when the github handle is bound, fetch recently-pushed
    # repos. One extra API call, gated on the same bound-handle check as the
    # resolver fan-out so we never query GitHub for an untrusted hint.
    # Best-effort: empty list on any failure.
    gh_handle_bound = _gh_handle(person)
    if gh_handle_bound:
        person.gh_recent_repos = fetch_recent_repos(gh_handle_bound)

    # Fan out resolvers under the shared wall-clock deadline (E6). The SAME budget
    # wraps Phase 2 below, so the ≤60s promise covers the slow probes too: Phase 2
    # gets whatever is left after Phase 1 (ENG-8).
    run_start = time.monotonic()
    results = run_pipeline(person, manual_known=manual_known, packages=packages,
                           deadline_sec=args.deadline)
    # Per-sensor records: ran/degraded from the fan-out + skipped (gated-off)
    # sensors with reasons — the typed degradation contract.
    run_records = [RunRecord.from_resolver(r) for r in results]
    dns_available = any(c.name == "dnspython" and c.status == "ok"
                        for c in capabilities)
    run_records += _sensor_skips(
        person, packages=packages, no_smtp=args.no_smtp, no_pgp=args.no_pgp,
        allow_google_account=args.allow_google_account,
        dns_available=dns_available,
    )
    candidates = cluster_candidates(results)
    candidates.sort(key=_probe_rank)  # observed addresses lead the bundle

    # rel=me identity cross-links (E2) + PGP corroboration (E3) are network-bound
    # slow probes. Run them under the SAME shared wall-clock budget as Phase 1, so
    # the ≤deadline promise actually covers them (they previously ran unbounded,
    # before phase2_budget was even measured). Abandoned probes commit nothing.
    rel_me_links = _run_slow_probes(
        person, candidates, run_records,
        budget_sec=args.deadline - (time.monotonic() - run_start),
        no_pgp=args.no_pgp,
    )
    phase2_budget = args.deadline - (time.monotonic() - run_start)
    probe_rec = _probe_candidates(person, candidates, args,
                                  google_ready=_google_ready(capabilities),
                                  deadline_sec=phase2_budget)
    if probe_rec is not None:
        run_records.append(probe_rec)
    _reassess_identity(person, candidates)
    sys.stderr.write(_run_summary(run_records) + "\n")

    # The deliverable: the observation bundle the host model reasons over.
    # (Identity state + resolver notes are observations even with no candidates.)
    # --no-search drops the host-supplied work_search_results from the bundle.
    # resolution_gaps coaches a richer Step-1 pass when the plan was thin.
    _emit_observations(person, candidates, {} if args.no_search else plan, warnings,
                       out_path=args.out, resolution_gaps=_resolution_gaps(person),
                       run_records=run_records, rel_me_links=rel_me_links)

    # Append yield metadata to the local ledger (E1, on by default, best-effort).
    if not args.no_ledger:
        _write_ledger(person, candidates, packages, run_records)
    return 0


if __name__ == "__main__":
    sys.exit(main())
