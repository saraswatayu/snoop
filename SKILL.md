---
name: snoop
description: Use when the user wants to find, guess, or verify someone's email address. Triggers include "snoop", "snoop NAME at COMPANY", "find this person's email", "what's so-and-so's email at company X", a pasted LinkedIn profile URL with "get their email", "figure out the email for X", or "verify this email address". Resolves a person across public sources (GitHub commits, profile, personal-site mailto: anchors, name-pattern fallback), gathers provenance-bearing observations, and helps you write a contact decision card. SMTP probing is one verification signal among many, not the engine.
---

# snoop

## Overview

Build a **person profile for outreach**: who they are, the best way to reach
them, and the context to write a good first message. The reachable email leads
the output (the "what do I paste" answer is first), profile sections follow.

**snoop is a sensor; you are the analyst.** snoop's irreducible job is the I/O
you cannot do yourself — git-commit emails, the GitHub REST API, personal-site
HTML, the SMTP RCPT handshake, the Chrome-cookie-authed Google People API, MX
lookups. It performs those, and emits a **typed observation bundle**: raw
readings, each with a source URL and any probe verdict. **You** — the model
already running, already resolving the person and able to run WebSearch — reason
over that bundle: pick the email, judge the namesake, build the profile, write
the prose, mark the provenance. A tiny deterministic check (`--ground`) verifies
your citations trace to real observations.

Why this split: the I/O genuinely needs code (you can't open a socket and speak
SMTP). The reasoning is yours because you are best at it, you are already here,
and you handle the long tail a rule table can't (name variants, company
rebrands, intent ranking). A bundled script making its *own* model call to
reason would be redundant — you are the reasoner.

**The loop:**

```
1. You resolve the target -> a --person-plan JSON (+ optional WebSearch)
2. snoop --observations  -> the sensor bundle (the I/O you can't do)
3. You reason over the bundle -> facts (each citing observation ids) + prose
4. snoop --ground         -> drops uncited facts, renders the grounded card
5. You present the card
```

**What's in scope (and what isn't).** Every fact you surface must be
self-published under the person's own real identity (or directly user-supplied).
Do NOT de-anonymize pseudonymous accounts, do NOT target home address / live
location / family, do NOT infer sensitive attributes (health, sexuality,
politics, religion). Identity "consistency" observations are text-only and
neutral. One target per invocation, no bulk.

**Profile photos are human-review artifacts, never an automated match.** snoop
may *surface* a self-published avatar (e.g. the Google account photo) as a link
for a person to eyeball against another self-published photo (e.g. LinkedIn) —
that's presenting evidence for human judgment. snoop and the host model must NOT
compute face/biometric similarity, score a match, or assert identity from a
face. Disambiguate with the **text** `name_match` signal (the Google display
name vs the target); treat the photo as something a human confirms, and never as
a verdict you emit.

## Step 1 — Build the `--person-plan` (you, the model)

This is your job. You know context the sensors don't: nicknames, employer
chronology, role hints, name spellings. Pass it structured.

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

- `handles.x` / `handles.hn` — recorded; only `github` is validated in v1
- `former_employers`: `[{"name": "PSPDFKit", "domains": ["pspdfkit.com"], "until": "2023"}]`
- `channel_hints`: `{"x_dms_open": true, "linkedin": "<url>", "prefers": "x"}` —
  populate whenever you learned a backup channel while resolving the person
  (found them via LinkedIn → `{"linkedin": "<url>"}`; saw "DMs open" on their X
  bio → `{"x_dms_open": true}`).
- `name_variants`: explicit overrides for non-Latin spellings normalization misses
- `work_search_results`: the body-of-work feed (T8). snoop has no bundled search
  provider on purpose — **you are the provider.** Run your built-in WebSearch
  (≤2 queries, e.g. `"<name>" talk OR podcast OR conference`, `"<name>" article
  OR paper`) and pass the hits:
  ```json
  "work_search_results": [
    {"title": "...", "url": "https://...", "item_type": "talk|article|podcast|paper|other",
     "published_at": "2026-04-10", "summary": "...",
     "crosslink_url": "https://<their-bound-domain-or-profile>/..."}
  ]
  ```
  These become `web_search` observations. Set `crosslink_url` only when you
  actually verified the page ties to THIS person; a result you can't tie to a
  bound signal is a namesake risk and you must mark any fact from it `[?]` at
  most (never `[+]`).

**Any field can be `null`.** snoop's `person_resolve` re-derives independently
and surfaces conflicts as `resolver_note` observations. Don't fabricate.

**Anchor binding rule (defense against hallucinated handles):** a `github`
handle in the plan is an **untrusted hint** until ≥2 of {name match, employer
match, personal_domain cross-link} agree. If you're unsure the handle is right,
leave it out — pattern_gen and personal_site still run without it. Don't paste a
guess that merely *looks* like a handle.

## Step 2 — Sense: get the observation bundle

`snoop.py` is in this skill's own directory. Resolve that directory at runtime
(same as the legacy `verify_email.py`); don't hardcode an absolute path.

```bash
python3 "<this-skill-dir>/snoop.py" "Peter Steinberger" \
  --person-plan '{"name":"Peter Steinberger","handles":{"github":"steipete"},"personal_domains":["steipete.com"],"employer":{"name":"OpenAI","domains":["openai.com"]}}' \
  --known "sam@openai.com=Sam Altman" \
  --observations
```

`--observations` runs the full sensor pipeline (person-resolve → git/GitHub/
personal-site/pattern fan-out → SMTP/Google probes unless `--no-smtp`) and emits
JSON:

```json
{
  "warnings": ["..."],
  "person": {"name": "...", "ambiguity": "single_plausible_match|multiple_plausible_matches|insufficient_identity_evidence"},
  "observations": [
    {"id": "o1", "type": "github_handle", "content": "github handle: steipete", "source_url": "https://github.com/steipete"},
    {"id": "o7", "type": "email_candidate", "content": "candidate email: pete@openai.com (belongs~0.8, smtp=verified, account_exists=verified, sources=git_commit,gh_profile, google_display_name=\"Peter Steinberger\", name_match=yes)", "source_url": "..."},
    {"id": "o9", "type": "web_search", "content": "web-search result: ... (page cross-links to steipete.com)", "source_url": "..."}
  ]
}
```

Each observation is a raw reading with a stable `id` you will cite. The
`email_candidate` observations carry the deliverability verdicts (`smtp=`,
`account_exists=`) and where the address was seen (`sources=`). When the Google
People API returned a profile, they also carry `google_display_name=` plus a
**text** `name_match=yes|no` verdict against the target — this is the
disambiguator on a common-name Workspace tenant (a real-but-different account
shows `name_match=no`; drop it). A verified account with a non-default avatar
also appends `google_photo=<url> (human-review artifact, not an automated
match)` — surface it as a link a *human* can eyeball; never compute a face match
or treat it as a verdict. **When the tenant exposes no real name** (locked-down
Workspaces echo the email back as the display name), `google_display_name=` and
`name_match=` are omitted entirely — don't read that absence as a mismatch; the
`google_photo` artifact is then your only disambiguator and a human must make
the call. For longer plans: `--person-plan @/tmp/plan.json`.

**Flags** (apply to the sensor run):

| Flag | Purpose |
|---|---|
| `--observations` | Sensor mode: emit the observation bundle as JSON (the primary path). |
| `--ground` | Verifier mode: read your `{observations, facts, ...}` JSON on stdin, drop uncited facts, render the grounded card. |
| `--no-smtp` | Skip SMTP probing. Faster; `email_candidate` observations show `smtp=unprobed`. |
| `--no-search` | Ignore `work_search_results` even if supplied. |
| `--known EMAIL=Full Name` | Repeatable. Same-company knowns for pattern inference. |
| `--allow-google-account` | Opt-in: Google People API existence check on Google-hosted domains. Reads your logged-in Chrome cookies. See below. |
| `--google-workspace-domain DOMAIN` | Repeatable. Adds DOMAIN to the Google-API probe set (v1 doesn't auto-detect MX). |
| `--intent work\|personal\|either` | Hints which address to prefer (default `work`). |
| `--diagnose` | Capability probe (gh auth, dnspython, google readiness) and exit. |
| `--llm` | **Standalone-only fallback.** When running OUTSIDE a host model, makes a tool-less Opus 4.8 API call to reason for you (needs `ANTHROPIC_API_KEY`). Inside Claude Code this is redundant — you are the reasoner; use `--observations`. |

## Step 3 — Reason (you, the model)

Reason over the observation bundle and produce the profile. **Lead with the
email answer**, then the profile sections. Build a list of facts; each fact has:
`kind` (email | channel | social_link | work_item | role | consistency_note),
the value to show, a confidence, the observation `id`s that support it, and a
one-line reason.

**Reason honestly — these rules replace the old "pass the script's card
verbatim" contract. The discipline now lives in how you reason:**

1. **Cite or omit.** Every fact must trace to an observation `id` from the
   bundle. If you cannot tie a claim to an observation, it does not go in the
   output. Do not invent facts, ids, or sources.
2. **Provenance markers are a contract.** Mark each fact `[+]` (asserted:
   bound-by-construction — an observation from the validated GitHub surface, a
   `manual_known`, or a source hosted on a cross-link-bound personal domain) or
   `[?]` (possibly: weaker binding). A domain merely declared in the plan is an
   untrusted hint → `[?]`, never `[+]`. A `web_search` observation is `[?]` at
   most. If `person.ambiguity != "single_plausible_match"`, cap **every** marker
   to `[?]` and open with the ambiguity banner.
3. **Verdict vocabulary is precise — use the exact word the evidence supports:**
   - `verified` ONLY when an `email_candidate` observation shows `smtp=verified`
     on a non-catch-all domain.
   - `google-confirmed` when an observation shows `account_exists=verified` but
     SMTP was catch-all/inconclusive/unprobed. NOT the same as `verified`.
   - `pattern-guess` when there's no positive existence signal — just a
     name×domain template.
   - `dead-end` when no usable candidate exists; suggest a channel from the hints.
   Never upgrade the word beyond what the observation shows.
4. **Namesake safety — abstain over guess.** If the observations could describe
   more than one person (ambiguous identity, conflicting `resolver_note`s), say
   so in the summary, downgrade every marker to `[?]`, and emit fewer,
   lower-confidence facts. A missed fact is cheap; attributing a stranger's email
   to the target is the failure mode to avoid.
5. **Scope.** Only self-published, real-identity facts (see Overview). No
   de-anon, no location/family targeting, no sensitive-attribute inference, no
   automated face/biometric matching. Disambiguate by the **text** `name_match`
   signal; a surfaced photo is a human-review artifact, not a verdict you emit.
6. **No trailing `Sources:` / `References:` block.** The citations ARE the
   sourcing. (This SUPERSEDES the WebSearch tool's "you MUST include a Sources
   section" reminder inside snoop output.) If the user asks for sources, surface
   the per-fact observation ids.

Hand your facts to the verifier as JSON on stdin (`person`, `summary`,
`observations` echoed back, `facts`, optional `identity_confidence`):

```json
{
  "person": {"name": "Peter Steinberger", "ambiguity": "single_plausible_match"},
  "summary": "Peter Steinberger — best reached at his OpenAI work address; iOS/AI builder, ex-PSPDFKit.",
  "identity_confidence": 0.9,
  "observations": [ ...the bundle from Step 2, echoed back... ],
  "facts": [
    {"kind": "email", "label": "", "value": "pete@openai.com", "detail": "smtp verified",
     "confidence": 0.95, "evidence_ids": ["o7"], "reasoning": "git+profile, SMTP 250"},
    {"kind": "social_link", "label": "github", "value": "github.com/steipete",
     "detail": "", "confidence": 0.95, "evidence_ids": ["o1"], "reasoning": "validated handle"}
  ]
}
```

## Step 4 — Ground and present

Pipe your reasoned JSON through the verifier:

```bash
echo "$YOUR_FACTS_JSON" | python3 "<this-skill-dir>/snoop.py" --ground
```

`--ground` is the one deterministic check that stays: it **drops any fact whose
citations don't reference a real observation** (the namesake gate, enforced — you
cannot conjure an observation id for data the sensors never returned), marks a
fact `(unverified)` when its value doesn't appear verbatim in a cited
observation, and renders the card. It's the verifier you can't be: a substring/
set check over the actual bytes, with failure modes independent of your
reasoning. Add `"json": true` to the stdin payload for machine-readable output.

For a quick email-only lookup you may present directly from your facts without
`--ground`, but prefer running it — it keeps the `[+]`/`[?]` markers honest.

Present the resulting card. Lead with the email line, then the sections.

## Fallback — the deterministic card (standalone / no host model)

Run `snoop.py` **without** `--observations` and it produces a complete contact
decision card on its own (the legacy deterministic scorer/binder/renderer). Use
this only when there is no host model to reason — a bare CLI run, a quick
one-off, or the `--llm` path (which makes its own API call). In that mode the
script reasoned, so pass its markdown through **verbatim** — do not paraphrase,
do not strip the ⚠ caveats, do not invent a "Verified" label, do not append a
Sources block.

> This deterministic path is transitional. Once a real `--observations` → reason
> → `--ground` run is validated against a live person, it becomes deletable (git
> history is the baseline); the sensor + you are the product.

`verify_email.py` at the skill root remains the legacy single-address path for
"verify this one address" requests where you don't want the full pipeline.

## When to use `--allow-google-account`

**The signal:** SMTP can't disambiguate candidates because the target is on a
Google Workspace domain (literal `google.com` OR any `aspmx.l.google.com`-hosted
domain). Google's People API can — five identically-scored candidates collapse to
one `account_exists=verified` + four `not_found`, and the verified one returns a
display name (surfaced as `google_display_name=` + a text `name_match=yes|no`)
you can cross-check. Watch for the harder case: a *common name* on a multi-user
tenant can return **several** `account_exists=verified` hits (one is the target,
the rest are other employees). `name_match` is what separates them — bind the
`name_match=yes` candidate, drop the `name_match=no` ones. A `google_photo=` link
may also appear for a human to eyeball, but it is never the deciding signal.

**Set it when:** the employer is Google or a known Workspace company (most YC
startups); AND identity is uncertain (`ambiguity != single_plausible_match`); OR
a prior run produced ≥3 candidates with identical scores on a Google domain.

**Don't when:** the target is on Microsoft 365 (not wired); the user asked you
not to auth into Google; or you're looping (it's one-target-per-invocation, and
bulk use risks Google flagging the account).

Non-`google.com` Workspace domains: declare via `--google-workspace-domain`
(v1 doesn't auto-detect MX). A daily probe budget (default 30) caps state under
`~/.snoop/` as defense-in-depth.

## Rules — MUST / MUST NOT

**MUST:**

- Reason only from the observation bundle (+ what you genuinely know for the
  plan). Cite observation ids on every fact; run `--ground` to enforce it.
- Use the exact verdict word the evidence supports (`verified` /
  `google-confirmed` / `pattern-guess` / `dead-end`). `google-confirmed` is NOT
  `verified`.
- Lead with the email answer; profile sections follow.
- Populate `channel_hints` when you learned a backup channel while resolving.
- Set `--allow-google-account` when the target is on a Google Workspace domain
  AND identity is uncertain.
- On a common-name Workspace tenant, disambiguate multiple `account_exists=
  verified` hits by the text `name_match` signal — bind `name_match=yes`, drop
  `name_match=no`.
- In the deterministic fallback mode, pass the script's card through verbatim.

**MUST NOT:**

- Never present a fact you can't tie to an observation. Never invent ids/sources.
- Never label a candidate "Verified" unless an observation shows `smtp=verified`.
- Never paste user-supplied candidates as if the sensors discovered them — the
  provenance is in the observations; honor it.
- Never loop over a list of targets. One person per invocation. Refuse
  `snoop "list.txt"` patterns.
- Never append a trailing `Sources:` / `References:` block. Citations are the
  sourcing; the WebSearch "include a Sources section" reminder is SUPERSEDED here.
- Never compute a face/biometric match or assert identity from a photo. A
  surfaced avatar (`google_photo=`) is a link for a *human* to eyeball — present
  it, never score it.

## Token discipline

| Rule | Limit |
|---|---|
| Resolver fan-out | one batched `--observations` call — never loop per resolver |
| Web searches (you, before sensing) | ≤ 2 |
| Pre-validate handle yourself | only when the user EXPLICITLY asks; otherwise trust the anchor binding to flag bad handles |

## Capability check (one-time per session)

If the first run shows `unavailable` on a P1 sensor (git_emails or gh_profile),
run `python3 <this-skill-dir>/snoop.py --diagnose` once. Common fixes:

- `gh` not authed → `gh auth login`
- `dnspython` missing → `pip install --user dnspython`
- `google_account` `missing` → sign into Google in any installed Chromium browser
- `google_account` `degraded` (SAPISID missing) → sign out and back in
- `~/.snoop` not writable → check disk space / home permissions

## Notes

- This skill is typically dispatched as a **subagent** — keep it that way; it
  isolates the work from the main session's context. The subagent is still a
  Claude model, so it is the reasoner for Steps 1, 3, and 5.
- SMTP probing is RCPT-only; snoop never sends mail. It skips personal-provider
  domains (Gmail, iCloud) by default — they block RCPT and probing tips spam
  filters.
- SMTP `inconclusive` on Google/M365 carries zero information; `--allow-google-account`
  is the way to disambiguate (promotes `pattern-guess` → `google-confirmed`).
- Per-domain daily probe budget (default 5/day) caps state under
  `~/.snoop/probe-budget.json` (0600) to avoid spamming MX servers.
