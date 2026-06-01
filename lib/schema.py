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

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Union


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


# ---- profile expansion (2026-06-01) -----------------------------------------
# snoop's deliverable grows from "an email" to "a person profile." Every fact
# we surface is a Contribution: a provenance-bearing claim about the target,
# tagged with its `kind` and a `bind_tier` saying how confident we are it
# belongs to THIS person (see lib.binding). EmailCandidate is the original
# Contribution (kind="email") and keeps its richer 3-axis scoring; the new
# kinds use the simpler bind_tier model.

# How strongly a fact is tied to the resolved person:
#   "asserted" : source is bound-by-construction — cross-linked from a validated
#                profile (e.g. GitHub blog→domain) or directly user-supplied.
#   "possibly" : weaker per-result binding (e.g. a free-text search hit that
#                cleared the anchor gate but isn't bound-by-construction).
#   "unbound"  : no binding evidence — caller should drop, not render.
# NOTE: a domain merely DECLARED in the model-produced --person-plan is an
# untrusted hint, NOT bound-by-construction (outside-voice Codex #2).
BindTier = Literal["asserted", "possibly", "unbound"]

ContributionKind = Literal[
    "email",             # EmailCandidate — the original deliverable
    "work_item",         # a repo / article / talk / podcast / paper
    "channel",           # an observed public reachability channel
    "social_link",       # a social profile the person linked themselves
    "role",              # employer / title / tenure fact
    "consistency_note",  # text-only identity-consistency observation
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

    # Contribution discriminant. EmailCandidate is the original/canonical
    # Contribution; `kind` lets the merge step dispatch it uniformly with the
    # new profile fact types. Appended (defaulted) so existing construction is
    # unaffected.
    kind: ContributionKind = "email"


# ---- profile fact types (new Contribution kinds) ----------------------------
# Each carries `sources` (provenance), a `bind_tier` (set by lib.binding), and
# `bind_reasons` (renderable receipts), mirroring EmailCandidate's shape so the
# renderer and merge step can treat all contributions uniformly.


@dataclass
class WorkItem:
    """Something the person published: a repo, article, talk, podcast, paper."""
    title: str
    kind: ContributionKind = "work_item"
    url: str | None = None
    item_type: Literal["repo", "article", "talk", "podcast", "paper", "other"] = "other"
    published_at: str | None = None      # ISO date if known
    summary: str | None = None
    sources: list[Source] = field(default_factory=list)
    bind_tier: BindTier = "unbound"
    bind_reasons: list[str] = field(default_factory=list)


@dataclass
class Channel:
    """An OBSERVED public reachability channel — not a ranked 'best way in'
    (outside-voice Codex #7: we can observe channels, not reliably rank intent
    fit). `rank_hint` is an optional ordering signal, evidence-based, not a
    promise."""
    channel_type: str                     # email|x_dm|linkedin|bluesky|contact_form|calendly
    value: str                            # the address / url / handle
    kind: ContributionKind = "channel"
    evidence: str | None = None           # e.g. "X bio says DMs open"
    rank_hint: float | None = None        # optional, observed ordering signal
    sources: list[Source] = field(default_factory=list)
    bind_tier: BindTier = "unbound"
    bind_reasons: list[str] = field(default_factory=list)


@dataclass
class SocialLink:
    """A social profile the person linked from their OWN profile/site. No
    inference: only links the person published about themselves."""
    platform: str                         # github|x|linkedin|bluesky|mastodon|instagram|...
    url: str
    kind: ContributionKind = "social_link"
    handle: str | None = None
    sources: list[Source] = field(default_factory=list)
    bind_tier: BindTier = "unbound"
    bind_reasons: list[str] = field(default_factory=list)


@dataclass
class RoleFact:
    """An employer / title / tenure fact, optionally with company context."""
    employer: str
    kind: ContributionKind = "role"
    title: str | None = None
    since: str | None = None
    until: str | None = None              # None == current
    summary: str | None = None           # what the company does / why-now context
    sources: list[Source] = field(default_factory=list)
    bind_tier: BindTier = "unbound"
    bind_reasons: list[str] = field(default_factory=list)


@dataclass
class ConsistencyNote:
    """Text-only identity-consistency observation (narrowed from "anti-catfish"
    per outside-voice Codex #4 — NO photo/image/reverse-image matching). A
    mismatch is neutral evidence, never a scary "FAKE?" verdict.
    e.g. 'GitHub name "Daniel Neil" vs plan "Dan" — diminutive, consistent'."""
    note: str
    kind: ContributionKind = "consistency_note"
    severity: Literal["info", "mismatch"] = "info"
    sources: list[Source] = field(default_factory=list)
    bind_tier: BindTier = "unbound"
    bind_reasons: list[str] = field(default_factory=list)


# The tagged union (outside-voice D2): a heterogeneous list of these flows from
# resolvers; the merge step dispatches on `.kind`.
Contribution = Union[
    EmailCandidate, WorkItem, Channel, SocialLink, RoleFact, ConsistencyNote,
]


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
    # Profile expansion: resolvers that produce non-email facts return them here
    # as a tagged Contribution list (email resolvers keep using `candidates`
    # during the migration; both feed the same Profile). Defaulted so existing
    # resolvers and their tests are unaffected. Typed as a covariant Sequence so
    # a resolver may hand back e.g. a list[SocialLink] without an invariance
    # complaint; nothing mutates this list in place (consumers read + dispatch).
    contributions: Sequence[Contribution] = field(default_factory=list)


# ---- Identity + Profile (the profile-expansion deliverable) ------------------

# `Identity` is the resolved-person record. Today it is `Person` (which already
# holds exactly the identity fields plus the gh_* dossier). The eng plan's
# "slim Person down to identity-only" is a follow-up refactor kept separate so
# each commit stays green; new code should reference `Identity`.
Identity = Person


@dataclass
class Profile:
    """The profile-expansion deliverable: a resolved identity plus typed,
    provenance-bearing sections. The email candidates remain their own list
    (the original product); the new sections carry the expansion.

    `add()` is the merge primitive (D2): dispatch a Contribution into its
    section by `.kind`. `contributions()` flattens back for uniform iteration."""
    identity: Person
    emails: list[EmailCandidate] = field(default_factory=list)
    work_items: list[WorkItem] = field(default_factory=list)
    channels: list[Channel] = field(default_factory=list)
    social_links: list[SocialLink] = field(default_factory=list)
    roles: list[RoleFact] = field(default_factory=list)
    consistency_notes: list[ConsistencyNote] = field(default_factory=list)

    def add(self, c: Contribution) -> None:
        """Dispatch a contribution into the right section by kind."""
        bucket = {
            "email": self.emails,
            "work_item": self.work_items,
            "channel": self.channels,
            "social_link": self.social_links,
            "role": self.roles,
            "consistency_note": self.consistency_notes,
        }[c.kind]
        bucket.append(c)

    def contributions(self) -> list[Contribution]:
        """All facts as a flat list, in a stable section order."""
        return [
            *self.emails, *self.work_items, *self.channels,
            *self.social_links, *self.roles, *self.consistency_notes,
        ]
