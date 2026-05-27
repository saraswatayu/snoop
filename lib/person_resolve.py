"""lib/person_resolve.py — narrow v1 contract for identity binding.

Per Codex finding #4 + Subagent finding F1: the original 12-line
pseudocode was wildly under-spec'd for what is the load-bearing
identity-resolution problem. v1 deliberately scopes down: accept
explicit identity anchors (handle / personal_domain / employer) from
the caller, validate each anchor independently, surface deltas
between what the host model claimed and what the evidence shows.

What v1 DOES:
  - If a `github` handle is provided, fetch GET /users/{handle} and
    cross-check name + employer + blog/URL against the input plan.
  - Compute `bound_anchors`: which anchors from the plan are
    INDEPENDENTLY corroborated by observation. A `github` handle
    in the plan binds only when ≥2 such anchors agree.
  - Surface plan-vs-observed deltas as `notes` (Codex finding #5:
    defense against host hallucinated handles laundering into
    "high-confidence provenance").
  - Generate name_variants for downstream resolvers via lib.normalize.
  - Set ambiguity per the 3-state contract.

What v1 DOES NOT (deferred to v2):
  - GitHub user search to find a handle from name alone. The caller
    (host model) is the planner; if they didn't supply a handle, we
    don't fabricate one.
  - Disambiguation across multiple Matt-Smith-at-Acme candidates
    via probabilistic ranking. v1 sets ambiguity to
    `multiple_plausible_matches` when the input plan provides
    `ambiguity_candidates` and lets the caller decide.
  - Company/domain resolution as a separate subsystem (Codex #10).
    v1 trusts `employer.domains` from the plan.

The contract: v1 either binds anchors with confidence, OR sets
ambiguity to `insufficient_identity_evidence` and lets the rest of
the pipeline run with weaker downstream-scorer confidence.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .normalize import (
    fold_ascii,
    name_match,
    name_variants,
    normalize_domain,
)
from .schema import Employer, Person


_DEFAULT_TIMEOUT_SEC = 6.0

GhCaller = Callable[[str], Any]


def _gh_via_cli(path: str, *, timeout: float = _DEFAULT_TIMEOUT_SEC) -> Any:
    result = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github+json", path],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if result.returncode != 0:
        raise subprocess.SubprocessError(
            f"gh api {path} exited {result.returncode}: {result.stderr.strip()[:200]}"
        )
    if not result.stdout.strip():
        return {}
    return json.loads(result.stdout)


def _gh_via_http(path: str, *, timeout: float = _DEFAULT_TIMEOUT_SEC) -> Any:
    url = path if path.startswith("http") else f"https://api.github.com{path}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "snoop-skill", "Accept": "application/vnd.github+json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # handle does not exist
        raise


def _default_gh_caller() -> GhCaller | None:
    from shutil import which
    if which("gh") is not None:
        try:
            r = subprocess.run(
                ["gh", "auth", "status"], capture_output=True, timeout=2, check=False,
            )
            if r.returncode == 0:
                return _gh_via_cli
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
    return _gh_via_http


# ---- name/string comparison helpers ----------------------------------------


def _employer_match(observed_company: str | None, target_employer_name: str) -> bool:
    """Tolerant company-name match. Companies often have variants
    ('OpenAI' vs '@openai' vs 'OpenAI, Inc.'). Fold both, then check
    substring containment in either direction."""
    if not observed_company or not target_employer_name:
        return False
    obs = fold_ascii(observed_company).lstrip("@").strip().rstrip(",").rstrip(".")
    tgt = fold_ascii(target_employer_name).lstrip("@").strip().rstrip(",").rstrip(".")
    if not obs or not tgt:
        return False
    # Strip common suffixes
    for suffix in (" inc", " llc", " ltd", " corp", " gmbh", " co"):
        if obs.endswith(suffix): obs = obs[:-len(suffix)].rstrip(",").rstrip(".")
        if tgt.endswith(suffix): tgt = tgt[:-len(suffix)].rstrip(",").rstrip(".")
    return obs in tgt or tgt in obs


def _blog_matches_personal_domain(blog: str | None, domains: list[str]) -> str | None:
    """If `blog` URL's host equals (or is a subdomain of) a known
    personal_domain, return the matched domain. Otherwise None.

    Host parsing matters here: a naive substring check binds 'foo.com'
    to 'https://barfoo.com', and the false bind unlocks resolvers on
    the wrong person.
    """
    if not blog or not domains:
        return None
    raw_blog = blog.strip()
    parsed = urllib.parse.urlsplit(
        raw_blog if "://" in raw_blog else "//" + raw_blog
    )
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    for raw in domains:
        d = normalize_domain(raw)
        if not d:
            continue
        if host == d or host.endswith("." + d):
            return d
    return None


# ---- plan parsing -----------------------------------------------------------


def _employer_from_plan_dict(d: Any) -> Employer | None:
    """Build an Employer from a {"name": ..., "domains": [...], ...} dict."""
    if not isinstance(d, dict):
        return None
    name = d.get("name")
    domains = d.get("domains") or []
    if not isinstance(name, str) or not isinstance(domains, list):
        return None
    norm_domains = [normalize_domain(x) for x in domains if isinstance(x, str)]
    norm_domains = [x for x in norm_domains if x]
    if not name and not norm_domains:
        return None
    return Employer(
        name=name or "",
        domains=norm_domains,
        since=d.get("since") if isinstance(d.get("since"), str) else None,
        until=d.get("until") if isinstance(d.get("until"), str) else None,
    )


# ---- main resolver ----------------------------------------------------------


def resolve_person(
    name: str,
    *,
    plan: dict | None = None,
    gh_caller: GhCaller | None = None,
) -> Person:
    """Build a Person record by validating identity anchors against evidence.

    Args:
        name: Target's full name (required).
        plan: Optional --person-plan dict from the host model. Recognized keys:
            handles: dict (only "github" is validated in v1)
            personal_domains: list[str]
            employer: dict (Employer-shaped)
            former_employers: list[dict]
            channel_hints: dict[str, Any]
            ambiguity_candidates: list[dict] (caller indicates >1 plausible person)
        gh_caller: Injectable for tests; defaults to gh CLI then anon HTTP.

    Returns:
        Person with bound_anchors populated, notes capturing any
        plan-vs-observed deltas, and ambiguity set per the 3-state contract.
    """
    plan = plan or {}
    caller = gh_caller if gh_caller is not None else _default_gh_caller()

    # Start from the plan's claimed structure, but treat all of it as
    # untrusted hints until we validate.
    handles: dict[str, str] = {}
    plan_handles = plan.get("handles") if isinstance(plan, dict) else None
    if isinstance(plan_handles, dict):
        for k, v in plan_handles.items():
            if isinstance(k, str) and isinstance(v, str) and v.strip():
                handles[k] = v.strip()

    personal_domains_raw = plan.get("personal_domains") or []
    personal_domains: list[str] = []
    if isinstance(personal_domains_raw, list):
        for d in personal_domains_raw:
            if isinstance(d, str) and d.strip():
                nd = normalize_domain(d)
                if nd:
                    personal_domains.append(nd)

    employer = _employer_from_plan_dict(plan.get("employer"))
    former_employers = []
    fem_raw = plan.get("former_employers") or []
    if isinstance(fem_raw, list):
        for fe in fem_raw:
            built = _employer_from_plan_dict(fe)
            if built:
                former_employers.append(built)

    channel_hints = plan.get("channel_hints") or {}
    if not isinstance(channel_hints, dict):
        channel_hints = {}

    # Pre-compute name variants for the downstream pipeline
    variants_raw = name_variants(name)
    variants = [f"{v.first} {v.last}" for v in variants_raw]

    bound_anchors: list[tuple[str, str]] = []
    notes: list[str] = []

    # Validate GitHub handle (the only handle we resolve in v1)
    github_handle = handles.get("github")
    profile: dict | None = None
    if github_handle and caller is not None:
        try:
            result = caller(f"/users/{github_handle}")
            if isinstance(result, dict) and result.get("login"):
                profile = result
            elif result is None:
                notes.append(
                    f"plan claimed github={github_handle!r} but GitHub returns 404 "
                    f"— handle does not exist; downstream resolvers will skip it"
                )
                handles.pop("github", None)
        except (subprocess.SubprocessError, urllib.error.URLError,
                json.JSONDecodeError, OSError) as e:
            notes.append(
                f"could not validate github handle {github_handle!r}: "
                f"{type(e).__name__}; downstream may degrade"
            )

    # If we got a profile, score the anchor binding
    if profile is not None:
        # Anchor 1: name match
        observed_name = profile.get("name")
        if name_match(observed_name, name):
            bound_anchors.append(("github_name_match", observed_name or ""))
        elif observed_name:
            notes.append(
                f"plan claimed name={name!r}; github profile says "
                f"name={observed_name!r} — names do not match, treating handle as weak signal"
            )
        else:
            notes.append(
                f"github profile for {github_handle!r} has no public name field "
                f"— cannot verify name match"
            )

        # Anchor 2: employer match
        if employer:
            observed_company = profile.get("company")
            if _employer_match(observed_company, employer.name):
                bound_anchors.append(("github_employer_match", employer.name))
            elif observed_company:
                notes.append(
                    f"plan claimed employer={employer.name!r}; github profile "
                    f"company={observed_company!r} — employer differs"
                )

        # Anchor 3: blog/URL crosses a personal_domain
        blog = profile.get("blog")
        matched_domain = _blog_matches_personal_domain(blog, personal_domains)
        if matched_domain:
            bound_anchors.append(("github_personal_domain_match", matched_domain))

        # Always: record the handle exists. This is NOT enough to bind on
        # its own (Codex c5: handle existence ≠ correct identity), so we
        # record it as a "weak" anchor distinct from the validating ones.
        bound_anchors.append(("github_handle_exists", str(github_handle)))

    # Compute ambiguity per the 3-state contract.
    plan_ambig_candidates = plan.get("ambiguity_candidates")
    if isinstance(plan_ambig_candidates, list) and len(plan_ambig_candidates) > 1:
        # Caller explicitly flagged "this name is ambiguous, here are the
        # candidate people." v1 surfaces but doesn't disambiguate.
        ambiguity = "multiple_plausible_matches"
    else:
        # Count "validating" anchors (excluding the trivial handle-exists one).
        validating = [
            a for a in bound_anchors
            if a[0] != "github_handle_exists"
        ]
        if len(validating) >= 2:
            ambiguity = "single_plausible_match"
        elif len(validating) == 1:
            # One anchor: weak. Surface as insufficient evidence per
            # Codex c6 (single anchor ≠ uniquely identified).
            ambiguity = "insufficient_identity_evidence"
            notes.append(
                "only 1 identity anchor bound — handle is plausibly the right "
                "person but not independently confirmed (≥2 anchors required)"
            )
        elif github_handle and profile is not None:
            # Handle resolved but nothing matched — could be a hallucinated
            # handle the host model invented.
            ambiguity = "insufficient_identity_evidence"
            notes.append(
                f"github handle {github_handle!r} exists but nothing in the "
                f"profile matches the input (name/employer/personal_domain) "
                f"— treating as untrusted hint"
            )
        elif github_handle is None and (employer or personal_domains):
            # No handle was given, but we have other anchors (employer,
            # personal_domain). Pipeline can still run; resolver abstains.
            ambiguity = "insufficient_identity_evidence"
            notes.append(
                "no github handle in plan — pattern_gen and personal_site can "
                "still run but person identity is not independently bound"
            )
        else:
            ambiguity = "insufficient_identity_evidence"
            notes.append(
                "no identity anchors provided or validated — downstream "
                "resolvers will run on minimal info"
            )

    return Person(
        name=name,
        name_variants=variants,
        handles=handles,
        personal_domains=personal_domains,
        employer=employer,
        former_employers=former_employers,
        channel_hints=channel_hints,
        ambiguity=ambiguity,
        bound_anchors=bound_anchors,
        notes=notes,
    )
