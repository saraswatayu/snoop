"""lib/consistency_notes.py — text-only identity-consistency notes.

The narrowed "anti-catfish" feature (outside-voice Codex #4): a DETERMINISTIC,
NO-NETWORK, TEXT-ONLY cross-check of what we resolved against what the GitHub
profile says. NO photo / image / reverse-image matching — that is biometric
scope creep, and a false "suspicious" label on a real person is higher harm
than any email miss.

A note is neutral evidence, never an accusation:
  - severity "info"     : fields agree, or differ only as a diminutive
                          (e.g. plan "Dan" vs profile "Daniel Neil").
  - severity "mismatch" : fields genuinely disagree (e.g. plan employer
                          "OpenAI" vs profile company "Anthropic").

Notes derive from the person's own resolved fields cross-checked against the
GitHub profile surface, so they bind off that surface (asserted when the handle
is validated, possibly otherwise). Unbound notes are dropped.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .binding import bind_and_keep
from .schema import ConsistencyNote, Person, ResolverResult, Source

_ORG_SUFFIXES = {"inc", "llc", "ltd", "corp", "co", "company", "gmbh", "ag", "sa"}


def _first(name: str) -> str:
    parts = name.strip().split()
    return parts[0].lower() if parts else ""


def _name_note(person: Person) -> str | None:
    target = (person.name or "").strip()
    observed = (person.gh_name or "").strip()
    if not target or not observed or observed.lower() == target.lower():
        return None
    ft, fo = _first(target), _first(observed)
    if ft and fo and ft != fo and fo.startswith(ft):
        return f'INFO: you said "{ft.title()}", github profile says "{observed}" (diminutive, consistent)'
    if ft != fo:
        return f'MISMATCH: github profile name "{observed}" differs from input "{target}"'
    return None


def _company_tokens(s: str) -> set[str]:
    raw = s.lower().lstrip("@").replace(",", " ").replace(".", " ").split()
    return {t for t in raw if t and t not in _ORG_SUFFIXES}


def _employer_note(person: Person) -> str | None:
    if not person.employer or not person.employer.name or not person.gh_company:
        return None
    plan = _company_tokens(person.employer.name)
    obs = _company_tokens(person.gh_company)
    if not plan or not obs:
        return None
    if plan.issubset(obs) or obs.issubset(plan):
        return None  # consistent — nothing to flag
    return (f'MISMATCH: plan employer "{person.employer.name}" vs github company '
            f'"{person.gh_company}"')


def collect_consistency_notes(
    person: Person, *, now: datetime | None = None
) -> ResolverResult:
    """Emit bound, text-only identity-consistency notes. Drops unbound."""
    start = datetime.now(timezone.utc) if now is None else now
    notes: list[ConsistencyNote] = []

    for text in (_name_note(person), _employer_note(person)):
        if not text:
            continue
        severity = "mismatch" if text.startswith("MISMATCH") else "info"
        notes.append(ConsistencyNote(
            note=text,
            severity=severity,
            sources=[Source(
                type="gh_profile", url=None, observed_at=start,
                detail="identity cross-check vs github profile",
            )],
        ))

    bound = bind_and_keep(notes, person)

    elapsed = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    return ResolverResult(
        resolver="consistency_notes",
        candidates=[],
        status="ok" if bound else "empty",
        elapsed_ms=elapsed,
        contributions=bound,
    )
