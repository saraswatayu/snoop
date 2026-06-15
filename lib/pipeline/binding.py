"""Identity binding — the stranger-proofing core.

Two distinct questions, both answered here, both pure (no I/O):

  1. Did we find the right *person* at all?  `bound_github_handle` reads
     Person.bound_anchors (set by person_resolve) to decide whether a GitHub
     handle is trustworthy enough to fan out on — a bare `github_handle_exists`
     is an untrusted hint, defended against host hallucination.

  2. Does THIS *address* belong to that person?  `candidate_is_bound` — the
     ≥2-independent-signals rule. SMTP fires only on bound candidates, so this
     is what stops snoop from opening a socket to a same-named stranger.

Extracted from snoop.py. Unit-tested in tests/pipeline/test_binding.py.
"""

from __future__ import annotations

from lib.normalize import name_match
from lib.schema import EmailCandidate, Person


# ENG-8 Phase-1 — candidate binding (identity binds before deliverability spends).
# Surfaces whose identity is the target's GitHub account: an address observed on
# one of these is an anchored observation ONLY when the handle itself bound.
GITHUB_SURFACES = frozenset({"git_commit", "gh_profile", "gh_readme", "github_repo"})
# Surfaces tied to a personal domain the target owns.
DOMAIN_SURFACES = frozenset({"personal_site", "whois"})


def bound_github_handle(person: Person) -> str | None:
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


def github_identity_bound(person: Person) -> bool:
    """True when a VALIDATING github anchor bound the handle (name/employer/
    personal-domain match) — not merely that the handle exists. A bare
    `github_handle_exists` is an untrusted hint and does not anchor an address."""
    return any(t.startswith("github") and t != "github_handle_exists"
               for t, _ in person.bound_anchors)


def verified_personal_domains(person: Person) -> set[str]:
    """Personal domains proven to belong to the target by a bidirectional rel=me
    (the IndieAuth self-attestation). This is the rel=me identity signal."""
    return {str(v).lower() for t, v in person.bound_anchors
            if t == "personal_domain_verified"}


def anchored_surface_domains(person: Person) -> set[str]:
    """Personal domains trusted enough that a reading observed ON them is an
    anchored observation — rel=me-verified domains plus a github blog/domain
    that matched a declared personal domain."""
    doms = verified_personal_domains(person)
    doms |= {str(v).lower() for t, v in person.bound_anchors
             if t == "github_personal_domain_match"}
    return doms


def bind_context(person: Person) -> tuple[set[str], set[str], bool]:
    """The three person-level invariants candidate_is_bound reads — the rel=me
    domains, the anchored-surface domains, and whether the GitHub identity is
    bound. Computed once and reused across a batch of candidates (each scans
    person.bound_anchors, which doesn't change per candidate)."""
    return (verified_personal_domains(person),
            anchored_surface_domains(person),
            github_identity_bound(person))


def candidate_is_bound(c: EmailCandidate, person: Person,
                       *, ctx: tuple[set[str], set[str], bool] | None = None) -> bool:
    """ENG-8 Phase-1: does THIS ADDRESS belong to the target? (Distinct from
    Person.bound_anchors, which only says 'we found the right person at all.')

    A candidate binds when ≥2 INDEPENDENT evidence classes agree on it:
      1. anchored observation — a real (non-pattern) source on a surface whose
         identity is bound: a GitHub surface when the handle bound, or a
         personal_site/whois reading on a bound personal domain;
      2. employer_match — the address domain is the resolved current employer's;
      3. rel=me ownership — the address domain is a bidirectionally-verified
         personal domain;
      4. PGP owner-UID — keys.openpgp.org returned a key whose UID is this address.

    A `manual_known` source (the --verify / --known lane: the user supplied the
    address AS the subject) short-circuits to bound. Binding requires ≥2 signals
    AND at least one IDENTITY-BEARING signal — an anchored observation (1) or
    rel=me ownership (3) — because those alone tie the address to THIS person.
    employer_match (2) and PGP owner-UID (4) are CORROBORATING but target-
    agnostic: a domain belongs to the employer, a key proves someone controls
    the inbox — neither says it is the target. So two corroborating signals
    (employer + PGP) never bind, and snoop will not open a socket to a possible
    namesake's mailbox on the strength of a name×domain template that merely
    landed on the employer domain and carried a published key.
    """
    if "@" not in c.address:
        return False
    source_types = {s.type for s in c.sources}
    if "manual_known" in source_types:
        return True
    domain = c.address.rsplit("@", 1)[1].lower()
    # Person-level invariants don't vary across candidates; when binding a whole
    # batch the caller hoists them once via bind_context and passes them in so
    # they aren't rescanned per candidate.
    rel_me_domains, surface_domains, github_bound = ctx or bind_context(person)

    on_github_surface = github_bound and bool(source_types & GITHUB_SURFACES)
    on_owned_domain_surface = (
        bool(source_types & DOMAIN_SURFACES) and domain in surface_domains
    )
    anchored = on_github_surface or on_owned_domain_surface  # 1. anchored obs
    rel_me_owned = domain in rel_me_domains                  # 3. rel=me ownership
    identity_bearing = anchored or rel_me_owned

    signals = 0
    if anchored:
        signals += 1
    if c.employer_match:
        signals += 1                                  # 2. employer (corroborating)
    if rel_me_owned:
        signals += 1
    if "pgp" in source_types:
        signals += 1                                  # 4. PGP UID (corroborating)
    return signals >= 2 and identity_bearing


def reassess_identity(person: Person, candidates: list[EmailCandidate]) -> None:
    """Promote identity confidence using the probe verdicts.

    person_resolve runs BEFORE the Google/SMTP probes and only knows how to bind
    identity from a validated GitHub handle — so without a handle it defaults to
    `insufficient_identity_evidence` and never sees the strongest identity signal
    snoop can produce: a Google account that is `verified` AND whose display name
    matches the target. That is genuine identity binding (existence + name), so
    when exactly one verified candidate name-matches, promote to
    `single_plausible_match` and record the anchor. Only acts on the
    not-yet-bound state; a declared `multiple_plausible_matches` (real namesake)
    is never auto-promoted."""
    if person.ambiguity != "insufficient_identity_evidence" or not person.name:
        return
    name_matched = [
        c for c in candidates
        if c.account_exists == "verified" and c.account_display_name
        and name_match(c.account_display_name, person.name)
    ]
    if len(name_matched) == 1:
        c = name_matched[0]
        person.ambiguity = "single_plausible_match"
        person.bound_anchors.append(("google_name_match", str(c.account_display_name)))
        person.notes.append(
            f"identity promoted to single_plausible_match: Google account "
            f"{c.address} is verified with display name "
            f"'{c.account_display_name}' matching the target"
        )
