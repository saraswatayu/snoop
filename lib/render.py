"""lib/render.py — contact decision card output.

Codex finding #2 reframed what /snoop's output should be: not a "person
card" but a "contact decision card." The user is trying to figure out
whether and how to reach someone, not just which strings exist. The
renderer surfaces:

  - Identity confidence (the 3-state ambiguity + bound anchor count)
  - Best contact-for-work-outreach with reasons-to-trust + caveats
  - Work alternatives ranked by current_work_address
  - Personal addresses (split from work; never recommended for cold business)
  - Channel hints (e.g. X DMs open) when email confidence is uniformly low
  - Resolver notes / plan-vs-observed deltas (Person.notes)
  - SMTP-inconclusive explanations ("M365 blocks RCPT — no information")

Three intent modes shape the output:
  - "work" (default): work addresses first, decision-line recommends
    a work option
  - "personal": personal addresses first, work shown as alternatives
  - "either": confidence-ranked combined, no work/personal split

The renderer NEVER labels a guess "Verified." A candidate without
belongs_to_person evidence > 0.6 always shows abstention markers
("—") instead of misleading scores.

Output format: markdown for chat-surface rendering. Plain prose for
the lead/recommendation; tables for the alternative rows.
"""

from __future__ import annotations

from typing import Iterable, Literal

from .schema import EmailCandidate, Person


Intent = Literal["work", "personal", "either"]


_AMBIGUITY_LABEL = {
    "single_plausible_match": "single plausible match",
    "multiple_plausible_matches": "multiple plausible matches — confirm before sending",
    "insufficient_identity_evidence": "insufficient identity evidence",
}


# ---- helpers ----------------------------------------------------------------


def _fmt_score(value: float | None) -> str:
    """Format a score field. None → '—' (abstention)."""
    if value is None:
        return "—"
    return f"{value:.2f}"


def _split_by_kind(
    candidates: Iterable[EmailCandidate],
) -> tuple[list[EmailCandidate], list[EmailCandidate], list[EmailCandidate]]:
    """Split into (work, personal, other) where:
      - work: employer_match AND not personal_provider
      - personal: is_personal_provider
      - other: neither — pattern candidates on personal_domains, etc.
    """
    work: list[EmailCandidate] = []
    personal: list[EmailCandidate] = []
    other: list[EmailCandidate] = []
    for c in candidates:
        if c.is_personal_provider:
            personal.append(c)
        elif c.employer_match:
            work.append(c)
        else:
            other.append(c)
    return work, personal, other


def _rank_for_work(c: EmailCandidate) -> float:
    """Sort key for the work column. Prefer high belongs × high current_work."""
    b = c.belongs_to_person or 0.0
    w = c.current_work_address if c.current_work_address is not None else 0.0
    return -(b * 0.6 + w * 0.4)


def _rank_for_personal(c: EmailCandidate) -> float:
    """Sort key for the personal column. Just belongs_to_person."""
    return -(c.belongs_to_person or 0.0)


def _short_provenance(c: EmailCandidate, max_parts: int = 3) -> str:
    """One-line 'found via' summary from sources. Most recent/highest-trust
    first; up to max_parts."""
    if not c.sources:
        return "no sources"
    parts = []
    for s in c.sources[:max_parts]:
        parts.append(s.detail)
    extra = len(c.sources) - max_parts
    if extra > 0:
        parts.append(f"+{extra} more")
    return "; ".join(parts)


# ---- card sections ----------------------------------------------------------


def _header_block(person: Person) -> list[str]:
    lines = [f"# {person.name}"]
    if person.employer:
        empl_line = f"at **{person.employer.name}**"
        if person.employer.domains:
            empl_line += f" ({', '.join(person.employer.domains)})"
        lines.append(empl_line)

    bound_validating = [
        a for a in person.bound_anchors if a[0] != "github_handle_exists"
    ]
    bound_summary = (
        f"{len(bound_validating)} identity anchor"
        f"{'s' if len(bound_validating) != 1 else ''} bound"
    )
    lines.append(
        f"_Identity: {_AMBIGUITY_LABEL[person.ambiguity]} "
        f"({bound_summary})_"
    )

    if person.notes:
        lines.append("")
        lines.append("**Resolver notes:**")
        for n in person.notes:
            lines.append(f"- {n}")
    return lines


def _decision_line(
    candidates: list[EmailCandidate],
    intent: Intent,
) -> list[str]:
    """The opinionated top recommendation. Codex finding #2: tell the user
    what to DO, not just what exists."""
    if not candidates:
        return [
            "## Decision",
            "No usable candidates found. Try the channel hints below "
            "or fall back to manual research.",
        ]

    work, personal, other = _split_by_kind(candidates)
    pick: EmailCandidate | None = None
    fell_back = False
    if intent == "work":
        if work:
            pick = sorted(work, key=_rank_for_work)[0]
        elif other:
            pick = sorted(other, key=_rank_for_personal)[0]
            fell_back = True
        elif personal:
            pick = sorted(personal, key=_rank_for_personal)[0]
            fell_back = True
    elif intent == "personal":
        if personal:
            pick = sorted(personal, key=_rank_for_personal)[0]
    else:  # either
        all_ranked = sorted(candidates, key=_rank_for_personal)
        pick = all_ranked[0] if all_ranked else None

    if pick is None:
        return [
            "## Decision",
            f"No candidates of intent type **{intent}** were found. "
            f"See alternatives below.",
        ]

    lines = ["## Decision"]
    # Confidence calibration: only label "high confidence" when belongs ≥ 0.7
    # AND at least one non-pattern source.
    has_observed = any(s.type != "pattern" for s in pick.sources)
    high_belief = (pick.belongs_to_person or 0) >= 0.7 and has_observed

    if intent == "work" and fell_back:
        lines.append(
            f"No current-employer address found. **Best available "
            f"fallback: `{pick.address}`** — but this is not a work "
            f"email at the resolved employer."
        )
    elif intent == "work" and high_belief:
        lines.append(
            f"**Best for cold work outreach: `{pick.address}`**"
        )
    elif intent == "work":
        lines.append(
            f"Best available work candidate: `{pick.address}` — "
            f"low confidence; verify before sending."
        )
    elif intent == "personal":
        lines.append(
            f"**Best personal address: `{pick.address}`** — for warm "
            f"outreach, not cold business."
        )
    else:
        lines.append(f"**Best overall: `{pick.address}`**")

    # Reason-to-trust line
    lines.append(f"_Found via: {_short_provenance(pick)}_")

    # Reason-to-avoid: surface SMTP and deliverability caveats
    caveats = []
    if pick.smtp_verdict == "inconclusive":
        prov = f" ({pick.mx_provider})" if pick.mx_provider else ""
        caveats.append(
            f"SMTP inconclusive{prov} — provider blocks RCPT; "
            f"deliverability not confirmed"
        )
    elif pick.smtp_verdict == "catch_all":
        caveats.append("catch-all domain — mailbox cannot be confirmed")
    elif pick.smtp_verdict == "invalid":
        caveats.append("SMTP rejected this address — likely bad")
    if pick.employer_former_match:
        caveats.append("former-employer domain — likely inactive")
    if caveats:
        lines.append("")
        for c in caveats:
            lines.append(f"⚠ {c}")
    return lines


def _candidate_table(
    candidates: list[EmailCandidate],
    *,
    show_work_score: bool,
) -> list[str]:
    """Markdown table rendering a list of candidates."""
    if not candidates:
        return []
    if show_work_score:
        lines = [
            "| Email | Belongs | Work | Deliverable | Found via |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    else:
        lines = [
            "| Email | Belongs | Deliverable | Found via |",
            "| --- | ---: | ---: | --- |",
        ]
    for c in candidates:
        addr = f"`{c.address}`"
        belongs = _fmt_score(c.belongs_to_person)
        deliv = _fmt_score(c.deliverable)
        provenance = _short_provenance(c)
        if show_work_score:
            work = _fmt_score(c.current_work_address)
            lines.append(f"| {addr} | {belongs} | {work} | {deliv} | {provenance} |")
        else:
            lines.append(f"| {addr} | {belongs} | {deliv} | {provenance} |")
    return lines


def _channel_hints_line(person: Person) -> list[str]:
    if not person.channel_hints:
        return []
    parts = []
    for k, v in person.channel_hints.items():
        if isinstance(v, bool) and v:
            parts.append(f"{k}")
        elif isinstance(v, str):
            parts.append(f"{k}: {v}")
        else:
            parts.append(f"{k}={v}")
    if not parts:
        return []
    return ["", f"**Channel hints:** {', '.join(parts)}"]


# ---- public API -------------------------------------------------------------


def render_decision_card(
    person: Person,
    candidates: list[EmailCandidate],
    *,
    intent: Intent = "work",
    max_per_section: int = 5,
) -> str:
    """Render the contact decision card as markdown.

    Args:
        person: Resolved Person record.
        candidates: Scored EmailCandidates (already scored by lib.score).
        intent: Default "work" — surfaces work addresses first with a
            work-oriented decision line. "personal" or "either" change
            ordering.
        max_per_section: Cap rows per Work/Personal/Other section.
    """
    parts: list[str] = []
    parts.extend(_header_block(person))
    parts.append("")
    parts.extend(_decision_line(candidates, intent))
    parts.append("")

    work, personal, other = _split_by_kind(candidates)
    work_sorted = sorted(work, key=_rank_for_work)[:max_per_section]
    personal_sorted = sorted(personal, key=_rank_for_personal)[:max_per_section]
    other_sorted = sorted(other, key=_rank_for_personal)[:max_per_section]

    # Section ordering depends on intent
    if intent == "personal":
        section_order = [
            ("## Personal", personal_sorted, False),
            ("## Work", work_sorted, True),
            ("## Other", other_sorted, False),
        ]
    elif intent == "either":
        # Combine all, sort by belongs_to_person, single table
        all_sorted = sorted(candidates, key=_rank_for_personal)[:max_per_section * 2]
        section_order = [("## All candidates", all_sorted, True)]
    else:  # work
        section_order = [
            ("## Work", work_sorted, True),
            ("## Personal", personal_sorted, False),
            ("## Other", other_sorted, False),
        ]

    for heading, cands, show_work in section_order:
        if not cands:
            continue
        parts.append(heading)
        parts.extend(_candidate_table(cands, show_work_score=show_work))
        parts.append("")

    parts.extend(_channel_hints_line(person))
    return "\n".join(parts).rstrip() + "\n"
