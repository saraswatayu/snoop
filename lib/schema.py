"""Schema for /snoop's person-resolver pipeline.

Three structural choices from the dual-voice review:

1. EmailCandidate has THREE score fields, each 0-1 with None=abstain:
   `belongs_to_person`, `current_work_address`, `deliverable`. The old
   single-decimal `score` was a fake probability that blended ownership
   with deliverability with currentness — different things, calibrated
   from different evidence. Splitting them keeps abstention explicit.

2. Person.ambiguity has THREE states (not two). `single_plausible_match`
   does NOT mean "this is definitively the person" — it means "we found
   one candidate." Search recall is incomplete. The old `unique` state
   was a false-confidence trap.

3. Person.bound_anchors records WHICH identity signals independently
   tie this Person to the input. A handle from `--person-plan` is an
   untrusted hint until ≥2 anchors bind it. Defends against the host
   model laundering a hallucinated handle into "high-confidence
   provenance" via downstream resolvers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


SourceType = Literal[
    "git_commit",       # commit author email from gh api /users/{h}/events or /repos
    "gh_profile",       # public email field on the GitHub user profile
    "gh_readme",        # email scraped from the user's profile README
    "hn_profile",       # public email on news.ycombinator.com/user
    "whois",            # registrant/tech/admin email from WHOIS
    "personal_site",    # mailto: link or scraped email on a declared personal_domain
    "substack",         # author email from a Substack publication
    "package_registry", # npm or PyPI publisher email
    "x_bio",            # email extracted from X profile bio
    "pgp",              # OpenPGP key UID
    "linkedin",         # LinkedIn profile contact email (cookie-jar gated)
    "pattern",          # name × domain template guess (lowest trust)
    "smtp",             # SMTP RCPT verdict — modifies score, not a source itself
    "manual_known",     # user passed via --known; ground truth
]


AmbiguityState = Literal[
    "single_plausible_match",     # ≥1 candidate found and identity anchors bind
    "multiple_plausible_matches", # >1 person plausibly matches (Matt Smith problem)
    "insufficient_identity_evidence",  # no anchors bound; cannot proceed safely
]


SmtpVerdict = Literal[
    "verified",      # clean RCPT 250 on a non-catch-all domain
    "catch_all",     # domain accepts mail for any localpart
    "inconclusive",  # provider blocked RCPT (Google/M365) — zero information
    "invalid",       # RCPT 5xx rejection
    "unprobed",      # never tested (skipped: personal provider, budget exhausted, etc.)
]


@dataclass(frozen=True)
class Source:
    """One observation of an email address from a single resolver."""
    type: SourceType
    url: str | None              # public URL where the address was observed, if any
    observed_at: datetime         # UTC; when the resolver saw it
    detail: str                   # one-line human-readable provenance for the renderer


@dataclass
class Employer:
    """A target's employer at some point in time."""
    name: str
    domains: list[str]            # email domains used by this employer (lowercase, IDNA-encoded)
    since: str | None = None      # ISO date "YYYY-MM" or "YYYY-MM-DD" if known
    until: str | None = None      # None == current; ISO date if past


@dataclass
class EmailCandidate:
    """One candidate email for a target person.

    The three score fields are NOT a single probability. Each answers
    a different question and can independently abstain (None).
    """
    address: str                  # lowercased; IDNA-encoded for non-ASCII domains
    sources: list[Source] = field(default_factory=list)

    # Verification-layer outputs
    smtp_verdict: SmtpVerdict = "unprobed"
    mx_provider: str | None = None  # "google" | "microsoft" | "other" | None for renderer hints

    # Domain-level facts
    employer_match: bool = False           # address domain ∈ resolved current employer.domains
    employer_former_match: bool = False    # address domain ∈ a former employer.domains
    is_personal_provider: bool = False     # @gmail / @yahoo / @icloud / @outlook / @hotmail / @protonmail

    # Three-field score (None = no signal; abstain)
    belongs_to_person: float | None = None     # 0-1: how confident is this person's address
    current_work_address: float | None = None  # 0-1: how confident this is a current WORK address
    deliverable: float | None = None           # 0-1: a message sent here will reach a human

    # Per-field reasons (renderable receipts)
    score_reasons: list[str] = field(default_factory=list)


@dataclass
class Person:
    """A resolved person record. Output of person_resolve."""
    name: str
    name_variants: list[str] = field(default_factory=list)  # normalize.py-generated
    handles: dict[str, str] = field(default_factory=dict)   # {"github": "steipete", "x": "steipete", "hn": "steipete"}
    personal_domains: list[str] = field(default_factory=list)
    employer: Employer | None = None
    former_employers: list[Employer] = field(default_factory=list)
    channel_hints: dict[str, Any] = field(default_factory=dict)  # {"x_dms_open": True, ...}

    # Disambiguation contract (3 states; see module docstring)
    ambiguity: AmbiguityState = "insufficient_identity_evidence"
    ambiguity_candidates: list["Person"] = field(default_factory=list)

    # Anchor binding (defense against host hallucinated handles)
    # Each anchor is (anchor_type, value):
    #   ("name_match", "Peter Steinberger")
    #   ("github_repo_owner", "steipete")
    #   ("personal_domain_whois", "steipete.com")
    #   ("profile_cross_link", "x:@steipete linked from github bio")
    # A handle in `handles` is an untrusted hint until ≥2 anchors bind.
    bound_anchors: list[tuple[str, str]] = field(default_factory=list)

    # Validation notes from person_resolve: plan-vs-observed deltas, missing-
    # anchor warnings, search ambiguity context. Surfaced by the renderer.
    notes: list[str] = field(default_factory=list)


@dataclass
class ResolverResult:
    """One resolver's output + status. Lets the pipeline distinguish 'returned empty'
    from 'timed out' from 'capability missing.'"""
    resolver: str                          # "git_emails", "gh_profile", ...
    candidates: list[EmailCandidate]
    status: Literal["ok", "empty", "timeout", "unavailable", "error"]
    elapsed_ms: int | None = None
    error_detail: str | None = None
