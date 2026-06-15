"""Candidate algebra — clustering, ranking, and name×domain template logic.

Pure functions over EmailCandidate lists: merge duplicate addresses across
resolvers, order candidates so observed addresses lead pure pattern guesses, and
rank the speculative name-template set by plausibility. No I/O.

Extracted from snoop.py. Unit-tested in tests/pipeline/test_candidates.py.
"""

from __future__ import annotations

import re

from lib.normalize import localpart_templates, parse_name
from lib.pattern_gen import _DEFAULT_TEMPLATE_ORDER
from lib.schema import EmailCandidate, ResolverResult


_TEMPLATE_RANK = {t: i for i, t in enumerate(_DEFAULT_TEMPLATE_ORDER)}


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


def probe_rank(c: EmailCandidate) -> tuple:
    """Tiebreak ORDERING within the bound set (ENG-8): rank by whether the address
    was actually observed (any non-pattern source) over a pure name×domain guess,
    then by how many sources corroborate it, then address for determinism. Since
    the ENG-8 gate now decides WHICH candidates are eligible to probe at all, this
    only sequences the survivors so an observed address is tried first; it is no
    longer a ranking that could let an unbound guess be probed."""
    observed = any(s.type != "pattern" for s in c.sources)
    return (0 if observed else 1, -len(c.sources), c.address)


def primary_localparts(name: str) -> set[str] | None:
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


def pattern_template(c: EmailCandidate) -> str | None:
    """Extract the pattern template name (e.g. 'first', 'flast') from a candidate's
    pattern Source detail, so the speculative set can be ranked by template
    plausibility rather than alphabetically — `first@` for a rare first name must
    stay reachable within the cap, even though `first` is a low-popularity template."""
    for s in c.sources:
        m = re.search(r"(?:template|pattern) '([^']+)'", s.detail or "")
        if m:
            return m.group(1)
    return None


def speculative_rank(c: EmailCandidate) -> tuple:
    """Order the unbound Google probe set: company-inferred winners first, then by
    template popularity, then address for determinism. Ensures the cap keeps the
    plausible patterns rather than slicing by alphabet."""
    detail = " ".join(s.detail or "" for s in c.sources)
    inferred = 0 if "matches company pattern" in detail else 1
    tmpl = pattern_template(c)
    rank = _TEMPLATE_RANK[tmpl] if tmpl in _TEMPLATE_RANK else len(_DEFAULT_TEMPLATE_ORDER)
    return (inferred, rank, c.address)
