---
name: snoop
description: Use when the user wants to find, guess, or verify someone's email address. Triggers include "snoop", "snoop NAME at COMPANY", "find this person's email", "what's so-and-so's email at company X", a pasted LinkedIn profile URL with "get their email", "figure out the email for X", or "verify this email address". Resolves a person across public sources (GitHub commits, profile, personal-site mailto: anchors, name-pattern fallback), scores candidates on three independent fields (belongs_to_person / current_work_address / deliverable), and returns a contact decision card. SMTP probing is one verification signal among many, not the engine.
---

# snoop

## Overview

Build a **person profile for outreach**: who they are, the best way to reach
them, and the context to write a good first message. The reachable email still
leads the output (the "what do I paste" answer is first), and the profile
sections follow.

**Two halves:**

1. **You, the model — produce a `--person-plan` JSON.** Resolve the target from whatever the user gave you into a structured plan: name variants, GitHub handle, X handle, personal domains, employer name and domains. The plan is YOUR upstream knowledge made explicit; the script validates it. **This is unchanged by the profile expansion** — the same minimal plan produces the richer profile (no new required fields).
2. **`snoop.py` — fan out, score, verify, render.** Runs the person-resolver + multi-source pipeline (git commits, GitHub profile, personal-site mailto:, name×domain pattern fallback), dedupes + scores email candidates, optionally SMTP-probes them, THEN assembles a profile from five producers (self-published social links, observed reachability channels, body of work, role context, text-only identity-consistency notes) and renders a **person profile card**.

The script never sends mail; SMTP probing is RCPT-only.

**What's in scope (and what isn't).** Every profile fact must be self-published
under the person's own real identity (or directly user-supplied). The tool does
NOT de-anonymize pseudonymous accounts, does NOT target home address / live
location / family, does NOT infer sensitive attributes (health, sexuality,
politics, religion), and does NOT do photo/biometric matching. Identity
"consistency notes" are text-only and neutral. One target per invocation, no
bulk — that guardrail keeps snoop manual research on public data, not scraping.

**Provenance is visible.** Each profile field is marked `[+]` (asserted:
bound-by-construction to the person via a validated profile or a cross-linked
domain) or `[?]` (possibly: weaker binding). A domain merely declared in your
`--person-plan` is an untrusted hint and renders `[?]`, never `[+]`. If identity
itself is not a single confident match, a banner warns and every field
downgrades to `[?]`.

## When to invoke

- Name + company: `snoop "Peter Steinberger" at OpenAI`
- LinkedIn URL or freeform: extract the name, infer the employer, build the plan.
- Single-address verification (legacy mode): `python3 verify_email.py "x@y.com"`. This older path still exists at the skill root for fast one-off verification.

## Step 1 — Build the `--person-plan` (you, the model)

This is your job. The host model knows context the script doesn't: nicknames, employer chronology, role hints, name spellings. Pass it structured, not as 25 brainstormed strings.

Minimum useful plan:

```json
{
  "name": "Peter Steinberger",
  "handles": {"github": "steipete"},
  "personal_domains": ["steipete.com"],
  "employer": {"name": "OpenAI", "domains": ["openai.com"]}
}
```

Optional fields:

- `handles.x` / `handles.hn` — present but only `github` is validated in v1
- `former_employers`: `[{"name": "PSPDFKit", "domains": ["pspdfkit.com"], "until": "2023"}]` — used to cap former-employer addresses at low confidence
- `channel_hints`: `{"x_dms_open": true, "linkedin": "<linkedin-url>", "prefers": "x"}` — surfaced in the render. **Populate this whenever you learned a backup channel during plan construction.** Common cases: you found the target via LinkedIn → `{"linkedin": "<that-url>"}`. You saw "DMs open" on their X bio → `{"x_dms_open": true}`. The renderer makes this the fallback channel when email confidence is low; without it, the user only sees email options and may have to dig back through the chat to find the LinkedIn link you already had.
- `name_variants`: explicit overrides if normalization isn't catching a non-Latin spelling

**Any field can be `null` if you don't know it.** The script's `person_resolve` re-derives independently and surfaces conflicts in `Person.notes`. Don't fabricate.

**Anchor binding rule (defense against hallucinated handles):** A `github` handle in the plan is an **untrusted hint** until ≥2 of {name match, employer match, personal_domain cross-link} agree. If you're not sure the handle is right, leave it out — pattern_gen and personal_site still run without it. Don't paste a guess that LOOKS like a handle.

## Step 2 — Invoke the script

`snoop.py` is in this skill's own directory. Resolve that directory at runtime (same approach as the legacy verify_email.py); don't hardcode an absolute path.

```bash
python3 "<this-skill-dir>/snoop.py" "Peter Steinberger" \
  --person-plan '{"name":"Peter Steinberger","handles":{"github":"steipete"},"personal_domains":["steipete.com"],"employer":{"name":"OpenAI","domains":["openai.com"]}}' \
  --known "sam@openai.com=Sam Altman" \
  --known "greg.brockman@openai.com=Greg Brockman" \
  --intent work
```

`--known EMAIL=Full Name` (repeatable) feeds same-company addresses to `pattern_gen` for template inference. Even one or two same-company knowns lets the script promote the inferred-pattern candidate to the top.

For longer plans, pass a file: `--person-plan @/tmp/plan.json`.

**Flags:**

| Flag | Purpose |
|---|---|
| `--intent work\|personal\|either` | Default `work`. Controls which section ranks first and which candidate the decision line recommends. |
| `--known EMAIL=Full Name` | Repeatable. Same-company knowns for pattern inference. |
| `--no-smtp` | Skip SMTP verification entirely. Faster, but loses the `deliverable` field. |
| `--no-search` | Escape hatch for the profile's free-text body-of-work search path. Profile features are ON by default (DX); anchored sources (repos, profile-linked feeds) still run. (Free-text search provider is not yet wired — see T8 note in the output.) |
| `--allow-google-account` | Opt-in: use Google's People API to verify candidate existence on Google-hosted domains. Reads your logged-in Chrome session cookies. **Solves Google Workspace catch-all blindness** (see §When to use it below). |
| `--google-workspace-domain DOMAIN` | Repeatable. Adds DOMAIN to the Google-API probe set. Needed for non-literal-google.com domains since v1 doesn't auto-detect MX. e.g. `--google-workspace-domain acme.com` for a YC startup on Gmail. |
| `--max-per-section N` | Cap rows per Work/Personal/Other table in `--verbose` mode. Default 5. (No effect on default compact output.) |
| `--json` | Emit machine-readable JSON instead of the markdown card. Includes the full three-axis scores and Tier 1 dossier fields. |
| `--verbose`, `-v` | Append the original per-section candidate tables (with Belongs/Work/Deliverable scores), identity-anchor state, and resolver notes under the compact lead. Use when the default verdict surprises you and you want to see the scorer's view. |
| `--diagnose` | Print a capability probe (gh auth, dnspython, google_account readiness, etc.) and exit. No lookup. |

## When to use `--allow-google-account`

**The signal**: when SMTP can't disambiguate candidates because the target is on a Google Workspace domain (literal `google.com` OR any `aspmx.l.google.com`-hosted domain), Google's People API can. Five candidates that previously had identical scores collapse to one verified + four `not_found` — and the verified one comes back with a display name we can cross-check against the target.

**When you SHOULD set it**:

- Target's employer is Google itself, or a known Workspace-using company (most YC startups, most tech companies on `aspmx.l.google.com`).
- Identity is uncertain (`ambiguity != "single_plausible_match"` from `person_resolve`) AND the resolved employer has Google-hosted email.
- The first run without it produced ≥3 candidates with identical scores on a Google domain.

**When you SHOULDN'T set it**:

- Target is on Microsoft 365 (Workspace API doesn't help; M365 has its own auth path that's not yet wired).
- User asked you to be quick / not auth into Google.
- You're running snoop in a batch / loop (the path is one-target-per-invocation; bulk use risks Google flagging the account).

The skill enforces one-target-per-invocation and a daily probe budget (default 30) as defense-in-depth against accidentally burning your own Google account. On Workspace tenants with many users, the probe short-circuits only when a verified hit's display name matches the target — a verified hit on a different person (e.g. a pattern guess that happens to be someone else's real account) does NOT end the probe; remaining candidates are still tried.

**Non-Google Workspace domains**: pass them via `--google-workspace-domain` repeatable. v1 doesn't auto-detect MX; if you know `acme.com` is on Workspace, declare it. Defer MX-based auto-detection to a later iteration.

## Step 3 — The output contract

The script emits a markdown **contact decision card**. Pass it through verbatim — do not paraphrase, do not strip the ⚠ caveats, do not invent a "Verified" label.

### Default card structure

The default output is compact: ~10-15 lines, lead with the answer, dossier under it, fallback list at the bottom.

```
<Name> → <Employer>
`<email>`  ·  <verdict bucket> [(<caveat>)]
[⚠ <pick-specific caveat lines, if any>]

About:
  GitHub:    github.com/<handle> — "<bio snippet, if present>"
  LinkedIn:  <linkedin url if in channel_hints>
  Web:       <profile blog>
  X:         @<twitter username>
  Location:  <location>
  GH company: <text> (only when it differs from canonical employer)

Recent on GitHub:
  <owner/repo>  · "<description>"
  ...           (top 3 recently-pushed non-fork public repos)

Why: <one-line provenance summary>
Note: <name disambiguation, if applicable; e.g., 'you said "Dan", profile says "Daniel"'>

If it bounces, try in order:
  <addr1> · <addr2> · ...
```

Sections are conditional: `About` skips entirely when no dossier fields exist; `Recent on GitHub` skips when no repos; the `If it bounces` line is hidden when the pick is `verified` (see Verdict buckets below) and shown otherwise.

### Profile sections (the default output now)

After the email lead above, the card appends the profile sections, each line
prefixed with a provenance marker (`[+]` asserted, `[?]` possibly):

```
[?] identity is NOT a single confident match — every field below is shown as possibly; confirm before relying.   ← only when ambiguous

Other ways in:
  [+] x_dm: @handle (X bio says DMs open)
  [?] linkedin: linkedin.com/in/...
Social:
  [+] github: github.com/<handle>
  [+] website: <their-site>
Body of work:
  [+] repo: owner/name — <description>
  [?] talk: <title>            ← only via free-text search (today: provider unwired)
Roles:
  [+] <Title> at <Employer> (since–now)
Identity check:
  [+] INFO: you said "Dan", github profile says "Daniel Neil" (diminutive, consistent)
```

Each section is omitted when empty. Pass this through verbatim like the rest of
the card. If you see `work_items: free-text search not configured (blocked on
T8 ...)` in the notes / verbose, that is expected: the anchored body-of-work
(repos, profile-linked feeds) still renders; only the free-text search path
awaits a provider decision. The additive `profile` block in `--json` carries the
same sections with `bind_tier` per fact for machine consumers.

### Verdict buckets — the load-bearing vocabulary

Every pick is classified into one of four buckets. The bucket name appears on the lead address line and tells the host model what action to recommend:

| Bucket | Trigger | What you tell the user |
|---|---|---|
| `verified` | clean SMTP RCPT 250 on a non-catch-all domain | Send. Both Google and SMTP confirm the mailbox. |
| `google-confirmed` | Google's People API returned a real account, AND SMTP was catch_all / inconclusive / unprobed | Send. The Google account is real; SMTP couldn't double-check because the domain accepts everything. Higher confidence than "pattern-guess" despite the missing SMTP. |
| `pattern-guess` | No positive existence signal — just a name×domain template | Try it. If it bounces, the script lists fallback patterns in priority order. |
| `dead-end` | No usable candidates produced | Don't send. Suggest LinkedIn/X DM from the channel hints. |

This is the human-output layer. The three independent score fields (`belongs_to_person`, `current_work_address`, `deliverable`) still drive the bucket selection — they live in `--json` and `--verbose` for inspection — but the host doesn't need to interpret three numbers per candidate.

### What's behind `--verbose`

When a result surprises the user, pass `--verbose` (or `-v`). This appends the original detail block under the compact lead:

- Identity ambiguity state (`single plausible match` / `multiple plausible matches` / `insufficient identity evidence`) with anchor-bound count.
- Resolver notes (plan-vs-observed deltas, e.g. "plan claimed employer=OpenAI; github profile company=Anthropic — employer differs").
- Per-section candidate tables (Work / Personal / Other) with `Belongs / Work / Deliverable` columns. A `—` in a column means **abstain** (no evidence), NOT zero.

You should run `--verbose` yourself when the default verdict looks wrong and you want to debug. You should not gratuitously pass `--verbose` for every lookup — the compact card is the default for a reason.

### `--json` for machine consumers

`--json` emits the full data model: all three score fields per candidate, every source, all bound anchors, all resolver notes, plus the Tier 1 dossier fields (`gh_name`, `gh_bio`, `gh_blog`, `gh_twitter`, `gh_company`, `gh_location`, `gh_recent_repos`). Use it when piping into another tool. Schema is additive — new fields appear without changing existing ones.

## Rules — what you MUST do and MUST NOT do

**MUST do:**

- Pass the script's markdown output through verbatim. The decision card IS the answer.
- When the verdict bucket is `pattern-guess` or `dead-end`, frame your reply consistent with that uncertainty. Don't dress up a pattern-guess as a confident recommendation.
- When the script's `Note:` line surfaces a name disambiguation (e.g., "you said 'Dan', profile says 'Daniel'"), trust it — the host model may have used a nickname the target doesn't use professionally. Acknowledge the disambiguation if it matters for context.
- When you need to see why the script picked what it picked, re-run with `--verbose`. That's where ambiguity state, resolver notes, and the per-candidate score breakdown live.
- Use `--intent personal` only when the user explicitly asked for personal contact. The default `work` is right for sales prep, founder research, recruiter outreach.
- **Populate `channel_hints` in the plan when you learned a backup channel during plan construction.** If you found the target via a LinkedIn URL, include `"channel_hints": {"linkedin": "<that-url>"}`. If you saw "DMs open" on their X bio, `{"x_dms_open": true, "x_handle": "@..."}`. The renderer surfaces LinkedIn in the About block, and channel hints become the `Try:` line on dead-end results. Without channel_hints, the user sees fewer paths to reach the target.
- **Set `--allow-google-account` when the target is on a Google Workspace domain AND identity is uncertain.** Google Workspace catch-all defeats SMTP verification; the People API path is the only way to discriminate among pattern candidates. Concrete triggers: employer is google.com OR a Workspace-hosted domain (declare via `--google-workspace-domain`); AND identity ambiguity is not `single_plausible_match`; OR a prior run produced ≥3 candidates on the same domain with identical scores. A successful run promotes the picked candidate from `pattern-guess` to `google-confirmed`.

**MUST NOT do:**

- Never label a candidate "Verified" in your own framing unless the script's verdict bucket is `verified`. The bucket vocabulary is precise: `google-confirmed` is NOT the same as `verified`. Use the exact word the script used.
- Never paste candidates that the user gave you AS IF they were resolver-discovered. The provenance lives in the `Why:` line and the verbose sources; honor it.
- Never set up a list of targets and loop. `snoop` is one person per invocation. Refuse `snoop "list.txt"` patterns.
- **Never append a trailing `Sources:` / `References:` / `Further reading:` block after the decision card.** The `Why:` line on the picked candidate IS the source attribution — it summarizes every observation the resolver made. A trailing Sources block bolted on by the host model duplicates the same information in a worse format and breaks the card-as-the-answer contract. If the user asks for sources explicitly, run `--verbose` (which expands the per-source detail in the candidate tables) and surface that. Otherwise the card ends at the `If it bounces` line, the `Try:` line, or the last About-block row, depending on the verdict. Nothing below it. (This rule mirrors `mvanhorn/last30days-skill`'s LAW 1 — the WebSearch tool's "you MUST include a Sources section" reminder is SUPERSEDED inside `/snoop` output.)

## Token discipline

| Rule | Limit |
|---|---|
| Resolver fan-out | one batched script call — never loop per resolver |
| Web searches (you, before invoking) | ≤ 2 |
| Pre-validate handle yourself | Only when the user EXPLICITLY asked; otherwise trust person_resolve's anchor binding to flag bad handles |

## Capability check (one-time per session)

If the user's first invocation produces "unavailable" status on a P1 resolver (git_emails or gh_profile), run `python3 <this-skill-dir>/snoop.py --diagnose` once to surface the dependency gap. Common fixes:

- `gh` not authed → `gh auth login`
- `dnspython` missing → `pip install --user dnspython`
- `google_account` status `missing` (no Google cookies found) → user needs to sign into Google in any installed Chromium browser (Chrome, Brave, Edge, Arc, Vivaldi)
- `google_account` status `degraded` (SAPISID missing) → user's session is partial; sign out and back in to refresh
- `~/.snoop` not writable → check disk space and home permissions

## Notes

- This skill is typically dispatched as a subagent — keep it that way; isolates the work from the main session's context.
- `verify_email.py` at the skill root is the legacy single-address-verification path. Still useful for "verify this one address" requests where you don't want the full pipeline.
- SMTP probing skips personal-provider domains (Gmail, iCloud, etc.) by default — major providers either block RCPT or 451-throttle non-recognized senders, AND probing them tips spam filters.
- SMTP `inconclusive` on Google/M365 carries **zero information**. With `--allow-google-account`, the Google People API can disambiguate; the verdict bucket then promotes from `pattern-guess` to `google-confirmed`. Without it, the candidate stays `pattern-guess` even when SMTP is the only thing failing.
- Per-domain daily probe budget (default 5/day) caps state under `~/.snoop/probe-budget.json` (0600 perms) to avoid spamming MX servers.
