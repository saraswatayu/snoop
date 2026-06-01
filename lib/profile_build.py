"""lib/profile_build.py — assemble a Profile from a resolved person + emails.

This is the merge step (D2): run the profile producers over the already-resolved
identity and scored email candidates, and dispatch every Contribution into the
right Profile section via Profile.add (dispatch-on-kind). It is a pure assembly
layer — the producers do the work and the binding, this just collects.

The email candidates are the original deliverable and seed Profile.emails
directly. The producers (social links, reachability, body of work, role context,
identity-consistency notes) each return a ResolverResult whose contributions are
already bound and unbound-dropped; we just route them.
"""

from __future__ import annotations

from datetime import datetime

from .consistency_notes import collect_consistency_notes
from .reachability import collect_channels
from .role_context import collect_role_context
from .schema import EmailCandidate, Person, Profile
from .social_links import collect_social_links
from .work_items import SearchFn, collect_work_items


def build_profile(
    person: Person,
    candidates: list[EmailCandidate],
    *,
    enable_search: bool = True,
    search_fn: SearchFn | None = None,
    now: datetime | None = None,
) -> Profile:
    """Assemble the full Profile deliverable.

    Args:
        person: the resolved identity (also exposed as Profile.identity).
        candidates: scored email candidates; seed Profile.emails.
        enable_search / search_fn: forwarded to the work_items producer.
        now: forwarded to producers for deterministic tests.

    Returns:
        Profile with emails plus every bound contribution from the producers
        routed into its section. Search/T8 notes (and any producer error_detail
        or crash) are appended to person.notes so the renderer can surface them.

    Robustness: each producer is isolated in its own try/except, mirroring the
    email fan-out's _run_resolver contract. A single producer raising (e.g. a
    malformed channel_hints value, an unexpected gh_recent_repos type, a
    host-model search_fn that throws) must NOT lose the whole profile — the
    email answer and the surviving sections still render. The failure is
    recorded as a note instead.
    """
    profile = Profile(identity=person, emails=list(candidates))

    producers = [
        ("social_links", lambda: collect_social_links(person, now=now)),
        ("reachability", lambda: collect_channels(person, candidates, now=now)),
        ("work_items", lambda: collect_work_items(
            person, enable_search=enable_search, search_fn=search_fn, now=now)),
        ("role_context", lambda: collect_role_context(person, now=now)),
        ("consistency_notes", lambda: collect_consistency_notes(person, now=now)),
    ]

    def _note(text: str) -> None:
        if text not in person.notes:
            person.notes.append(text)

    for name, run in producers:
        try:
            result = run()
        except Exception as exc:  # one bad producer must not lose the card
            _note(f"{name}: profile producer failed "
                  f"({type(exc).__name__}: {exc})")
            continue
        for contribution in result.contributions:
            profile.add(contribution)
        # Surface capability degradations (e.g. work_items' T8 note) once.
        if result.error_detail:
            _note(f"{result.resolver}: {result.error_detail}")

    return profile
