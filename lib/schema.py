"""Schema for /snoop's sensor pipeline.

The sensors read public sources and emit these typed records; the host model
reasons over the resulting observation bundle. Two structural choices worth
keeping in view:

1. Person.ambiguity has THREE states (not two). `single_plausible_match`
   does NOT mean "this is definitively the person" — it means "we found
   one candidate." Search recall is incomplete. A two-state `unique`/`not`
   would be a false-confidence trap.

2. Person.bound_anchors records WHICH identity signals independently
   tie this Person to the input. A handle from `--person-plan` is an
   untrusted hint until ≥2 anchors bind it. Defends against the host
   model laundering a hallucinated handle into "high-confidence
   provenance" via downstream sensors.
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
    "google_account",   # Google People API existence + (sometimes) profile
    "pattern",          # name × domain template guess (lowest trust)
    "smtp",             # SMTP RCPT verdict — modifies score, not a source itself
    "manual_known",     # user passed via --known; ground truth
    # --- profile-expansion source types (2026-06-01) ---
    "channel_hint",     # a reachability channel declared in person.channel_hints
    "github_repo",      # a public repo from the GitHub /repos surface (body of work)
    "web_search",       # a free-text search result (lowest trust; binding-gated, "possibly" at most)
]


# Account-existence verdict, distinct from smtp_verdict.
# - "verified"            : account exists AND profile is visible to us
#                           (we can cross-check name; strongest belongs signal)
# - "exists_unverifiable" : account exists, profile is NOT visible (Workspace
#                           visibility-restricted account, querying outside org).
#                           Existence is positive belongs evidence; name match
#                           anchor does NOT bind.
# - "not_found"           : explicit not-found response — strong negative
# - "unprobed"            : never asked (cookies missing, budget exhausted,
#                           candidate domain not Google-hosted, prior probe
#                           in same batch was rate-limited, etc.)
AccountExistsVerdict = Literal[
    "verified",
    "exists_unverifiable",
    "not_found",
    "unprobed",
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
class GitHubRepo:
    """One recently-pushed public repo, surfaced in the dossier."""
    name: str                     # "owner/repo" form for display
    description: str | None       # one-line description, may be None
    html_url: str                 # https://github.com/owner/repo
    pushed_at: str                # ISO-8601 timestamp from the API; rendered as-is


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
    # Independent of SMTP: did the Google People API confirm this account exists?
    # See AccountExistsVerdict for semantics. The scorer's _score_deliverable
    # merges signals from both smtp_verdict AND account_exists; either positive
    # signal lifts deliverable. Set by lib.google_account.fetch_google_account.
    account_exists: AccountExistsVerdict = "unprobed"
    # When account_exists=="verified" AND Google returned a profile name, this
    # holds the display name as Google sees it. The scorer compares against
    # the target's name to bind the name_match anchor (or flag a delta if
    # Google's name doesn't match the target's).
    account_display_name: str | None = None
    # When account_exists=="verified" AND Google returned a non-default profile
    # photo, this holds its URL. Surfaced ONLY as a human-review artifact — a
    # person can eyeball it against, e.g., a LinkedIn photo. snoop never compares
    # faces or derives a match signal from it (see SKILL.md scope). Default /
    # placeholder avatars are dropped at capture (a generic silhouette tells you
    # nothing). Set by lib.google_account.fetch_google_account.
    account_photo_url: str | None = None

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

    # Tier 1 dossier — populated by person_resolve from the same
    # GET /users/{handle} call that validates anchors, so it costs zero
    # extra API budget. All optional; renderer skips absent fields.
    # The `gh_` prefix keeps GitHub-sourced fields distinct from
    # cross-validated identity fields above (e.g. `name` here is the
    # canonical target name; `gh_name` is what GitHub's profile says,
    # which may or may not match).
    gh_name: str | None = None
    gh_bio: str | None = None
    gh_blog: str | None = None          # blog/website URL from the profile
    gh_twitter: str | None = None       # twitter_username from the profile
    gh_company: str | None = None       # raw company text (may not match employer.name)
    gh_location: str | None = None
    # Tier 2 dossier (D4-C): top-N recently-pushed non-fork public repos.
    # Fetched by lib.gh_profile.fetch_recent_repos as an opt-in second call.
    gh_recent_repos: list[GitHubRepo] = field(default_factory=list)


@dataclass
class ResolverResult:
    """One resolver's output + status. Lets the pipeline distinguish 'returned empty'
    from 'timed out' from 'capability missing.'"""
    resolver: str                          # "git_emails", "gh_profile", ...
    candidates: list[EmailCandidate]
    status: Literal["ok", "empty", "timeout", "unavailable", "error"]
    elapsed_ms: int | None = None
    error_detail: str | None = None
