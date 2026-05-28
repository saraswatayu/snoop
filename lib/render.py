"""lib/render.py — contact decision card output.

Reframed in 2026-05-28 per docs/plans/2026-05-28-output-redesign.md:
the output's job is to answer "what should I paste into Gmail?"
in the first two lines. Forensic detail moves behind --verbose.

Default output (verbose=False):
    Name → Employer
    address · verdict [(caveat)]
    About: (dossier — GitHub, LinkedIn, blog, X, recent repos)
    Why: (one-line provenance)
    Note: (name disambiguation, if any)
    If it bounces, try in order: (fallback patterns; asymmetric per D1-C)

Verbose output (verbose=True):
    Everything above, plus the original Identity / Resolver notes block
    and the per-candidate score tables. Use when the default surprises you.

Verdict buckets replace the three-axis score triplet at the human
layer. The axes (belongs_to_person, current_work_address, deliverable)
remain in --json output for machine consumers and in verbose tables.

Four buckets:
  - verified         : SMTP RCPT 250 on a non-catch-all domain
  - google-confirmed : Google's People API returned a real account
                       (catch-all/inconclusive SMTP doesn't downgrade this)
  - pattern-guess    : no positive existence signal, just a name×domain guess
  - dead-end         : nothing usable, suggest alternate channels
"""

from __future__ import annotations

from typing import Iterable, Literal

from .schema import EmailCandidate, Person


Intent = Literal["work", "personal", "either"]
VerdictBucket = Literal["verified", "google-confirmed", "pattern-guess", "dead-end"]


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


def _pick_best(
    candidates: list[EmailCandidate],
    intent: Intent,
) -> tuple[EmailCandidate | None, bool]:
    """Choose the best candidate to recommend. Returns (pick, fell_back).

    `fell_back` is True when intent=="work" but we had to use a non-employer
    address because no employer-domain candidate was found.
    """
    if not candidates:
        return None, False
    work, personal, other = _split_by_kind(candidates)
    if intent == "work":
        if work:
            return sorted(work, key=_rank_for_work)[0], False
        if other:
            return sorted(other, key=_rank_for_personal)[0], True
        if personal:
            return sorted(personal, key=_rank_for_personal)[0], True
        return None, False
    if intent == "personal":
        if personal:
            return sorted(personal, key=_rank_for_personal)[0], False
        return None, False
    # either
    all_ranked = sorted(candidates, key=_rank_for_personal)
    return (all_ranked[0] if all_ranked else None), False


def _verdict_bucket(pick: EmailCandidate | None) -> VerdictBucket:
    """Classify the pick into one of four buckets driving the default output.

    `google-confirmed` is the load-bearing case: when Google's People API
    returned a real account, SMTP being catch-all/inconclusive does NOT
    downgrade to pattern-guess. Existence is independently positive evidence.
    """
    if pick is None:
        return "dead-end"
    if pick.smtp_verdict == "verified":
        return "verified"
    if (pick.account_exists == "verified"
            and pick.smtp_verdict in ("catch_all", "inconclusive", "unprobed")):
        return "google-confirmed"
    return "pattern-guess"


def _verdict_label(bucket: VerdictBucket, pick: EmailCandidate | None) -> str:
    """The compact verdict string that follows the address on the lead line."""
    if bucket == "verified":
        return "verified"
    if bucket == "google-confirmed":
        if pick is not None and pick.smtp_verdict == "catch_all":
            return "google-confirmed (catch-all, so SMTP inconclusive)"
        if pick is not None and pick.smtp_verdict == "inconclusive":
            prov = f", {pick.mx_provider}" if pick.mx_provider else ""
            return f"google-confirmed (SMTP blocked by provider{prov})"
        return "google-confirmed"
    if bucket == "pattern-guess":
        if pick is not None and pick.smtp_verdict == "invalid":
            return "pattern-guess (SMTP rejected — likely bad)"
        if pick is not None and pick.smtp_verdict == "catch_all":
            return "pattern-guess (catch-all, no verification possible)"
        return "pattern-guess (no positive verification)"
    return "dead-end"


# ---- About block (dossier) --------------------------------------------------


def _format_blog_url(blog: str) -> str:
    """Render a blog/website value. Strip protocol for display compactness;
    keep the trailing path so non-root URLs remain identifiable."""
    s = blog.strip()
    for prefix in ("https://", "http://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break
    return s.rstrip("/")


def _format_linkedin_url(value: object) -> str | None:
    """Channel hints sometimes carry the full URL, sometimes just the slug.
    Render compactly without protocol; return None when missing."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    for prefix in ("https://", "http://"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break
    return s.rstrip("/")


def _about_block(person: Person) -> list[str]:
    """The dossier — compact summary of what we know about the person.

    Skips entirely if no fields populated. Each line is `  Label:   value`
    with two-space indent for visual grouping under the lead.
    """
    rows: list[tuple[str, str]] = []

    gh_handle = person.handles.get("github")
    if gh_handle:
        gh_url = f"github.com/{gh_handle}"
        if person.gh_bio:
            # Collapse internal whitespace (newlines, tabs, multi-space) so
            # multi-paragraph bios render as a single scannable line. Without
            # this, a real Peter-Steinberger-style bio with "\n\nPreviously…"
            # breaks the About block into three visual lines.
            bio = " ".join(person.gh_bio.split())
            if len(bio) > 80:
                bio = bio[:77] + "…"
            rows.append(("GitHub", f'{gh_url} — "{bio}"'))
        else:
            rows.append(("GitHub", gh_url))

    linkedin = _format_linkedin_url(person.channel_hints.get("linkedin"))
    if linkedin:
        rows.append(("LinkedIn", linkedin))

    if person.gh_blog:
        rows.append(("Web", _format_blog_url(person.gh_blog)))

    if person.gh_twitter:
        rows.append(("X", f"@{person.gh_twitter}"))

    if person.gh_company and person.employer and person.gh_company != person.employer.name:
        # Surface GitHub's company text only when it differs from the
        # canonical employer (potential mismatch worth seeing). When they
        # match, the employer is already in the header line.
        rows.append(("GH company", person.gh_company))

    if person.gh_location:
        rows.append(("Location", person.gh_location))

    if not rows:
        return []

    # Compute label column width for visual alignment
    width = max(len(label) for label, _ in rows) + 1
    lines = ["About:"]
    for label, value in rows:
        lines.append(f"  {label + ':':<{width + 1}} {value}")
    return lines


def _recent_repos_block(person: Person, n: int = 3) -> list[str]:
    """Show top-N recently-pushed public repos in the dossier."""
    if not person.gh_recent_repos:
        return []
    repos = person.gh_recent_repos[:n]
    # Align repo names for the human eye
    width = max(len(r.name) for r in repos)
    lines = ["", "Recent on GitHub:"]
    for r in repos:
        desc = r.description.strip() if r.description else ""
        if desc:
            if len(desc) > 60:
                desc = desc[:57] + "…"
            lines.append(f"  {r.name:<{width}}  · \"{desc}\"")
        else:
            lines.append(f"  {r.name}")
    return lines


# ---- name disambiguation ----------------------------------------------------


def _name_disambiguation_note(person: Person) -> str | None:
    """When the GitHub-observed name or LinkedIn-observed name differs from
    the target name in a meaningful way, surface it as a first-class note
    instead of burying it in resolver-notes prose."""
    target = person.name.strip()
    if not target:
        return None
    observed = person.gh_name and person.gh_name.strip()
    if not observed or observed.lower() == target.lower():
        return None
    # If the target is a prefix/diminutive of observed (e.g., "Dan" vs
    # "Daniel Neil"), call it out plainly.
    first_target = target.split()[0].lower()
    first_observed = observed.split()[0].lower()
    if first_target != first_observed and first_observed.startswith(first_target):
        return f'you said "{first_target.title()}", profile says "{first_observed.title()}"'
    if first_target != first_observed:
        return f'profile name "{observed}" differs from input "{target}"'
    return None


# ---- caveats ---------------------------------------------------------------


def _pick_caveats(pick: EmailCandidate) -> list[str]:
    """Caveats specific to the pick (SMTP/provider notes, former-employer flag).
    These render under the lead address line as ⚠ items."""
    caveats: list[str] = []
    if pick.smtp_verdict == "inconclusive":
        prov = f" ({pick.mx_provider})" if pick.mx_provider else ""
        caveats.append(
            f"SMTP inconclusive{prov} — provider blocks RCPT; "
            "deliverability not confirmed"
        )
    elif pick.smtp_verdict == "catch_all":
        caveats.append("catch-all domain — mailbox cannot be confirmed via SMTP")
    elif pick.smtp_verdict == "invalid":
        caveats.append("SMTP rejected this address — likely bad")
    if pick.employer_former_match:
        caveats.append("former-employer domain — likely inactive")
    return caveats


# ---- fallback list ----------------------------------------------------------


def _fallback_addresses(
    pick: EmailCandidate,
    candidates: list[EmailCandidate],
    intent: Intent,
) -> list[str]:
    """The "if it bounces, try in order" list — same kind as the pick,
    excluding the pick itself. Sorted by belongs descending.

    Asymmetric visibility (D1-C from the plan): callers that should hide
    the fallback list (e.g., verified pick) call this and discard the
    result, rather than _render_lead deciding for them.
    """
    work, personal, other = _split_by_kind(candidates)
    if intent == "work":
        same_kind = work or other or personal
    elif intent == "personal":
        same_kind = personal
    else:
        same_kind = candidates
    rest = [c for c in same_kind if c is not pick]
    rest.sort(key=_rank_for_work if intent == "work" else _rank_for_personal)
    return [c.address for c in rest]


# ---- channel hints (dead-end aid) ------------------------------------------


def _channel_hints_inline(person: Person) -> list[str]:
    """One-line channel hints rendered as a 'Try:' suggestion. Used in
    dead-end output and as a footer when no email confidence is high."""
    if not person.channel_hints:
        return []
    parts: list[str] = []
    for k, v in person.channel_hints.items():
        if isinstance(v, bool) and v:
            parts.append(k.replace("_", " "))
        elif isinstance(v, str):
            # Strip protocol for display when it's a URL
            value = v
            for prefix in ("https://", "http://"):
                if value.lower().startswith(prefix):
                    value = value[len(prefix):]
                    break
            parts.append(f"{k}: {value.rstrip('/')}")
        else:
            parts.append(f"{k}={v}")
    if not parts:
        return []
    return [f"Try: {' · '.join(parts)}"]


# ---- verbose appendix (old behavior) ----------------------------------------


def _candidate_table(
    candidates: list[EmailCandidate],
    *,
    show_work_score: bool,
) -> list[str]:
    """Markdown table rendering a list of candidates. Verbose-only now."""
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


def _verbose_identity_block(person: Person) -> list[str]:
    """Identity / ambiguity / resolver notes — verbose-only.

    The default output deliberately hides this: "0 identity anchors bound"
    is internal jargon that undermines confidence at the moment the user
    needs to act. --verbose surfaces it for the cases when the user is
    debugging a surprising result.
    """
    bound_validating = [
        a for a in person.bound_anchors if a[0] != "github_handle_exists"
    ]
    bound_summary = (
        f"{len(bound_validating)} identity anchor"
        f"{'s' if len(bound_validating) != 1 else ''} bound"
    )
    lines = [
        "",
        "---",
        "",
        f"_Identity: {_AMBIGUITY_LABEL[person.ambiguity]} ({bound_summary})_",
    ]
    if person.notes:
        lines.append("")
        lines.append("**Resolver notes:**")
        for n in person.notes:
            lines.append(f"- {n}")
    return lines


def _verbose_tables(
    candidates: list[EmailCandidate],
    intent: Intent,
    max_per_section: int,
) -> list[str]:
    """Per-section candidate tables — verbose-only."""
    work, personal, other = _split_by_kind(candidates)
    work_sorted = sorted(work, key=_rank_for_work)[:max_per_section]
    personal_sorted = sorted(personal, key=_rank_for_personal)[:max_per_section]
    other_sorted = sorted(other, key=_rank_for_personal)[:max_per_section]
    if intent == "personal":
        section_order = [
            ("## Personal", personal_sorted, False),
            ("## Work", work_sorted, True),
            ("## Other", other_sorted, False),
        ]
    elif intent == "either":
        all_sorted = sorted(candidates, key=_rank_for_personal)[: max_per_section * 2]
        section_order = [("## All candidates", all_sorted, True)]
    else:
        section_order = [
            ("## Work", work_sorted, True),
            ("## Personal", personal_sorted, False),
            ("## Other", other_sorted, False),
        ]
    out: list[str] = []
    for heading, cands, show_work in section_order:
        if not cands:
            continue
        out.append("")
        out.append(heading)
        out.extend(_candidate_table(cands, show_work_score=show_work))
    return out


# ---- the lead block (the whole show in default mode) ------------------------


def _employer_header(person: Person) -> str:
    """First line of the card: 'Name → Employer'. Falls back gracefully
    when employer is missing."""
    if person.employer and person.employer.name:
        return f"{person.name} → {person.employer.name}"
    return person.name


def _render_lead(
    person: Person,
    pick: EmailCandidate | None,
    bucket: VerdictBucket,
    candidates: list[EmailCandidate],
    intent: Intent,
    fell_back: bool,
) -> list[str]:
    """The compact lead block — name, address, verdict, About, Recent,
    Why, Note, fallback list. This IS the default output."""
    lines = [_employer_header(person)]

    if pick is None:
        # Dead-end path
        lines.append("")
        lines.append("No deliverable address found.")
        # What did we try?
        if candidates:
            tried = len(candidates)
            lines.append("")
            lines.append(
                f"Probed {tried} candidate{'s' if tried != 1 else ''}; "
                "nothing positively confirmed."
            )
        else:
            lines.append("")
            lines.append(
                "No candidates produced — try providing more context "
                "(github handle, personal domain, known address) via --person-plan."
            )
        # Suggest channels
        about = _about_block(person)
        if about:
            lines.append("")
            lines.extend(about)
        recent = _recent_repos_block(person)
        if recent:
            lines.extend(recent)
        hints = _channel_hints_inline(person)
        if hints:
            lines.append("")
            lines.extend(hints)
        return lines

    # ----- normal path: we have a pick -----
    verdict = _verdict_label(bucket, pick)
    lines.append(f"`{pick.address}`  ·  {verdict}")

    # Caveats inline (loud, adjacent to the address)
    caveats = _pick_caveats(pick)
    # Suppress redundant caveats already named in the verdict label
    if bucket == "google-confirmed":
        caveats = [c for c in caveats if "catch-all" not in c and "SMTP inconclusive" not in c]
    for cav in caveats:
        lines.append(f"⚠ {cav}")

    # Fell-back framing: surface that the picked address isn't on the
    # current employer's domain. Important when the user asked for
    # work outreach and we couldn't find one.
    if fell_back and intent == "work":
        lines.append(
            "⚠ no current-employer address found — this is a fallback, "
            "not a confirmed work email"
        )

    # About block
    about = _about_block(person)
    if about:
        lines.append("")
        lines.extend(about)

    # Recent on GitHub (D4-C)
    recent = _recent_repos_block(person)
    if recent:
        lines.extend(recent)

    # Why line
    lines.append("")
    why = _short_provenance(pick)
    lines.append(f"Why: {why}")

    # Name disambiguation note (first-class, not buried in resolver notes)
    note = _name_disambiguation_note(person)
    if note:
        lines.append(f"Note: {note}")

    # Personal-address-for-cold-business warning
    if intent == "personal" and pick.is_personal_provider:
        lines.append("Note: personal address — for warm outreach, not cold business.")

    # Fallback list: asymmetric per D1-C — hide for verified, show for
    # google-confirmed and pattern-guess (since neither is double-verified).
    if bucket != "verified":
        fallbacks = _fallback_addresses(pick, candidates, intent)
        if fallbacks:
            lines.append("")
            lines.append("If it bounces, try in order:")
            # Compact comma/dot-separated list, one indented line
            lines.append("  " + " · ".join(fallbacks[:5]))

    return lines


# ---- public API -------------------------------------------------------------


def render_decision_card(
    person: Person,
    candidates: list[EmailCandidate],
    *,
    intent: Intent = "work",
    max_per_section: int = 5,
    verbose: bool = False,
) -> str:
    """Render the contact decision card as markdown.

    Args:
        person: Resolved Person record.
        candidates: Scored EmailCandidates (already scored by lib.score).
        intent: "work" (default), "personal", or "either". Shapes which
            kind of address gets picked and what fallback list is shown.
        max_per_section: Cap rows per Work/Personal/Other section. Only
            applies in verbose mode (default hides per-section tables).
        verbose: When True, append the original Identity/Resolver-notes
            block and per-section candidate tables under the compact lead.

    Returns:
        Markdown string. Default form (verbose=False) is compact: lead
        with a verdict, dossier, provenance, optional fallback list.
        Verbose form adds detail without changing the lead.
    """
    pick, fell_back = _pick_best(candidates, intent)
    bucket = _verdict_bucket(pick)
    lines = _render_lead(person, pick, bucket, candidates, intent, fell_back)

    if verbose:
        lines.extend(_verbose_identity_block(person))
        if candidates:
            lines.extend(_verbose_tables(candidates, intent, max_per_section))

    return "\n".join(lines).rstrip() + "\n"
