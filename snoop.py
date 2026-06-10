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
    google_account probe  (Google-hosted candidates, --allow-google-account)
              ↓
    verify_smtp top-K candidates  (skip personal-provider / known-dead)
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
import json
import sys
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
)
from pathlib import Path
from typing import Any, Callable

from lib import __version__, diagnose, reason, render
from lib.diagnose import Capability
from lib.git_emails import fetch_git_emails
from lib.gh_profile import fetch_gh_profile, fetch_recent_repos
from lib.google_account import fetch_google_account
from lib.normalize import is_personal_provider
from lib.pattern_gen import fetch_pattern_candidates
from lib.person_resolve import resolve_person
from lib.personal_site import fetch_personal_site
from lib.schema import EmailCandidate, Person, ResolverResult
from lib.verify_smtp import ProbeBudget, default_budget, is_google_hosted, verify_candidates


_PER_RESOLVER_TIMEOUT_SEC = 5.0


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
        "--no-smtp",
        action="store_true",
        help="Skip SMTP probing entirely (faster; loses deliverable signal).",
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
        "--ground",
        action="store_true",
        help="Verifier mode: read {observations, facts, ...} JSON on stdin and "
             "drop any fact whose citations don't reference a real observation, "
             "then render the grounded card. The deterministic citation check.",
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
    """Wrap a resolver call with a uniform error → ResolverResult contract.
    Per-call timeout is enforced by the outer ThreadPoolExecutor via
    future.result(timeout=N)."""
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
    per_resolver_timeout_sec: float = _PER_RESOLVER_TIMEOUT_SEC,
) -> list[ResolverResult]:
    """Fan out all enabled resolvers in parallel.

    Each resolver gets a wall-clock timeout via future.result(timeout=N).
    A timed-out resolver returns ResolverResult(status="timeout"); the
    pipeline keeps going.
    """
    manual_known = manual_known or []
    gh_handle = _gh_handle(person)

    tasks: list[tuple[str, Callable[[], ResolverResult]]] = []
    if gh_handle:
        tasks.append(("git_emails", lambda: fetch_git_emails(gh_handle)))
        tasks.append(("gh_profile", lambda: fetch_gh_profile(gh_handle)))
    if person.personal_domains:
        tasks.append((
            "personal_site",
            lambda: fetch_personal_site(person.personal_domains),
        ))
    # pattern_gen runs even with no other inputs (it's the explicit fallback)
    tasks.append((
        "pattern_gen",
        lambda: fetch_pattern_candidates(person, manual_known=manual_known),
    ))

    results: list[ResolverResult] = []
    with ThreadPoolExecutor(max_workers=max(1, len(tasks))) as ex:
        future_to_name: dict[Future, str] = {
            ex.submit(_run_resolver, name, fn): name for name, fn in tasks
        }
        for future in list(future_to_name):
            name = future_to_name[future]
            try:
                results.append(future.result(timeout=per_resolver_timeout_sec))
            except FutureTimeoutError:
                results.append(ResolverResult(
                    resolver=name,
                    candidates=[],
                    status="timeout",
                    error_detail=f"exceeded {per_resolver_timeout_sec}s budget",
                ))
                future.cancel()
    return results


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
    """Ordering key for which candidates to probe (and emit) first. No scorer:
    rank by whether the address was actually observed (any non-pattern source)
    over a pure name×domain guess, then by how many sources corroborate it, then
    address for determinism. The host model still reasons over every candidate;
    this only decides probe order so an observed address is tried before a guess
    that might hit a stranger's mailbox on a multi-user Workspace tenant."""
    observed = any(s.type != "pattern" for s in c.sources)
    return (0 if observed else 1, -len(c.sources), c.address)


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
             "evidence_ids": f.evidence_ids, "reasoning": f.reasoning}
            for f in profile.facts
        ],
        "usage": profile.usage,
    }, indent=2, default=str)


def _emit_observations(person: Person, candidates: list[EmailCandidate],
                       plan: dict[str, Any], warnings: list[str]) -> None:
    """Sensor mode: emit the raw observation bundle the host model reasons over.

    snoop's irreducible job is the I/O the host can't do (git/GitHub/SMTP/Google/
    MX); this dumps what those sensors saw, typed and cited, plus any host-model
    web-search observations. No binding, no rendering — the host is the analyst.
    The one exception is the lightweight `belongs~` ordering hint carried on each
    email_candidate observation: it only ranks which candidates were probed
    first, and the host reasons over the readings regardless."""
    observations = reason.build_evidence(person, candidates)
    base = len(observations)
    for i, o in enumerate(_work_search_observations(plan), start=base + 1):
        observations.append(reason.Observation(
            id=f"o{i}", type=o.type, content=o.content, source_url=o.source_url,
        ))
    bundle = {
        "warnings": warnings or [],
        "person": {"name": person.name, "ambiguity": person.ambiguity},
        "observations": [
            {"id": o.id, "type": o.type, "content": o.content,
             "source_url": o.source_url}
            for o in observations
        ],
    }
    sys.stdout.write(json.dumps(bundle, indent=2, default=str))


def _run_ground() -> int:
    """Verifier mode: read {observations, facts, summary, ...} on stdin, drop
    facts whose citations don't reference a real observation, render the card.
    Pure + offline — the deterministic check the host model can't self-certify."""
    from lib.ground import ground

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"--ground: invalid JSON on stdin: {exc}\n")
        return 2

    observations = [
        reason.Observation(
            id=str(o.get("id", "")), type=str(o.get("type", "")),
            content=str(o.get("content", "")), source_url=o.get("source_url"),
        )
        for o in payload.get("observations", []) if isinstance(o, dict)
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
    if payload.get("json"):
        sys.stdout.write(_format_reasoned_json(rp, warnings=payload.get("warnings")))
    else:
        sys.stdout.write(render.render_reasoned_card(rp, warnings=payload.get("warnings")))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.diagnose:
        print(diagnose.format_report(diagnose.diagnose()))
        return 0

    # Verifier mode reads its bundle from stdin; no name/fetch needed.
    if args.ground:
        return _run_ground()

    if not args.name and not args.person_plan:
        parser.error("must provide a name or --person-plan")

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

    # Dossier enrichment: when the github handle is bound, fetch recently-pushed
    # repos. One extra API call, gated on the same bound-handle check as the
    # resolver fan-out so we never query GitHub for an untrusted hint.
    # Best-effort: empty list on any failure.
    gh_handle_bound = _gh_handle(person)
    if gh_handle_bound:
        person.gh_recent_repos = fetch_recent_repos(gh_handle_bound)

    # Fan out resolvers
    results = run_pipeline(person, manual_known=manual_known)
    candidates = cluster_candidates(results)
    candidates.sort(key=_probe_rank)  # observed addresses lead the bundle

    # Google account verification on Google-hosted candidates. Runs BEFORE SMTP
    # so that not_found verdicts short-circuit SMTP probes on dead candidates
    # (Google's view is authoritative when it returns a verdict).
    if candidates and args.allow_google_account:
        # Auto-detect Workspace MX so the user doesn't need to pass
        # --google-workspace-domain for every YC startup on Gmail.
        merged_workspace = _autodetect_workspace_domains(
            candidates, args.google_workspace_domain,
        )
        google_targets = _google_account_candidates(candidates, merged_workspace)
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
                person.notes.append(
                    f"google_account {google_result.status}: {google_result.error_detail}"
                )

    # SMTP probe top-K work candidates
    if candidates and not args.no_smtp:
        smtp_targets = _smtp_candidates(candidates)
        if smtp_targets:
            verify_candidates(smtp_targets, budget=default_budget())

    # The deliverable: the observation bundle the host model reasons over.
    # (Identity state + resolver notes are observations even with no candidates.)
    # --no-search drops the host-supplied work_search_results from the bundle.
    _emit_observations(person, candidates, {} if args.no_search else plan, warnings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
