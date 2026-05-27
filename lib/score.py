"""lib/score.py — provenance-aware 3-field scorer.

Per Codex finding #7 (post-dual-voice review): the old single-decimal
score blended unlike things — ownership, currentness, deliverability —
into one number and called it "confidence." That was a fake probability.
Three independent fields keep abstention explicit and let the renderer
present each dimension separately.

  belongs_to_person      0-1 | None : is this actually this person's address?
  current_work_address   0-1 | None : is this their current work email?
  deliverable            0-1 | None : will a message sent here reach them?

None means "no signal supports a verdict in either direction" — not
"measured and zero." Renderer should display abstained fields differently
(e.g. "—" or "not evaluated") from measured-bad fields (e.g. "0.05 — RCPT
rejected").

Every weight in SOURCE_WEIGHTS is hand-picked and explainable. No ML.
No calibration data. If you change one, update the comment with the
reason. The calibration-fixture work (planned post-P1) will provide a
labeled ground truth to retune against; until then these are defensible
defaults, not probabilities.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from .schema import EmailCandidate, Person, Source


# ---- source weights ---------------------------------------------------------

# Each weight is the BASE belongs_to_person score for a candidate whose
# sole source is of that type. Corroboration from additional non-pattern
# sources adds on top (see _score_belongs).
SOURCE_WEIGHTS: dict[str, float] = {
    "manual_known":     0.65,  # user-supplied ground truth — highest trust
    "git_commit":       0.55,  # demonstrably used; age-decayed below
    "gh_profile":       0.55,  # explicit public profile email
    "hn_profile":       0.55,  # explicit HN public profile email
    "linkedin":         0.50,  # explicit profile field (cookie-jar gated)
    "package_registry": 0.50,  # publisher self-identifies (npm requires verified)
    "personal_site":    0.50,  # mailto: anchor on their own site
    "pgp":              0.45,  # cryptographic UID claim
    "gh_readme":        0.45,  # scraped from profile README — lower extraction trust
    "substack":         0.45,  # publication metadata
    "x_bio":            0.45,  # often press/PR alias
    "whois":            0.40,  # rare hit but high-trust when present
    "pattern":          0.15,  # guess; corroboration multiplies
    "smtp":             0.0,   # not a source by itself — modifies deliverable only
}

# Generic-but-legitimate localparts: hello@/info@/contact@/team@ are often
# real personal addresses on one-person consultancies, but ALSO often
# shared inboxes. Downrank belongs_to_person when these appear; the user's
# context (sales vs friend-lookup) determines whether this matters.
_GENERIC_LOCALPARTS = frozenset({
    "info", "hello", "contact", "team", "press", "media",
    "support", "sales", "admin", "hi", "say-hi", "sayhi",
})

# Personal-provider domains — addresses on these are NEVER work addresses.
PERSONAL_PROVIDER_DOMAINS = frozenset({
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.co.uk", "ymail.com",
    "icloud.com", "me.com", "mac.com",
    "outlook.com", "hotmail.com", "live.com",
    "protonmail.com", "proton.me", "pm.me",
    "fastmail.com", "fastmail.fm",
    "aol.com", "duck.com", "hey.com",
})


def is_personal_provider(domain: str) -> bool:
    return domain.lower() in PERSONAL_PROVIDER_DOMAINS


# ---- helpers ----------------------------------------------------------------


def _age_decay(observed_at: datetime, now: datetime) -> float:
    """Decay factor for git_commit recency.

    1.0 at observation; linear decay to 0.4 at 365d; floor at 0.4.
    """
    age_days = max(0, (now - observed_at).days)
    return max(0.4, 1.0 - age_days / 365)


def _localpart(addr: str) -> str:
    return addr.split("@", 1)[0] if "@" in addr else ""


def _domain(addr: str) -> str:
    return addr.split("@", 1)[1] if "@" in addr else ""


# ---- belongs_to_person ------------------------------------------------------


def _score_belongs(
    c: EmailCandidate,
    person: Person,
    now: datetime,
) -> tuple[float | None, list[str]]:
    """How confident is this address owned by `person`?"""
    if not c.sources:
        return None, []

    reasons: list[str] = []

    # Base: best single source weight, with age decay for git_commit
    best = 0.0
    best_source: Source | None = None
    for s in c.sources:
        w = SOURCE_WEIGHTS.get(s.type, 0.1)
        if s.type == "git_commit":
            w *= _age_decay(s.observed_at, now)
        if w > best:
            best = w
            best_source = s

    base = best
    if best_source is not None:
        reasons.append(f"primary: {best_source.detail}")

    # Corroboration: each additional non-pattern source adds, capped at +0.20
    non_pattern = [s for s in c.sources if s.type != "pattern"]
    if len(non_pattern) > 1:
        bonus = min(0.20, 0.08 * (len(non_pattern) - 1))
        base += bonus
        reasons.append(
            f"corroborated by {len(non_pattern)} independent sources (+{bonus:.2f})"
        )

    # Generic-localpart downrank
    local = _localpart(c.address).lower()
    if local in _GENERIC_LOCALPARTS:
        old = base
        base = min(base, 0.35)
        if base < old:
            reasons.append(
                f"generic localpart '{local}' — may be a shared inbox (capped at 0.35)"
            )

    # Pattern-only candidates capped (no observational evidence)
    if len(non_pattern) == 0:
        base = min(base, 0.30)
        reasons.append("pattern-only candidate — no observational evidence (capped)")

    return round(min(1.0, base), 2), reasons


# ---- current_work_address ---------------------------------------------------


_FRESH_SOURCE_TYPES = frozenset({
    "gh_profile", "hn_profile", "linkedin", "manual_known",
    "x_bio", "personal_site", "gh_readme", "substack", "package_registry",
})


def _has_fresh_signal(sources: Iterable[Source], now: datetime,
                     fresh_window_days: int = 90) -> bool:
    """True if any source represents a snapshot ≤ fresh_window_days old."""
    for s in sources:
        if s.type == "git_commit":
            if (now - s.observed_at).days <= fresh_window_days:
                return True
        elif s.type in _FRESH_SOURCE_TYPES:
            # These are current-snapshot sources; always fresh if recently observed.
            if (now - s.observed_at).days <= fresh_window_days:
                return True
    return False


def _score_current_work(
    c: EmailCandidate,
    person: Person,
    now: datetime,
    belongs: float | None,
) -> tuple[float | None, list[str]]:
    """How confident is this a current work email for `person`?"""
    reasons: list[str] = []

    if c.is_personal_provider:
        return 0.0, ["personal-provider domain — not a work address"]

    if c.employer_former_match:
        # Former employer: cap hard. Even if the address was their work email
        # a year ago, it likely bounces or routes to "no longer here."
        return 0.30, ["former-employer domain — likely inactive"]

    # No current-employer info → abstain
    if person.employer is None or not person.employer.domains:
        return None, ["no resolved current employer; cannot evaluate"]

    if not c.employer_match:
        return None, ["domain doesn't match resolved current employer"]

    # OK: it's a current-employer address. Strength depends on whether we have
    # observation evidence vs pure-pattern.
    if belongs is None or belongs <= 0:
        return None, ["no belongs_to_person signal; cannot evaluate"]

    has_observed = any(s.type != "pattern" for s in c.sources)
    if not has_observed:
        # Pattern-only on the right domain: we believe the company format,
        # but no evidence this person uses this specific address.
        score = round(min(0.40, belongs + 0.25), 2)
        reasons.append("current-employer domain + pattern match (no observation)")
        return score, reasons

    if _has_fresh_signal(c.sources, now):
        # Strong: current-employer domain with a recent observational source.
        score = round(min(1.0, belongs + 0.10), 2)
        reasons.append("current-employer domain + fresh observational source")
        return score, reasons

    # Current-employer domain but no fresh signal: lukewarm.
    score = round(belongs * 0.85, 2)
    reasons.append("current-employer domain but no recent observation")
    return score, reasons


# ---- deliverable ------------------------------------------------------------


def _score_deliverable(c: EmailCandidate) -> tuple[float | None, list[str]]:
    """Will a message sent here reach someone?

    Driven by SMTP verdict. Per Codex c1, inconclusive is zero information
    (None), not a low confidence floor.
    """
    if c.smtp_verdict == "verified":
        return 0.85, ["SMTP RCPT accepted on non-catch-all domain"]
    if c.smtp_verdict == "invalid":
        return 0.05, ["SMTP RCPT rejected"]
    if c.smtp_verdict == "catch_all":
        return 0.40, [
            "catch-all domain — mail accepted but mailbox cannot be confirmed"
        ]
    if c.smtp_verdict == "inconclusive":
        provider_hint = f" ({c.mx_provider})" if c.mx_provider else ""
        return None, [
            f"SMTP inconclusive{provider_hint} — provider blocks RCPT; "
            f"verdict carries zero information either way"
        ]
    # "unprobed" — never tested. Could be deliberate (personal provider,
    # budget exhausted) or just not yet scored.
    if c.is_personal_provider:
        return None, [
            "not probed (personal-provider domain) — major providers are "
            "generally deliverable IF the mailbox exists; we have no evidence either way"
        ]
    return None, ["not probed"]


# ---- public API -------------------------------------------------------------


def score_candidate(
    c: EmailCandidate,
    person: Person,
    *,
    now: datetime | None = None,
) -> EmailCandidate:
    """Mutate `c` in place: set all 3 score fields and score_reasons.

    Returns `c` for chainable use.
    """
    now = now or datetime.now(timezone.utc)

    # Auto-detect employer/personal-provider flags if the pipeline didn't
    # set them (lets score_candidate work standalone in tests).
    dom = _domain(c.address).lower()
    if not c.is_personal_provider and is_personal_provider(dom):
        c.is_personal_provider = True
    if not c.employer_match and person.employer and dom in [
        d.lower() for d in person.employer.domains
    ]:
        c.employer_match = True
    if not c.employer_former_match:
        for fe in person.former_employers:
            if dom in [d.lower() for d in fe.domains]:
                c.employer_former_match = True
                break

    belongs, belongs_reasons = _score_belongs(c, person, now)
    current_work, work_reasons = _score_current_work(c, person, now, belongs)
    deliv, deliv_reasons = _score_deliverable(c)

    c.belongs_to_person = belongs
    c.current_work_address = current_work
    c.deliverable = deliv
    c.score_reasons = belongs_reasons + work_reasons + deliv_reasons
    return c


def score_all(
    candidates: list[EmailCandidate],
    person: Person,
    *,
    now: datetime | None = None,
) -> list[EmailCandidate]:
    """Score every candidate and return them sorted by belongs_to_person desc.

    Candidates with None belongs_to_person sort to the end. Ties broken by
    candidate address for determinism.
    """
    for c in candidates:
        score_candidate(c, person, now=now)
    # Use -1.0 as the None sentinel for sorting (puts None values last).
    def _key(c: EmailCandidate) -> tuple[float, str]:
        b = c.belongs_to_person if c.belongs_to_person is not None else -1.0
        return (-b, c.address)
    return sorted(candidates, key=_key)
