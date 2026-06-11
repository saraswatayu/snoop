"""lib/render.py — the grounded card renderer for --ground.

snoop emits an observation bundle; the host model reasons over it and produces
facts; `--ground` checks the citations and calls this to render the result. The
renderer only formats and SANITIZES — `_oneline` on every value — because the
facts trace back to untrusted observations (a target's own GitHub bio, a
host-model search hit) and must never forge a marked line.
"""

from __future__ import annotations

import re

from .schema import Person


# Control characters (incl. newline, tab, carriage return, the ANSI ESC byte,
# and DEL) collapsed to a space before any untrusted value is interpolated into
# a card line.
_CTRL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _oneline(value: object) -> str:
    """Collapse an untrusted value to a single safe display line.

    Strips control characters (newlines, tabs, carriage returns, ANSI escapes,
    DEL) and collapses whitespace runs to single spaces. The card's confidence
    markers are a trust contract; without this, an untrusted field — a host-model
    search result, a target's own GitHub display name — could embed a newline and
    forge a fabricated marked line such as `[+] verified email: evil@x.com`.
    """
    return " ".join(_CTRL_CHARS.sub(" ", str(value)).split())


# Section headers + display order for the model's fact kinds.
_KIND_SECTION: dict[str, str] = {
    "email": "Email",
    "channel": "Other ways in",
    "social_link": "Social",
    "work_item": "Body of work",
    "role": "Roles",
    "consistency_note": "Identity check",
}
_KIND_ORDER = list(_KIND_SECTION)


def _conf_marker(confidence: float, person: Person) -> str:
    """Confidence band -> provenance marker.

    The hard cap (no `[+]`) applies ONLY to `multiple_plausible_matches` — the
    genuine namesake case, where attributing a fact to the wrong person is the
    risk. `insufficient_identity_evidence` (we simply have no anchor yet) is NOT
    capped: the model's per-fact confidence speaks, so a Google/SMTP-verified
    address can read as strong while the card carries a scoped 'not anchored'
    caveat. Conflating the two was the bug that buried verified results under a
    wall of `[?]`."""
    band = "[+]" if confidence >= 0.66 else "[?]" if confidence >= 0.33 else "[·]"
    if person.ambiguity == "multiple_plausible_matches" and band == "[+]":
        return "[?]"
    return band


def render_reasoned_card(profile, *, warnings: list[str] | None = None) -> str:
    """Render the grounded profile (lib.reason.ReasonedProfile).

    The model wrote the summary and every fact; this renderer formats and
    sanitizes. Each fact shows its confidence marker and an `unverified` tag when
    the deterministic grounding check could not find the value in a cited
    observation."""
    person = profile.identity
    lines: list[str] = []
    if warnings:
        lines.extend(f"⚠ {_oneline(w)}" for w in warnings)
        lines.append("")

    if profile.summary:
        lines.append(_oneline(profile.summary))

    # Two non-confident states, two different banners. multiple_plausible_matches
    # (or the model signalling strong doubt) is the genuine namesake case → loud
    # cap-and-confirm. insufficient_identity_evidence just means no anchor was
    # bound → a softer, scoped caveat that doesn't bury verified facts.
    ic = profile.identity_confidence
    if person.ambiguity == "multiple_plausible_matches" or (ic is not None and ic < 0.5):
        lines.append("")
        lines.append("[?] identity is NOT a single confident match — more than one "
                     "person may fit; confirm WHO before relying.")
    elif person.ambiguity == "insufficient_identity_evidence":
        lines.append("")
        lines.append("[·] identity not independently anchored (no GitHub or "
                     "cross-linked profile found) — verified facts are still "
                     "verified-deliverable; confirm the person if it matters.")

    by_kind: dict[str, list] = {}
    for f in profile.facts:
        by_kind.setdefault(f.kind, []).append(f)

    for kind in _KIND_ORDER:
        facts = sorted(by_kind.get(kind, []),
                       key=lambda f: f.confidence, reverse=True)
        if not facts:
            continue
        lines.append("")
        lines.append(f"{_KIND_SECTION[kind]}:")
        for f in facts:
            label = f"{_oneline(f.label)}: " if f.label else ""
            detail = f" — {_oneline(f.detail)}" if f.detail else ""
            tag = "" if f.verified else " (unverified)"
            lines.append(
                f"  {_conf_marker(f.confidence, person)} {label}{_oneline(f.value)}"
                f"{detail}{tag}"
            )

    if not profile.facts:
        lines.append("")
        lines.append("No attributable facts — nothing tied back to a confirmed "
                     "observation for this person.")

    return "\n".join(lines).rstrip() + "\n"
