"""lib/reachability.py — aggregate OBSERVED public reachability channels.

A deterministic, no-network transform. It takes a resolved Person plus the
person's email candidates and emits typed Channel contributions: the email
address, an open-DM signal, a LinkedIn URL, a Calendly link, a personal
website, a contact form. Each Channel carries provenance (sources) and is
bound to the identity via lib.binding so the renderer never attributes a
channel we cannot tie to this person.

IMPORTANT framing (outside-voice finding): we render OBSERVED channels with
their evidence. We do NOT claim to know the single "best way in". The
`rank_hint` is an evidence-based ordering signal, not a promise that a higher
rank reaches the person faster. It simply puts the better-evidenced channels
first (e.g. a verified email above a "DMs open" bio claim).

Channels whose sources do not bind to the identity (tier "unbound") are
dropped, not rendered: the same iron rule that governs the rest of the
pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .binding import bind_and_keep
from .schema import (
    Channel,
    EmailCandidate,
    Person,
    ResolverResult,
    Source,
)
from .score import is_personal_provider

# Base ordering weights for the OBSERVED channels. Higher == surfaced first.
# These encode an evidence-based preference, not a delivery guarantee: a
# verified email beats an unverified "DMs open" bio claim, which beats a
# self-linked LinkedIn, and so on. Email's rank also rides on its
# belongs_to_person score so a weak email does not outrank a strong DM signal.
_RANK_BASE: dict[str, float] = {
    "email": 0.90,
    "x_dm": 0.70,
    "linkedin": 0.55,
    "calendly": 0.40,
    "website": 0.25,
    "contact_form": 0.20,
}

# An email channel's [+] marker asserts the address belongs to the person.
# Below this ownership score (or when the scorer abstained) the marker is capped
# to [?] so source provenance alone can never claim "asserted" for a weak/nil
# belongs (red-team I3).
_BELONGS_FLOOR = 0.5


def collect_channels(
    person: Person,
    emails: list[EmailCandidate] | None = None,
    *,
    now: datetime | None = None,
) -> ResolverResult:
    """Build bound Channel contributions for a person from observed signals.

    Args:
        person: The resolved identity. `channel_hints` and `gh_twitter` are read.
        emails: Email candidates already found; the top one (by
            belongs_to_person, None treated as 0) becomes an "email" channel
            and reuses that candidate's own sources so binding reflects how
            the address was found.
        now: For deterministic tests; defaults to datetime.now(timezone.utc).

    Returns:
        ResolverResult(resolver="reachability", candidates=[], contributions=[
            ...Channel sorted by rank_hint descending (stable)], status="ok"
            if any channel survived binding else "empty").
    """
    start = datetime.now(timezone.utc) if now is None else now
    contributions: list[Channel] = []
    email_weak = False  # I3: cap the email channel's tier when ownership is nil

    # --- email: take the strongest candidate by belongs_to_person ---
    if emails:
        top = max(emails, key=lambda c: c.belongs_to_person or 0.0)
        belongs = top.belongs_to_person or 0.0
        email_weak = belongs < _BELONGS_FLOOR
        # I2: a personal-provider address surfaced as an alternate channel
        # carries the same caveat the lead uses, so a work-intent card never
        # presents a personal address as if it were a vetted work channel.
        domain = top.address.rsplit("@", 1)[-1]
        personal = is_personal_provider(domain)
        if top.smtp_verdict == "verified":
            base_evidence = "SMTP verified"
        elif top.belongs_to_person is None:
            base_evidence = "ownership unscored"
        else:
            base_evidence = f"belongs={belongs:g}"
        evidence = f"personal address; {base_evidence}" if personal else base_evidence
        # rank_hint rides on belongs so a weak email cannot outrank a strong
        # non-email signal (it may fall below x_dm — that is intended).
        rank = _RANK_BASE["email"] - (1.0 - belongs) * 0.25
        contributions.append(Channel(
            channel_type="email",
            value=top.address,
            evidence=evidence,
            rank_hint=rank,
            sources=list(top.sources),
        ))

    hints = person.channel_hints or {}

    # --- x_dm: an explicit "DMs open" bio claim ---
    if hints.get("x_dms_open") is True:
        value = ("@" + person.gh_twitter) if person.gh_twitter else "(x handle unknown)"
        contributions.append(Channel(
            channel_type="x_dm",
            value=value,
            evidence="X bio says DMs open",
            rank_hint=_RANK_BASE["x_dm"],
            sources=[Source(
                type="channel_hint",
                url=None,
                observed_at=start,
                detail="x_dms_open",
            )],
        ))

    # --- url-shaped hints: linkedin / calendly / website / contact_form ---
    for key in ("linkedin", "calendly", "website", "contact_form"):
        value = hints.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        contributions.append(Channel(
            channel_type=key,
            value=value,
            evidence=None,
            rank_hint=_RANK_BASE.get(key, 0.10),
            sources=[Source(
                type="channel_hint",
                url=value,
                observed_at=start,
                detail=f"channel_hint:{key}",
            )],
        ))

    # --- bind each channel; drop any that do not tie to this person ---
    bound = bind_and_keep(contributions, person)

    # I3: a weak/nil ownership score must not let the email channel show [+].
    # Source provenance (where the address was seen) can bind "asserted", but the
    # email channel's marker is read as "this address belongs to the person" —
    # cap it to "possibly" when belongs is below the floor or unscored.
    if email_weak:
        for ch in bound:
            if ch.channel_type == "email" and ch.bind_tier == "asserted":
                ch.bind_tier = "possibly"
                ch.bind_reasons = [
                    *ch.bind_reasons,
                    "ownership score weak/unscored; email channel capped to possibly",
                ]

    # Stable sort by rank_hint descending. None ranks sink to the bottom.
    bound.sort(key=lambda c: (c.rank_hint if c.rank_hint is not None else -1.0),
               reverse=True)

    elapsed = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    return ResolverResult(
        resolver="reachability",
        candidates=[],
        status="ok" if bound else "empty",
        elapsed_ms=elapsed,
        contributions=bound,
    )
