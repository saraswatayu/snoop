"""lib/role_context.py — employer / title / tenure facts.

A DETERMINISTIC, NO-NETWORK transform over the already-resolved Person. It emits
RoleFact contributions for the current employer and any former employers.

Provenance matters here (Codex #2): Person.employer comes from the
model-produced --person-plan and is an UNTRUSTED hint UNLESS the GitHub profile
corroborated it (a github_employer_match anchor). So:
  - corroborated current employer  -> Source(type="gh_profile") -> "asserted"
    when the handle is independently bound, else "possibly".
  - uncorroborated (plan-only)      -> Source(type="channel_hint") -> "possibly".
  - former employers (plan-only)    -> "possibly".

Company "why now" context (RoleFact.summary) needs a network fetch and is left
None for now: it depends on the same search-provider decision as work_items
(T8). Unbound facts are dropped.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .binding import bind_best
from .schema import Employer, Person, ResolverResult, RoleFact, Source


def _has_employer_anchor(person: Person) -> bool:
    return any(a[0] == "github_employer_match" for a in person.bound_anchors)


def _role_from_employer(
    employer: Employer, *, current: bool, corroborated: bool, now: datetime
) -> RoleFact:
    """Build a RoleFact. Corroborated current roles cite the github profile
    (so they can assert); everything else is a plan-declared hint."""
    if current and corroborated:
        src = Source(
            type="gh_profile", url=None, observed_at=now,
            detail="company field on github profile",
        )
    else:
        src = Source(
            type="channel_hint", url=None, observed_at=now,
            detail="employer declared in plan",
        )
    return RoleFact(
        employer=employer.name,
        since=employer.since,
        until=None if current else employer.until,
        summary=None,  # company "why now" context needs a fetch (T8) — deferred
        sources=[src],
    )


def collect_role_context(
    person: Person, *, now: datetime | None = None
) -> ResolverResult:
    """Emit bound RoleFact contributions for current + former employers.

    Returns ResolverResult(resolver="role_context", candidates=[],
    contributions=[...RoleFact], status "ok"/"empty"). Each RoleFact is bound
    via lib.binding; unbound facts are dropped.
    """
    start = datetime.now(timezone.utc) if now is None else now
    facts: list[RoleFact] = []

    if person.employer and person.employer.name:
        facts.append(_role_from_employer(
            person.employer, current=True,
            corroborated=_has_employer_anchor(person), now=start,
        ))

    for fe in person.former_employers:
        if fe and fe.name:
            facts.append(_role_from_employer(
                fe, current=False, corroborated=False, now=start,
            ))

    bound: list[RoleFact] = []
    for fact in facts:
        binding = bind_best(fact.sources, person)
        if binding.tier == "unbound":
            continue
        fact.bind_tier = binding.tier
        fact.bind_reasons = binding.reasons
        bound.append(fact)

    elapsed = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    return ResolverResult(
        resolver="role_context",
        candidates=[],
        status="ok" if bound else "empty",
        elapsed_ms=elapsed,
        contributions=bound,
    )
