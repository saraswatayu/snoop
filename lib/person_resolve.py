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
from typing import Any

from . import _gh_api
from ._gh_api import GhCaller
from .normalize import (
    fold_ascii,
    name_match,
    name_variants,
    normalize_domain,
)
from .schema import Employer, Person


def _gh_404_to_none(caller: GhCaller) -> GhCaller:
    """Wrap a caller so a 404 HTTPError comes back as None — the resolver
    uses None as 'handle does not exist on GitHub' to emit a precise note
    ('handle does not exist; downstream resolvers will skip it') instead of
    the generic 'could not validate' message.

    This is a person_resolve-specific contract. Other resolvers treat 404 as
    a generic error since a missing user there means 'no data,' not 'plan
    was wrong about the handle.'
    """
    def wrapped(path: str) -> Any:
        try:
            return caller(path)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
    return wrapped


def _default_gh_caller() -> GhCaller | None:
    return _gh_404_to_none(_gh_api.default_gh_caller())


# ---- name/string comparison helpers ----------------------------------------


_ORG_SUFFIX_TOKENS = frozenset({
    "inc", "llc", "ltd", "corp", "co", "company",
    "gmbh", "ag", "sa", "bv", "kg", "ltda", "srl", "spa",
    "lp", "llp", "plc",
})


def _employer_match(observed_company: str | None, target_employer_name: str) -> bool:
    """Tolerant company-name match. Companies often have variants
    ('OpenAI' vs '@openai' vs 'OpenAI, Inc.'). Compare as token sets —
    one must be a subset of the other after stripping common org suffixes.

    Token-set was chosen over the earlier substring containment because
    `tgt in obs or obs in tgt` false-positives in two real ways:
      - plan='Apple' vs observed='Applesauce' → 'apple' in 'applesauce' = True
      - plan='A' vs observed='OpenAI'         → 'a' in 'openai' = True
    Both would falsely bind the github_employer_match anchor, which (combined
    with one other correct anchor) flips ambiguity to single_plausible_match
    and shows the user a misleading "identity confirmed" framing.
    """
    if not observed_company or not target_employer_name:
        return False
    def _tokens(s: str) -> set[str]:
        folded = fold_ascii(s).lstrip("@")
        # Split on whitespace AND common separators
        raw = [t.strip(",.()[]{}\"'") for t in folded.split()]
        out = {t for t in raw if t and t not in _ORG_SUFFIX_TOKENS}
        return out
    obs_tokens = _tokens(observed_company)
    tgt_tokens = _tokens(target_employer_name)
    if not obs_tokens or not tgt_tokens:
        return False
    return obs_tokens.issubset(tgt_tokens) or tgt_tokens.issubset(obs_tokens)


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
    gh_name: str | None = None
    gh_bio: str | None = None
    gh_blog: str | None = None
    gh_twitter: str | None = None
    gh_company: str | None = None
    gh_location: str | None = None
    if profile is not None:
        # Capture dossier fields from the same fetch (Tier 1 dossier per
        # docs/plans/2026-05-28-output-redesign.md, D4-C). Free: this
        # is the same /users/{handle} call that drives anchor binding.
        def _str_or_none(v: object) -> str | None:
            if isinstance(v, str) and v.strip():
                return v.strip()
            return None
        gh_name = _str_or_none(profile.get("name"))
        gh_bio = _str_or_none(profile.get("bio"))
        gh_blog = _str_or_none(profile.get("blog"))
        gh_twitter = _str_or_none(profile.get("twitter_username"))
        gh_company = _str_or_none(profile.get("company"))
        gh_location = _str_or_none(profile.get("location"))

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
        gh_name=gh_name,
        gh_bio=gh_bio,
        gh_blog=gh_blog,
        gh_twitter=gh_twitter,
        gh_company=gh_company,
        gh_location=gh_location,
    )
