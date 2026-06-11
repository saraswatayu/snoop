"""lib/reason.py — the observation bundle the host model reasons over.

The deterministic sensors (git_emails, gh_profile, personal_site, pattern_gen,
verify_smtp, google_account, plus the host model's own WebSearch) gather RAW
OBSERVATIONS about a person. `build_evidence` flattens the resolved person +
scored candidates into a numbered, typed, cited list of those observations.

The host model (already running in Claude Code) reasons over that bundle: it
picks the email, judges the namesake, builds the profile, writes the prose. It
then hands its facts to `lib.ground`, which deterministically checks every
citation traces to a real observation before the card renders.

`Observation` is one raw evidence unit; `ReasonedProfile` is the resolved
deliverable (identity + summary + grounded facts) that `--ground` renders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .ground import GroundedFact
from .normalize import name_match
from .schema import EmailCandidate, Person


@dataclass
class Observation:
    """One raw evidence unit handed to the host model. `id` is stable within a
    run (o1, o2, ...) and is what facts cite; lib.ground checks those citations.

    `content` is the one-line human/grounding-readable form (the substring
    surface lib.ground verifies against). `data` is an optional structured
    mirror — for an email_candidate it carries {address, smtp, account_exists,
    sources:[{type,url,detail}], google_display_name, name_match, google_photo}
    so the host model reads fields instead of re-parsing the sentence, and the
    full per-source list (with every URL) survives rather than collapsing to one
    link."""
    id: str
    type: str
    content: str
    source_url: str | None = None
    data: dict[str, Any] | None = None

    def render(self) -> str:
        loc = f" <{self.source_url}>" if self.source_url else ""
        return f"[{self.id}] ({self.type}){loc} {self.content}"


@dataclass
class ReasonedProfile:
    """The reasoned deliverable: the resolved identity, a prose lead, and the
    grounded facts. `observations` echoes the bundle the facts cite; `usage`
    carries optional token accounting."""
    identity: Person
    summary: str
    facts: list[GroundedFact] = field(default_factory=list)
    identity_confidence: float | None = None
    observations: list[Observation] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def build_evidence(person: Person, candidates: list[EmailCandidate]) -> list[Observation]:
    """Flatten the resolved person + scored candidates into a numbered list of
    raw observations. Deterministic and side-effect-free."""
    obs: list[Observation] = []
    n = 0

    def add(type_: str, content: str, url: str | None = None,
            data: dict[str, Any] | None = None) -> None:
        nonlocal n
        content = " ".join(str(content).split())  # one-line; keep ids scannable
        if not content:
            return
        n += 1
        obs.append(Observation(id=f"o{n}", type=type_, content=content,
                               source_url=url, data=data))

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
        # Structured mirror of the candidate so the host model reads fields
        # instead of re-parsing the sentence, and EVERY source URL survives
        # (the content line keeps just one for readability).
        data: dict[str, Any] = {
            "address": cand.address,
            "smtp": cand.smtp_verdict,
            "account_exists": cand.account_exists,
            "sources": [
                {"type": s.type, "url": s.url, "detail": s.detail}
                for s in cand.sources
            ],
        }
        # Surface the MX provider so an inconclusive SMTP verdict is actionable:
        # on microsoft (M365) there is NO existence oracle — unlike google, where
        # --allow-google-account discriminates — so the host model must lean on
        # channel hints rather than trust the inconclusive RCPT. (See the M365
        # spike: every unauthenticated existence probe there fails snoop's
        # honest-over-confident bar.)
        provider_note = ""
        if cand.mx_provider:
            data["mx_provider"] = cand.mx_provider
            provider_note = f", mx={cand.mx_provider}"
            if cand.mx_provider == "microsoft" and cand.smtp_verdict == "inconclusive":
                provider_note += " (M365 blocks RCPT and has no existence oracle — lean on channel hints)"
                data["smtp_note"] = "M365 blocks RCPT and has no existence oracle — lean on channel hints"
        # When Google returned a display name, surface it WITH a text name-match
        # verdict against the target. This is the load-bearing disambiguator on a
        # common-name Workspace tenant: a pattern guess that hits a real-but-
        # different account (e.g. jdoe@ vs jdoeh@) shows name_match=no, and the
        # host model can drop it — text, not faces.
        extra = ""
        if cand.account_display_name:
            extra += f', google_display_name="{cand.account_display_name}"'
            data["google_display_name"] = cand.account_display_name
            if person.name:
                nm = name_match(cand.account_display_name, person.name)
                extra += f", name_match={'yes' if nm else 'no'}"
                data["name_match"] = bool(nm)
        # The photo is a HUMAN-REVIEW artifact only — never an automated match
        # signal (see SKILL.md scope). Labelled inline so the reader treats it
        # as something to eyeball, not a verdict to trust.
        if cand.account_photo_url:
            extra += (f", google_photo={cand.account_photo_url}"
                      " (human-review artifact, not an automated match)")
            data["google_photo"] = cand.account_photo_url
            data["google_photo_note"] = "human-review artifact, not an automated match"
        add("email_candidate",
            f"candidate email: {cand.address} "
            f"(smtp={cand.smtp_verdict}{provider_note}, "
            f"account_exists={cand.account_exists}, sources={src_types}{extra})",
            next((s.url for s in cand.sources if s.url), None),
            data=data)

    for note in person.notes:
        add("resolver_note", f"resolver note: {note}")

    return obs
