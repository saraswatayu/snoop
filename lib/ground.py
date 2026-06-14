"""lib/ground.py — the deterministic verifier for LLM-native reasoning.

This is the ONE piece that stays deterministic in the LLM-native pipeline. The
model (lib.reason) does all the judgment; this module only checks the receipts:

  1. EXISTENCE — every fact must cite an observation id that actually exists in
     the evidence bundle we fetched. The model can be talked into a claim, but
     it cannot make a string (an observation id) appear in data it never
     received. A fact with no surviving citation is dropped — that is the
     namesake gate, now enforced by grounding rather than by a hand-written
     binding rule.

  2. VERIFICATION — a softer substring check: does the fact's asserted value
     actually appear in one of its cited observations? Sets `verified`. Used to
     mark a fact as confirmed-against-source vs. inferred; it downgrades trust,
     it does not drop (the model may legitimately normalize a value, e.g.
     lowercasing an email or summarizing a title).

Why a check at all, when the model is good? Because a verifier is only worth
running if its failure modes are INDEPENDENT of the thing it checks. A second
LLM reading the same adversarial bundle shares the generator's errors; a plain
substring/set check does not. Keep this until prompt injection is a solved,
relied-upon property — not before. (Personal read-only tool: the blast radius of
a wrong fact is small, so this is a quality net more than a security wall, but
the existence check is cheap and keeps the [+]/[?] markers honest.)
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Iterable, Protocol, Sequence


class _Observation(Protocol):
    """Anything with a stable id and raw text content. lib.reason.Observation
    satisfies this; so does a plain object or a dict-wrapper."""
    id: str
    content: str


@dataclass
class GroundedFact:
    """A model-produced fact after the deterministic check.

    `evidence_ids` is filtered to citations that exist. `grounded` is always
    True for a returned fact (ungrounded facts are dropped). `verified` is the
    substring confirmation against a cited observation."""
    kind: str
    label: str
    value: str
    detail: str
    confidence: float
    reasoning: str
    evidence_ids: list[str] = field(default_factory=list)
    grounded: bool = True
    verified: bool = False


# Tokens long enough to be meaningful for the substring check. A 2-char token
# ("AI") would match almost anything; require >=4 alphanumeric chars.
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{3,}")


def _significant_tokens(value: str) -> list[str]:
    return [m.group(0).lower() for m in _TOKEN.finditer(value or "")]


def _distinctive_url_token(value: str) -> str | None:
    """For a URL value, the last non-empty path segment — the username / repo /
    slug that actually identifies it. Returns None when the value isn't a URL
    with a path. The host (github.com, x.com) is deliberately NOT used: it is
    generic and shared across every profile on the platform, so matching on it
    would verify a DIFFERENT profile's URL."""
    s = (value or "").strip()
    if "://" not in s:
        return None
    try:
        parsed = urllib.parse.urlsplit(s)
    except ValueError:
        return None
    if not parsed.netloc:
        return None
    segments = [seg for seg in parsed.path.split("/") if seg]
    return segments[-1].lower() if segments else None


def _value_appears(value: str, haystacks: Iterable[str]) -> bool:
    """True when the value (or its most significant token) shows up verbatim in
    any cited observation. Whole-value match first; else fall back to the
    longest token so a normalized form (lowercased email, trimmed title) still
    verifies without a brittle exact match.

    A URL value is matched on its DISTINCTIVE path segment, not the longest
    token: the longest token of `https://github.com/janedoe` is the generic host
    `github.com`, which would spuriously verify against any other github URL. The
    last path segment (`janedoe`) is the identity that must actually appear."""
    v = (value or "").strip().lower()
    if not v:
        return False
    blob = "\n".join(h.lower() for h in haystacks)
    if v in blob:
        return True
    url_token = _distinctive_url_token(value)
    if url_token is not None:
        return url_token in blob
    toks = sorted(_significant_tokens(value), key=len, reverse=True)
    return any(t in blob for t in toks[:1])  # the single longest token


def ground(facts: list[dict], observations: Sequence[_Observation]) -> list[GroundedFact]:
    """Filter and stamp model facts against the evidence bundle.

    For each fact:
      - keep only evidence_ids that name a real observation; DROP the fact if
        none survive (no cited evidence -> not attributable);
      - set `verified` if the asserted value appears in a cited observation.

    Returns the surviving facts in input order. Pure; no network, no model.
    """
    by_id = {o.id: o for o in observations}
    kept: list[GroundedFact] = []

    for fact in facts:
        raw_ids = fact.get("evidence_ids") or []
        valid = [i for i in raw_ids if isinstance(i, str) and i in by_id]
        if not valid:
            continue  # the gate: a fact with no real citation is dropped

        cited_content = [by_id[i].content for i in valid]
        kept.append(GroundedFact(
            kind=str(fact.get("kind", "")),
            label=str(fact.get("label", "")),
            value=str(fact.get("value", "")),
            detail=str(fact.get("detail", "")),
            confidence=_clamp01(fact.get("confidence")),
            reasoning=str(fact.get("reasoning", "")),
            evidence_ids=valid,
            grounded=True,
            verified=_value_appears(str(fact.get("value", "")), cited_content),
        ))
    return kept


def _clamp01(value: object) -> float:
    """Coerce the model's confidence to [0, 1]; default 0.0 on junk."""
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))
