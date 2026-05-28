#!/usr/bin/env python3
"""snoop.py — entry point for the redesigned /snoop skill.

Pipeline:

    parse args → load --person-plan JSON
              ↓
    person_resolve.resolve_person  (validate identity anchors, surface deltas)
              ↓
    fan-out (ThreadPoolExecutor, 5s per-resolver timeout):
       git_emails | gh_profile | personal_site | pattern_gen
              ↓
    cluster  (dedupe by lowercased address; merge source lists)
              ↓
    score_all  (3-field provenance-aware scorer)
              ↓
    verify_smtp top-K work candidates  (skip personal-provider)
              ↓
    re-score  (SMTP modifies deliverable)
              ↓
    render_decision_card  (markdown contact card)

Legacy compatibility: the original verify_email.py script still works
for single-address verification. snoop.py is the new rich-pipeline
entry; use it for any name+company lookup.
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

from lib import diagnose, render
from lib.git_emails import fetch_git_emails
from lib.gh_profile import fetch_gh_profile
from lib.google_account import fetch_google_account
from lib.pattern_gen import fetch_pattern_candidates
from lib.person_resolve import resolve_person
from lib.personal_site import fetch_personal_site
from lib.schema import EmailCandidate, Person, ResolverResult
from lib.score import is_personal_provider, score_all
from lib.verify_smtp import ProbeBudget, default_budget, verify_candidates


_PER_RESOLVER_TIMEOUT_SEC = 5.0


# ---- CLI plumbing -----------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="snoop",
        description="Resolve a person's email addresses across public sources.",
    )
    p.add_argument(
        "name",
        nargs="?",
        help="Full name of the target (e.g. 'Peter Steinberger')",
    )
    p.add_argument(
        "--person-plan",
        help=(
            "JSON file path OR @file OR inline JSON string. Schema: "
            "{name, handles{github,x,hn}, personal_domains[], "
            "employer{name,domains[]}, former_employers[], channel_hints{}}. "
            "Host model produces this upstream; person_resolve re-derives and "
            "surfaces deltas."
        ),
    )
    p.add_argument(
        "--intent",
        choices=("work", "personal", "either"),
        default="work",
        help="Sort/recommendation bias (default: work)",
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
        "--max-per-section",
        type=int,
        default=5,
        help="Maximum rows per Work / Personal / Other section (default 5).",
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
            "Repeatable. Treat DOMAIN as Google-Workspace-hosted (its MX is "
            "aspmx.l.google.com). Required for non-literal-google.com domains "
            "since v1 doesn't auto-detect MX. e.g. --google-workspace-domain acme.com"
        ),
    )
    p.add_argument(
        "--diagnose",
        action="store_true",
        help="Print a capability probe report and exit (no lookup).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the markdown card.",
    )
    return p


def _load_plan(arg: str | None) -> dict[str, Any]:
    """Resolve --person-plan to a dict.

    Three forms accepted:
      - "@path/to/file.json" (literal @-prefix → file path)
      - "path/to/file.json"  (heuristic: if it looks like a path and exists)
      - "{... json ...}"     (inline)
    """
    if not arg:
        return {}
    arg = arg.strip()
    if not arg:
        return {}
    if arg.startswith("@"):
        path = Path(arg[1:]).expanduser()
        return json.loads(path.read_text())
    if (arg.startswith("{") or arg.startswith("[")):
        return json.loads(arg)
    # Treat as file path if it exists
    p = Path(arg).expanduser()
    if p.exists():
        return json.loads(p.read_text())
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


def _google_account_candidates(
    candidates: list[EmailCandidate],
    workspace_domains: list[str],
) -> list[EmailCandidate]:
    """Filter candidates to those on Google-hosted domains worth probing.
    Skip candidates that already have an account_exists verdict (don't
    re-probe within one invocation)."""
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
    return out


def _smtp_candidates(candidates: list[EmailCandidate], top_k: int = 5) -> list[EmailCandidate]:
    """Pick the top candidates that are worth SMTP-probing: non-personal-provider,
    at least one source, not already known-dead via Google. Sorted by
    belongs_to_person descending.

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
    eligible.sort(key=lambda c: -(c.belongs_to_person or 0))
    return eligible[:top_k]


def _format_json_report(person: Person, candidates: list[EmailCandidate]) -> str:
    """Machine-readable equivalent of the markdown card. Useful for
    pipelining `snoop --json` into another tool."""
    return json.dumps({
        "person": {
            "name": person.name,
            "name_variants": person.name_variants,
            "handles": person.handles,
            "personal_domains": person.personal_domains,
            "employer": (
                {"name": person.employer.name, "domains": person.employer.domains}
                if person.employer else None
            ),
            "ambiguity": person.ambiguity,
            "bound_anchors": [list(a) for a in person.bound_anchors],
            "notes": person.notes,
            "channel_hints": person.channel_hints,
        },
        "candidates": [
            {
                "address": c.address,
                "belongs_to_person": c.belongs_to_person,
                "current_work_address": c.current_work_address,
                "deliverable": c.deliverable,
                "smtp_verdict": c.smtp_verdict,
                "mx_provider": c.mx_provider,
                "employer_match": c.employer_match,
                "is_personal_provider": c.is_personal_provider,
                "score_reasons": c.score_reasons,
                "sources": [
                    {
                        "type": s.type, "url": s.url,
                        "observed_at": s.observed_at.isoformat(),
                        "detail": s.detail,
                    }
                    for s in c.sources
                ],
            }
            for c in candidates
        ],
    }, indent=2, default=str)


# ---- main -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.diagnose:
        print(diagnose.format_report(diagnose.diagnose()))
        return 0

    if not args.name and not args.person_plan:
        parser.error("must provide a name or --person-plan")

    plan = _load_plan(args.person_plan)
    name = args.name or plan.get("name")
    if not name:
        parser.error("name required (positional or in --person-plan)")

    person = resolve_person(name, plan=plan)
    manual_known = _parse_knowns(args.known)

    # Fan out resolvers
    results = run_pipeline(person, manual_known=manual_known)
    candidates = cluster_candidates(results)

    if not candidates:
        # Empty pipeline — still render so the user sees identity state
        # and resolver notes. Respect --json mode.
        if args.json:
            sys.stdout.write(_format_json_report(person, []))
        else:
            sys.stdout.write(render.render_decision_card(person, [], intent=args.intent))
        return 0

    # Initial scoring (without verification signals yet)
    score_all(candidates, person)

    # Google account verification on Google-hosted candidates. Runs BEFORE
    # SMTP so that not_found verdicts short-circuit SMTP probes on dead
    # candidates (Google's view is authoritative when it returns a verdict).
    if args.allow_google_account:
        google_targets = _google_account_candidates(
            candidates, args.google_workspace_domain,
        )
        if google_targets:
            google_result = fetch_google_account(
                google_targets,
                target_domains=_google_target_domains(args.google_workspace_domain),
                budget=ProbeBudget(
                    per_domain=30,
                    state_path=Path.home() / ".snoop" / "google-budget.json",
                ),
            )
            # Surface non-ok resolver outcomes (rate limit, cookies missing,
            # auth failure) so the user can tell 'didn't find' apart from
            # 'didn't ask'. Silent swallowing lets a stale browser session
            # masquerade as a clean lookup.
            if google_result.status in ("error", "unavailable") and google_result.error_detail:
                person.notes.append(
                    f"google_account {google_result.status}: {google_result.error_detail}"
                )
            # Re-score so account_exists feeds the next stage's top-K pick
            score_all(candidates, person)

    # SMTP probe top-K work candidates
    if not args.no_smtp:
        smtp_targets = _smtp_candidates(candidates)
        if smtp_targets:
            verify_candidates(smtp_targets, budget=default_budget())
            # Re-score after SMTP modifies deliverable
            score_all(candidates, person)

    if args.json:
        sys.stdout.write(_format_json_report(person, candidates))
    else:
        output = render.render_decision_card(
            person, candidates,
            intent=args.intent,
            max_per_section=args.max_per_section,
        )
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
