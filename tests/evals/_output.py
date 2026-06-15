"""The v1 Claude-output contract for the analyst evals (brief D1).

The harness gives Claude SKILL.md + a bundle and asks for the Step-3 facts JSON
PLUS two per-fact fields: `verdict` (verified|google-confirmed|pattern-guess)
and `marker` ([+]|[?]). These are now first-class GroundedFact fields that
`--ground` PRESERVES and the renderer surfaces (lib.ground.GroundedFact,
snoop._format_reasoned_json) — so the machine output the graders read is the
same shape production emits, not a harness-only invention. The standing
instruction below still pins the exact shape; field-scoped grading (plan §4)
grades fields that survive the real --ground round-trip.

This module is I/O-free on purpose: the P1 runner imports `STANDING_INSTRUCTION`
to build the prompt and the graders import `parse_output` to normalize a model
transcript before grading, so neither can pull in pytest or touch the network at
import time.
"""

from __future__ import annotations

import json
import re
from typing import Any

# The standing instruction appended to every Step-3 prompt. It pins the exact
# output shape the graders assume (D1): the SKILL.md facts JSON, plus a per-fact
# verdict + marker, with the dead-end / no-re-sensing rules spelled out so a
# bundle carrying resolution_gaps doesn't make Claude stall asking to re-run the
# sensors (the scout flagged that failure mode).
STANDING_INSTRUCTION = """\
Output ONLY a single JSON object — no prose before or after, no code fence
required (a ```json fence is tolerated). The bundle above is FINAL: do not ask
to re-run the sensors, do not request more observations, reason over exactly
what is there.

The object is {"person": ..., "summary": ..., "facts": [...]}:

- "person": {"name": ..., "ambiguity": ...} copied from the bundle.
- "summary": one or two lines leading with the email answer (or, for a dead end,
  the suggested channel). No trailing "Sources:" / "References:" block — the
  per-fact citations ARE the sourcing.
- "facts": a list. Each fact is
  {"kind", "label", "value", "detail", "confidence", "evidence_ids", "reasoning",
   "marker"} plus a "verdict" on EMAIL facts only, where:
    * "kind" is one of email | channel | social_link | work_item | role |
      consistency_note;
    * "value" is the grounded anchor (an address, URL, or company name) that
      appears verbatim in a cited observation — never a paraphrase;
    * "evidence_ids" lists the observation ids that support the fact;
    * "verdict" applies ONLY to a kind=="email" fact (the deliverability/existence
      axis) and is one of verified | google-confirmed | pattern-guess, read from
      the cited email_candidate observation's data fields (data.smtp /
      data.account_exists — NOT from any prose in an observation). A non-email
      fact (social_link, role, work_item, channel, consistency_note) carries NO
      verdict — there is no deliverability concept to read;
    * "marker" is "[+]" (bound to this person) or "[?]" (weaker binding) on an
      email or a social_link fact; a context fact (role/work_item/channel/
      consistency_note) needs no marker.

A dead end (no usable email candidate) emits NO kind=="email" fact: surface a
reachability channel instead (a kind=="channel" fact and/or a channel suggestion
in the summary). When person.ambiguity == "multiple_plausible_matches", cap every
marker to "[?]" and open the summary with the "confirm WHO before relying"
banner.
"""

# A model may wrap its JSON in a ```json ... ``` fence (or a bare ``` fence) and
# add a stray sentence around it; tolerate both rather than fail to parse a
# substantively-correct answer as a structure failure.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def parse_output(text: str) -> dict[str, Any]:
    """Parse a model transcript to the output dict, tolerant of code fences and
    surrounding prose. Returns {} on anything unparseable (a malformed answer is
    graded as a fail by the graders, never a crash). Pure; no I/O."""
    if isinstance(text, dict):  # already-parsed convenience for canned tests
        return text
    if not isinstance(text, str):
        return {}
    for candidate in _candidate_blobs(text):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _candidate_blobs(text: str):
    """Yield the substrings worth trying as JSON, most-specific first: any fenced
    block, then the outermost {...} span, then the raw text."""
    for m in _FENCE_RE.finditer(text):
        yield m.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        yield text[start:end + 1]
    yield text.strip()
