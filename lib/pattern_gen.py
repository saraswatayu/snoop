"""lib/pattern_gen.py — name × domain × template → candidate emails.

The fallback resolver. When discovery resolvers (git_emails, gh_profile,
personal_site) come up dry, pattern generation is what remains. It is
explicitly the lowest-trust source in the scorer's source weights
(SOURCE_WEIGHTS["pattern"] = 0.15 baseline; corroboration via
known-company-pattern inference multiplies up to ~0.65).

Behavior matches the legacy `verify_email.py`'s `_templates` /
`infer_patterns` / `rank_candidates` logic, with three changes:

1. Name handling delegates to `lib.normalize` (NFKD fold, accent strip,
   name_variants for Eastern-order / German-transliteration).

2. Output is a list of `EmailCandidate`s with `Source(type="pattern",
   detail=...)`, not the legacy bare-list. The scorer treats pattern
   sources distinctly.

3. Pattern inference uses *all* `manual_known` addresses whose domain
   matches one of the target domains, even if the names span multiple
   target domains (e.g. user passes both Acme and Beta knowns when
   asking about a multi-domain target).

A `manual_known` entry is `(email, full_name)`. Acme uses `flast` if
two known acme.com employees both follow that pattern; pattern_gen
then ranks `flast`-shaped candidates for the target at the top with
a "matches company format from N knowns" provenance string.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from .normalize import (
    fold_ascii,
    localpart_templates,
    name_variants,
    normalize_domain,
    parse_name,
)
from .schema import EmailCandidate, Person, ResolverResult, Source


# Template-popularity baseline. Used when there are no `manual_known` votes:
# we still want the well-known patterns (first.last, flast) ranked above
# rare ones (lastf, fl). This list is just an ordering hint — not used as
# a confidence weight.
_DEFAULT_TEMPLATE_ORDER = (
    "first.last",
    "firstlast",
    "flast",
    "f.last",
    "firstl",
    "first_last",
    "first-last",
    "last.first",
    "first",
    "lastfirst",
    "lastf",
    "fl",
)


def infer_company_templates(
    manual_known: Sequence[tuple[str, str | None]],
    target_domains: Sequence[str],
) -> dict[str, dict[str, int]]:
    """Count template votes per target domain from same-domain known addresses.

    Returns: {domain: {template_name: vote_count}}

    Only counts a vote when:
      - the known email has a parsable name attached
      - the known email's domain matches one of `target_domains`
      - the email's localpart EQUALS one of the templates applied to the
        known's name (after fold_to_letters)
    """
    target_domains_lc = {normalize_domain(d) for d in target_domains if d}
    votes: dict[str, dict[str, int]] = {}

    for email, full_name in manual_known:
        if not email or "@" not in email or not full_name:
            continue
        local, _, domain = email.lower().rpartition("@")
        domain = normalize_domain(domain)
        if domain not in target_domains_lc:
            continue
        parsed = parse_name(full_name)
        if parsed is None:
            continue
        # Accent-fold the localpart but PRESERVE separators (.,-,_).
        # `fold_to_letters` would strip the dot in "sam.altman" — wrong here,
        # since we're comparing against template outputs that DO have dots.
        local_clean = fold_ascii(local)
        # For each variant of the known's name, check which templates this
        # localpart matches. A name can match multiple templates
        # (e.g. "pete" matches both "first" and "f" depending on which
        # parsed form is used). Each matching template gets a vote.
        for variant in name_variants(full_name):
            templates = localpart_templates(variant.first, variant.last)
            for template_name, predicted in templates.items():
                if predicted == local_clean:
                    domain_votes = votes.setdefault(domain, {})
                    domain_votes[template_name] = domain_votes.get(template_name, 0) + 1
                    break  # one vote per variant max
    return votes


def fetch_pattern_candidates(
    person: Person,
    target_domains: Sequence[str] | None = None,
    *,
    manual_known: Sequence[tuple[str, str | None]] = (),
    cap: int = 25,
    now: datetime | None = None,
) -> ResolverResult:
    """Generate pattern-based EmailCandidates for the target on each domain.

    Args:
        person: The target Person record.
        target_domains: Domains to generate candidates on. Defaults to
            (person.employer.domains) + person.personal_domains. Pass
            explicitly to override (e.g., when probing a former employer).
        manual_known: Optional (email, full_name) tuples for same-company
            known addresses. Used to infer the company's template, which
            promotes pattern-matching candidates to the top of the list.
        cap: Maximum candidates returned (across all domains). Default 25.
        now: For deterministic tests.

    Returns:
        ResolverResult with status="ok" if any candidates emitted,
        "empty" otherwise.
    """
    start = datetime.now(timezone.utc) if now is None else now

    if target_domains is None:
        domains: list[str] = []
        if person.employer:
            domains.extend(person.employer.domains)
        domains.extend(person.personal_domains)
    else:
        domains = list(target_domains)

    # Two-stage filter: drop falsy inputs (None, "") AND drop entries that
    # become empty after normalization (e.g. "  " → "" after strip).
    domains_normalized: list[str] = []
    for d in domains:
        if not d or not str(d).strip():
            continue
        nd = normalize_domain(d)
        if nd:
            domains_normalized.append(nd)
    if not domains_normalized:
        return ResolverResult(
            resolver="pattern_gen", candidates=[], status="empty",
            error_detail="no target_domains and person has no employer/personal_domains",
        )

    variants = name_variants(person.name)
    if not variants:
        return ResolverResult(
            resolver="pattern_gen", candidates=[], status="empty",
            error_detail=f"could not parse target name {person.name!r}",
        )

    votes = infer_company_templates(manual_known, domains_normalized)

    # Build the ordered template list per domain. Domains with knowns put
    # the inferred-winner templates first; everything else falls back to
    # the popularity-default order.
    #
    # Tiebreaker: when two templates have equal vote counts (which happens
    # naturally because name_variants emits Eastern-order reversals,
    # producing symmetric votes for first.last AND last.first from one
    # known), break the tie using default-popularity rank. first.last is
    # empirically far more common than last.first in corporate email, so
    # the default order encodes that prior.
    _default_rank = {tmpl: i for i, tmpl in enumerate(_DEFAULT_TEMPLATE_ORDER)}
    def template_order_for(domain: str) -> list[tuple[str, int]]:
        d_votes = votes.get(domain, {})
        ordered: list[tuple[str, int]] = []
        for tmpl, count in sorted(
            d_votes.items(),
            key=lambda kv: (-kv[1], _default_rank.get(kv[0], 999)),
        ):
            ordered.append((tmpl, count))
        seen = {t for t, _ in ordered}
        for tmpl in _DEFAULT_TEMPLATE_ORDER:
            if tmpl not in seen:
                ordered.append((tmpl, 0))
        return ordered

    by_addr: dict[str, list[Source]] = {}
    by_addr_match_count: dict[str, int] = {}

    # Emit candidates: outer loop domains (smaller set) × inner template order
    # × innermost name variants.
    for domain in domains_normalized:
        templates_for_domain = template_order_for(domain)
        knowns_for_domain = sum(votes.get(domain, {}).values())
        for tmpl_name, vote_count in templates_for_domain:
            for variant in variants:
                templates = localpart_templates(variant.first, variant.last)
                local = templates.get(tmpl_name)
                if not local:
                    continue
                addr = f"{local}@{domain}"
                if addr in by_addr:
                    # Already emitted (different variant produced same string).
                    # Increment the match count if this came from an inferred winner.
                    if vote_count > 0:
                        by_addr_match_count[addr] = max(
                            by_addr_match_count.get(addr, 0), vote_count,
                        )
                    continue
                if vote_count > 0:
                    detail = (
                        f"matches company pattern '{tmpl_name}' "
                        f"corroborated by {vote_count} known address"
                        f"{'es' if vote_count > 1 else ''}"
                    )
                    by_addr_match_count[addr] = vote_count
                elif knowns_for_domain > 0:
                    # We had inference signal for this domain, but THIS template
                    # didn't win the vote. Mark it as a runner-up.
                    detail = f"generic template '{tmpl_name}' (other patterns matched on {domain})"
                else:
                    detail = f"generic template '{tmpl_name}' (no company pattern inferred)"
                src = Source(
                    type="pattern",
                    url=None,
                    observed_at=start,
                    detail=detail,
                )
                by_addr[addr] = [src]

                if len(by_addr) >= cap:
                    break
            if len(by_addr) >= cap:
                break
        if len(by_addr) >= cap:
            break

    # Order: inferred-winner candidates first (by match count desc), then
    # default-order candidates. This is the renderer's natural top-down read.
    def _sort_key(addr_srcs: tuple[str, list[Source]]) -> tuple[int, int]:
        addr, _srcs = addr_srcs
        match_count = by_addr_match_count.get(addr, 0)
        return (-match_count, 0)

    candidates = [
        EmailCandidate(address=addr, sources=srcs)
        for addr, srcs in sorted(by_addr.items(), key=_sort_key)
    ]
    elapsed = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    status = "ok" if candidates else "empty"
    return ResolverResult(
        resolver="pattern_gen",
        candidates=candidates,
        status=status,
        elapsed_ms=elapsed,
        error_detail=None if candidates else "no candidates generated",
    )
