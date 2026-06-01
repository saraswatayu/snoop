"""lib/binding.py — per-fact provenance binding.

ONE question for any Source: how strongly is it tied to the resolved identity?
This is the shared primitive behind D3 (the search anchor gate) and D4 (the
two-level provenance display) — built once, reused by both, so name/domain/
cross-link matching lives in a single place (Codex DRY finding). It reuses the
matching rules from person_resolve rather than reimplementing them.

Two-level model (D4):
  Level 1 (identity gate) is applied by the CALLER (renderer): if
  ``identity.ambiguity != "single_plausible_match"``, cap every tier to
  "possibly". This module implements Level 2 (the per-field provenance tier).

A source is **asserted** (bound-by-construction) when:
  - it is directly user-supplied (``Source.type == "manual_known"``), or
  - its host is a personal_domain that is itself BOUND via a
    ``github_personal_domain_match`` anchor (cross-linked from the profile), or
  - it is derived from the VALIDATED profile surface (the github handle is
    bound by >=2 validating anchors).

A domain merely DECLARED in the model-produced ``--person-plan`` is NOT bound
(outside-voice Codex #2). It yields "possibly" at most. A source with no tie at
all is "unbound" — the caller drops it (this is what makes free-text search
safe: a page that merely mentions the name, with no cross-link back to a bound
signal, never gets attributed).

      Source ─┐
              ├─ manual_known ─────────────────────────▶ asserted
              ├─ host ∈ cross-link-bound domains ───────▶ asserted
              ├─ profile-typed & handle validated ──────▶ asserted
              ├─ host ∈ declared (unvalidated) domains ─▶ possibly
              ├─ profile-typed, handle NOT validated ───▶ possibly
              └─ otherwise ────────────────────────────▶ unbound  (drop)
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from typing import Iterable

from .normalize import normalize_domain
from .schema import BindTier, Identity, Source


# Anchor types from person_resolve that independently validate the github
# handle. >=2 of these is person_resolve's single_plausible_match threshold.
_VALIDATING_ANCHORS = frozenset({
    "github_name_match",
    "github_employer_match",
    "github_personal_domain_match",
})

# Source types that originate from the github identity surface itself.
_PROFILE_SOURCE_TYPES = frozenset({
    "gh_profile", "gh_readme", "git_commit", "github_repo",
})

# Source types that are OBSERVED for this specific person (provided during plan
# construction or read from a profile field) but are not bound-by-construction.
# They tie to the person by provenance, so they are "possibly" rather than
# "unbound" — distinct from a free-text web_search hit, which is namesake-risky
# and must cross-link to bind.
_PROVIDED_SOURCE_TYPES = frozenset({
    "channel_hint", "linkedin", "hn_profile", "x_bio",
})

_TIER_ORDER = {"unbound": 0, "possibly": 1, "asserted": 2}


@dataclass
class Binding:
    """The result of binding a source (or set of sources) to an identity."""
    tier: BindTier
    reasons: list[str] = field(default_factory=list)


def _host(url: str | None) -> str | None:
    """Parse the registrable host from a URL. Returns None when absent.

    Host parsing (not substring matching) is deliberate: a substring check
    binds 'foo.com' to 'https://barfoo.com'. Mirrors
    person_resolve._blog_matches_personal_domain.
    """
    if not url:
        return None
    raw = url.strip()
    parsed = urllib.parse.urlsplit(raw if "://" in raw else "//" + raw)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _bound_domains(identity: Identity) -> set[str]:
    """Domains PROVEN to belong to the person via a cross-link anchor — NOT the
    raw declared personal_domains (Codex #2: declared != validated)."""
    out: set[str] = set()
    for atype, value in identity.bound_anchors:
        if atype == "github_personal_domain_match":
            d = normalize_domain(value)
            if d:
                out.add(d)
    return out


def _handle_is_validated(identity: Identity) -> bool:
    """True when >=2 validating anchors bind the github handle, matching
    person_resolve's single_plausible_match threshold."""
    n = sum(1 for atype, _ in identity.bound_anchors if atype in _VALIDATING_ANCHORS)
    return n >= 2


def bind_source(source: Source, identity: Identity) -> Binding:
    """Classify one source's binding to the resolved identity. See module docs."""
    # 1) user-supplied ground truth
    if source.type == "manual_known":
        return Binding("asserted", ["user-supplied source"])

    host = _host(source.url)

    # 2) host is a cross-link-bound personal domain
    if host:
        for d in _bound_domains(identity):
            if _host_matches(host, d):
                return Binding(
                    "asserted",
                    [f"source host '{host}' is a bound personal domain ('{d}')"],
                )

    # 3) derived from the validated profile surface
    if source.type in _PROFILE_SOURCE_TYPES and _handle_is_validated(identity):
        return Binding("asserted", [f"from validated profile ({source.type})"])

    # 4) host matches a DECLARED-but-unvalidated personal_domain -> possibly
    #    (Codex #2: a plan-declared domain is an untrusted hint, not asserted)
    if host:
        for raw in identity.personal_domains:
            d = normalize_domain(raw)
            if d and _host_matches(host, d):
                return Binding(
                    "possibly",
                    [f"source host '{host}' matches declared (unvalidated) domain '{d}'"],
                )

    # 5) profile-typed source but the handle is not independently bound
    if source.type in _PROFILE_SOURCE_TYPES:
        return Binding(
            "possibly",
            [f"{source.type} source but identity not independently bound (<2 anchors)"],
        )

    # 6) observed-for-this-person channels (provided in the plan / a profile
    #    field) — tied by provenance but not bound-by-construction
    if source.type in _PROVIDED_SOURCE_TYPES:
        return Binding("possibly", [f"observed channel ({source.type}); not bound-by-construction"])

    # 7) no tie at all (e.g. a free-text web_search hit with no cross-link) -> drop
    return Binding("unbound", ["no binding evidence tying source to the person"])


def bind_best(sources: Iterable[Source], identity: Identity) -> Binding:
    """Highest binding across a fact's sources (asserted > possibly > unbound).

    A fact is attributed at the strength of its strongest source. A fact whose
    sources all come back 'unbound' should be dropped by the caller.
    """
    best = Binding("unbound", ["no sources"])
    first = True
    for s in sources:
        b = bind_source(s, identity)
        if first or _TIER_ORDER[b.tier] > _TIER_ORDER[best.tier]:
            best = b
            first = False
    return best


def apply_identity_gate(tier: BindTier, identity: Identity) -> BindTier:
    """D4 Level 1: when identity itself is not a single plausible match, no field
    can be asserted — cap to 'possibly'. Callers (the renderer) apply this on top
    of the per-field tier from bind_source/bind_best."""
    if identity.ambiguity != "single_plausible_match" and tier == "asserted":
        return "possibly"
    return tier
