"""Probe eligibility — which candidates are worth which Phase-2 probe.

ENG-9 splits the Phase-2 gate by probe class:
  - SMTP opens a socket to the target's mailbox, so `smtp_candidates` returns only
    candidates the caller has already bound (ENG-8) — snoop never RCPT-probes a
    namesake.
  - The Google People API existence check is an authed call through the user's own
    cookies (no socket), and the only disambiguator on a catch-all Workspace tenant,
    so `google_account_candidates` / `speculative_google_candidates` MAY include
    unbound pattern guesses on a Google-hosted domain.

Pure selection predicates only — the threaded probe *harness* that runs the actual
fetches stays in snoop.py (it calls monkeypatched sensor functions). `is_google_hosted`
is injected as a default so the one DNS-touching helper here stays stubbable.

Extracted from snoop.py. Unit-tested in tests/pipeline/test_probes.py.
"""

from __future__ import annotations

from typing import Callable

from lib.normalize import is_personal_provider
from lib.pipeline.candidates import primary_localparts, probe_rank, speculative_rank
from lib.schema import EmailCandidate, Person
from lib.verify_smtp import is_google_hosted


_GOOGLE_NATIVE_DOMAIN = "google.com"
# ENG-9 cap on the unbound Google existence-probe burst — a name-variant blowup
# can't spend the whole per-domain daily budget on one target.
_SPECULATIVE_GOOGLE_CAP = 12


def google_target_domains(workspace_domains: list[str]) -> set[str]:
    """Domains worth probing via the Google People API. Always includes the
    literal google.com; user can add Workspace tenant domains explicitly."""
    domains = {_GOOGLE_NATIVE_DOMAIN}
    for d in workspace_domains or []:
        if isinstance(d, str) and d.strip():
            domains.add(d.strip().lower())
    return domains


def autodetect_workspace_domains(
    candidates: list[EmailCandidate],
    explicit: list[str],
    *,
    is_google_hosted_fn: Callable[[str], bool] = is_google_hosted,
) -> list[str]:
    """Find candidate domains whose MX is Google Workspace and add them to
    the explicit list. Skips google.com (already included) and personal
    providers (gmail.com etc. — these ARE Google MX but probing arbitrary
    personal addresses is invasive and wrong-scope for this tool).

    One DNS lookup per unique non-skip candidate domain. Returns the merged
    list; the caller threads it to google_target_domains.

    Function injection on is_google_hosted_fn lets tests stub the MX
    lookup without monkeypatching the verify_smtp module globally.
    """
    explicit_set = {d.strip().lower() for d in explicit or [] if isinstance(d, str)}
    seen_candidate_domains: set[str] = set()
    additions: list[str] = []
    for c in candidates:
        if "@" not in c.address:
            continue
        d = c.address.rsplit("@", 1)[1].lower()
        if d in seen_candidate_domains or d in explicit_set or d == _GOOGLE_NATIVE_DOMAIN:
            continue
        seen_candidate_domains.add(d)
        if is_personal_provider(d):
            continue
        if is_google_hosted_fn(d):
            additions.append(d)
    return list(explicit or []) + additions


def google_account_candidates(
    candidates: list[EmailCandidate],
    workspace_domains: list[str],
) -> list[EmailCandidate]:
    """Filter candidates to those on Google-hosted domains worth probing.
    Skip candidates that already have an account_exists verdict (don't
    re-probe within one invocation). Ordered by probe_rank so observation-
    backed candidates probe first — on a multi-user Workspace tenant a pattern
    guess that hits someone else's real account shouldn't short-circuit probing
    of an observed address that hasn't been tried yet.
    """
    domains = google_target_domains(workspace_domains)
    out: list[EmailCandidate] = []
    for c in candidates:
        if not c.address or "@" not in c.address:
            continue
        if c.account_exists != "unprobed":
            continue
        domain = c.address.rsplit("@", 1)[1].lower()
        if domain in domains:
            out.append(c)
    out.sort(key=probe_rank)
    return out


def speculative_google_candidates(
    candidates: list[EmailCandidate],
    bound_addrs: set[str],
    workspace_domains: list[str],
    person: Person,
) -> list[EmailCandidate]:
    """ENG-9: unbound candidates on a Google-hosted domain, eligible for the Google
    existence check (but NOT SMTP). Excludes already-bound addresses (those probe
    via the bound path), anything already carrying an account_exists verdict, and —
    to keep the burst within the Phase-2 deadline — the reversed-order name guesses
    (only the primary name parse's local-parts probe speculatively). Ordered by
    template plausibility, capped at _SPECULATIVE_GOOGLE_CAP."""
    domains = google_target_domains(workspace_domains)
    allowed_localparts = primary_localparts(person.name)
    out: list[EmailCandidate] = []
    seen: set[str] = set()
    for c in candidates:
        if not c.address or "@" not in c.address:
            continue
        if c.address in bound_addrs or c.address in seen:
            continue
        if c.account_exists != "unprobed":
            continue
        local, _, domain = c.address.partition("@")
        domain = domain.lower()
        if domain not in domains:
            continue
        if allowed_localparts is not None and local.lower() not in allowed_localparts:
            continue
        seen.add(c.address)
        out.append(c)
    out.sort(key=speculative_rank)
    return out[:_SPECULATIVE_GOOGLE_CAP]


def smtp_candidates(candidates: list[EmailCandidate], top_k: int = 5) -> list[EmailCandidate]:
    """Pick the top candidates worth SMTP-probing: non-personal-provider, at
    least one source, not already known-dead via Google. Ordered by probe_rank
    (observed addresses before pure guesses).

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
    eligible.sort(key=probe_rank)
    return eligible[:top_k]
