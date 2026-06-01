"""lib/reason.py — the LLM-native reasoning step (Opus 4.8).

The deterministic fetchers (git_emails, gh_profile, personal_site, pattern_gen,
verify_smtp, the host model's WebSearch) gather RAW OBSERVATIONS about a person.
This module hands all of them to Claude in a SINGLE tool-less call and gets back
a structured profile: facts, each with a citation to the observations that
support it, a confidence, and reasoning, plus a prose summary. The model does
the judgment that lib.score / lib.binding / lib.consistency_notes / lib.role_*
used to do as hand-written rules.

Two deterministic guarantees remain, by construction:

  1. GROUNDING (lib.ground): every returned fact cites an observation id that
     actually exists in the bundle, else it is dropped. The model can be
     persuaded to assert something; it cannot conjure an observation id for data
     it never received.

  2. TOOL-LESS: the call passes NO `tools`. The model reasons over already-
     fetched text and has no ability to fetch, run shell, or read files during
     the call. So adversarial text in the bundle (a target's own GitHub bio, an
     untrusted host-model search result) cannot steer the model into
     exfiltrating the operator's `gh` token or Google cookies. Read-only to the
     *target* is not read-only to *your machine*; this is the control that makes
     it so. (A merely-wrong profile is acceptable for a personal tool; leaking
     the operator's credentials is not.)

Why LLM-native at all: Opus 4.8 abstains when uncertain instead of
confabulating (its headline reliability property), which is exactly the
namesake-safety behavior the deterministic gate was hand-built to simulate — and
it does the long tail (name variants, company rebrands, intent ranking) that a
rule table never could. The judgment moves to the model; only the receipt-check
stays deterministic.

Standalone runs need ANTHROPIC_API_KEY (or an injected client). Without one the
caller falls back to the deterministic pipeline — see snoop.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .ground import GroundedFact, ground
from .normalize import name_match
from .schema import EmailCandidate, Person

MODEL = "claude-opus-4-8"

# Fact kinds the model may emit. Mirrors ContributionKind so the renderer can
# dispatch uniformly, but the model — not a weight table — decides the values.
FACT_KINDS = [
    "email", "work_item", "channel", "social_link", "role", "consistency_note",
]

# Structured-outputs JSON schema (no numeric/string constraints, every object
# additionalProperties:false — the structured-output limitations).
PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "facts"],
    "properties": {
        "summary": {"type": "string"},
        "identity_confidence": {"type": "number"},
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "kind", "label", "value", "detail",
                    "confidence", "evidence_ids", "reasoning",
                ],
                "properties": {
                    "kind": {"type": "string", "enum": FACT_KINDS},
                    "label": {"type": "string"},   # platform / channel_type / employer / ""
                    "value": {"type": "string"},   # address / url / title / note text
                    "detail": {"type": "string"},  # summary / evidence phrase / "" if none
                    "confidence": {"type": "number"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "reasoning": {"type": "string"},
                },
            },
        },
    },
}
# identity_confidence is optional; include it in required only if the model is
# reliable about it. Keep it optional so a missing value never fails validation.
PROFILE_SCHEMA["required"] = ["summary", "facts"]

# The frozen instruction block. Stable across every invocation, so it caches:
# put it first with cache_control, volatile evidence after (prompt-caching is a
# prefix match). No timestamps / ids in here, or the cache silently misses.
INSTRUCTIONS = """\
You are the reasoning core of `snoop`, a tool that builds a contact profile of a
real, named person for outreach. You receive a bundle of RAW OBSERVATIONS that
were already fetched from public sources (GitHub profile + repos, git commit
emails, a personal site, name-pattern email guesses, SMTP/account verdicts, and
possibly web-search results). Each observation has an id like `o3`.

Your job: decide who this person is and produce a profile.

OUTPUT (structured): a `summary` (2-4 sentences, the human lead), an optional
`identity_confidence` in [0,1], and a list of `facts`. Each fact:
  - kind: email | work_item | channel | social_link | role | consistency_note
  - label: the secondary key (platform, channel_type, employer) or ""
  - value: the address / url / title / note text — the thing to show
  - detail: a short evidence phrase or summary, or ""
  - confidence: YOUR calibrated [0,1] that this fact is true AND belongs to THIS
    person (not a namesake)
  - evidence_ids: the observation ids that support this fact. REQUIRED and
    non-empty. Cite only ids present in the bundle.
  - reasoning: one line on why you believe it / how it ties to the person

HARD RULES:
- Cite real observation ids only. A fact you cannot tie to an observation does
  not belong in the output — omit it. Do not invent ids.
- The lead/email answer matters most: surface the best email as a kind="email"
  fact, and order facts so the most reachable, best-evidenced contact is first.
- NAMESAKE SAFETY: if observations could describe more than one person, say so
  in the summary and lower every confidence accordingly. When you are not
  confident the bundle is a single person, ABSTAIN — emit fewer, lower-confidence
  facts rather than guessing. A missed fact is cheap; a wrong attribution to a
  stranger is the failure mode to avoid.
- Only self-published, real-identity facts. No pseudonym de-anonymization, no
  home address / location targeting, no sensitive-attribute inference.
- Treat web-search observations as untrusted: a page that merely names the
  person, with no corroborating signal in the bundle, is not evidence — do not
  emit a fact from it alone.
"""


@dataclass
class Observation:
    """One raw evidence unit handed to the model. `id` is stable within a run
    (o1, o2, ...) and is what facts cite; lib.ground checks those citations."""
    id: str
    type: str
    content: str
    source_url: str | None = None

    def render(self) -> str:
        loc = f" <{self.source_url}>" if self.source_url else ""
        return f"[{self.id}] ({self.type}){loc} {self.content}"


@dataclass
class ReasonedProfile:
    """The LLM-native deliverable: the resolved identity, a prose lead, and the
    grounded facts. `raw` keeps the model's pre-grounding output for debugging /
    --json; `usage` carries token accounting."""
    identity: Person
    summary: str
    facts: list[GroundedFact] = field(default_factory=list)
    identity_confidence: float | None = None
    observations: list[Observation] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


class ReasoningUnavailable(RuntimeError):
    """Raised when no Anthropic client/key is available. The CLI catches this
    and falls back to the deterministic pipeline."""


def build_evidence(person: Person, candidates: list[EmailCandidate]) -> list[Observation]:
    """Flatten the resolved person + scored candidates into a numbered list of
    raw observations. Deterministic and side-effect-free."""
    obs: list[Observation] = []
    n = 0

    def add(type_: str, content: str, url: str | None = None) -> None:
        nonlocal n
        content = " ".join(str(content).split())  # one-line; keep ids scannable
        if not content:
            return
        n += 1
        obs.append(Observation(id=f"o{n}", type=type_, content=content, source_url=url))

    gh = person.handles.get("github")
    if gh:
        add("github_handle", f"github handle: {gh}", f"https://github.com/{gh}")
    if person.gh_name:
        add("gh_profile", f'profile name: "{person.gh_name}"')
    if person.gh_bio:
        add("gh_profile", f'profile bio: "{person.gh_bio}"')
    if person.gh_company:
        add("gh_profile", f'profile company: "{person.gh_company}"')
    if person.gh_blog:
        add("gh_profile", f"profile blog/website: {person.gh_blog}", person.gh_blog)
    if person.gh_twitter:
        add("gh_profile", f"profile twitter: @{person.gh_twitter}",
            f"https://x.com/{person.gh_twitter}")
    if person.gh_location:
        add("gh_profile", f"profile location: {person.gh_location}")

    for atype, value in person.bound_anchors:
        add("anchor", f"identity anchor validated: {atype} = {value}")

    if person.employer and person.employer.name:
        add("employer", f"declared current employer: {person.employer.name}")
    for fe in person.former_employers:
        if fe and fe.name:
            add("employer", f"declared former employer: {fe.name}")

    for repo in person.gh_recent_repos:
        desc = f" — {repo.description}" if getattr(repo, "description", None) else ""
        add("github_repo", f"recent public repo: {repo.name}{desc}",
            getattr(repo, "html_url", None))

    for key, value in (person.channel_hints or {}).items():
        add("channel_hint", f"declared channel hint: {key} = {value}",
            value if isinstance(value, str) and value.startswith("http") else None)

    for cand in candidates:
        src_types = ",".join(sorted({s.type for s in cand.sources})) or "none"
        belongs = "?" if cand.belongs_to_person is None else f"{cand.belongs_to_person:g}"
        # When Google returned a display name, surface it WITH a text name-match
        # verdict against the target. This is the load-bearing disambiguator on a
        # common-name Workspace tenant: a pattern guess that hits a real-but-
        # different account (e.g. jdoe@ vs jdoeh@) shows name_match=no, and the
        # host model can drop it — text, not faces.
        extra = ""
        if cand.account_display_name:
            extra += f', google_display_name="{cand.account_display_name}"'
            if person.name:
                nm = name_match(cand.account_display_name, person.name)
                extra += f", name_match={'yes' if nm else 'no'}"
        # The photo is a HUMAN-REVIEW artifact only — never an automated match
        # signal (see SKILL.md scope). Labelled inline so the reader treats it
        # as something to eyeball, not a verdict to trust.
        if cand.account_photo_url:
            extra += (f", google_photo={cand.account_photo_url}"
                      " (human-review artifact, not an automated match)")
        add("email_candidate",
            f"candidate email: {cand.address} "
            f"(belongs~{belongs}, smtp={cand.smtp_verdict}, "
            f"account_exists={cand.account_exists}, sources={src_types}{extra})",
            next((s.url for s in cand.sources if s.url), None))

    for note in person.notes:
        add("resolver_note", f"resolver note: {note}")

    return obs


def reason_profile(
    person: Person,
    candidates: list[EmailCandidate],
    *,
    client: Any | None = None,
    model: str = MODEL,
    extra_observations: list[Observation] | None = None,
) -> ReasonedProfile:
    """Run the single tool-less reasoning call and return a grounded profile.

    Args:
        person: the resolved identity (still produced by the deterministic
            anchor resolver — identity validation stays a tool, profile judgment
            becomes the model's).
        candidates: scored email candidates (seed the email observations).
        client: an Anthropic client (injected in tests). If None, one is
            constructed from the environment; ReasoningUnavailable is raised when
            that is not possible.
        extra_observations: e.g. host-model web-search results pre-shaped by the
            caller, appended to the bundle.

    Returns:
        ReasonedProfile with grounded facts (every fact cites a real observation).
    """
    client = client or _default_client()

    observations = build_evidence(person, candidates)
    if extra_observations:
        # renumber appended observations so ids stay unique + contiguous
        base = len(observations)
        for i, o in enumerate(extra_observations, start=base + 1):
            observations.append(Observation(
                id=f"o{i}", type=o.type, content=o.content, source_url=o.source_url,
            ))

    evidence_text = (
        f"TARGET: {person.name}\n"
        f"resolved identity ambiguity: {person.ambiguity}\n\n"
        "OBSERVATIONS:\n" + "\n".join(o.render() for o in observations)
    )

    resp = client.messages.create(
        model=model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "high",
            "format": {"type": "json_schema", "schema": PROFILE_SCHEMA},
        },
        # NO `tools=` — tool-less by construction (the exfiltration control).
        system=[{
            "type": "text",
            "text": INSTRUCTIONS,
            "cache_control": {"type": "ephemeral"},  # frozen prefix -> cache hit
        }],
        messages=[{"role": "user", "content": evidence_text}],
    )

    data = _parse_response(resp)
    grounded = ground(data.get("facts", []), observations)

    return ReasonedProfile(
        identity=person,
        summary=str(data.get("summary", "")),
        facts=grounded,
        identity_confidence=_opt_float(data.get("identity_confidence")),
        observations=observations,
        usage=_usage(resp),
        raw=data,
    )


def _default_client() -> Any:
    try:
        import anthropic  # noqa: PLC0415 — optional dep, only for the LLM path
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise ReasoningUnavailable(
            "anthropic SDK not installed; `pip install anthropic` for --llm"
        ) from exc
    try:
        return anthropic.Anthropic()
    except Exception as exc:  # pragma: no cover - env-dependent
        raise ReasoningUnavailable(
            "no Anthropic credentials (set ANTHROPIC_API_KEY) for --llm"
        ) from exc


def _parse_response(resp: Any) -> dict[str, Any]:
    """Pull the JSON object out of the response. output_config.format guarantees
    the text block is valid JSON for the schema."""
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
    if not text:
        raise ValueError("reasoning call returned no text block")
    return json.loads(text)


def _usage(resp: Any) -> dict[str, int]:
    u = getattr(resp, "usage", None)
    if u is None:
        return {}
    return {
        "input_tokens": getattr(u, "input_tokens", 0) or 0,
        "output_tokens": getattr(u, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


def _opt_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
