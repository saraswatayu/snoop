---
name: snoop
description: Use when the user wants to find, guess, or verify someone's email address. Triggers include "snoop", "snoop NAME at COMPANY", "find this person's email", "what's so-and-so's email at company X", a pasted LinkedIn profile URL with "get their email", "figure out the email for X", or "verify this email address". Resolves a person across public sources (GitHub commits, profile, personal-site mailto: anchors, name-pattern fallback), scores candidates on three independent fields (belongs_to_person / current_work_address / deliverable), and returns a contact decision card. SMTP probing is one verification signal among many, not the engine.
---

# snoop

## Overview

Find a person's reachable email and tell the user whether and how to use it.

**Two halves:**

1. **You, the model — produce a `--person-plan` JSON.** Resolve the target from whatever the user gave you into a structured plan: name variants, GitHub handle, X handle, personal domains, employer name and domains. The plan is YOUR upstream knowledge made explicit; the script validates it.
2. **`snoop.py` — fan out, score, verify, render.** Runs the person-resolver + multi-source pipeline (git commits, GitHub profile, personal-site mailto:, name×domain pattern fallback) in parallel with per-resolver timeouts, dedupes across sources, scores each candidate on three fields, optionally SMTP-probes the top work candidates, and emits a markdown decision card.

The script never sends mail; SMTP probing is RCPT-only.

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
| `--max-per-section N` | Cap rows per Work/Personal/Other section. Default 5. |
| `--json` | Emit machine-readable JSON instead of the markdown card. |
| `--diagnose` | Print a capability probe (gh auth, dnspython, etc.) and exit. No lookup. |

## Step 3 — The output contract

The script emits a markdown **contact decision card**. Pass it through verbatim — do not paraphrase, do not strip the ⚠ caveats, do not invent a "Verified" label.

The card has these sections in this order:

1. **Header** — name, employer, identity ambiguity label (`single plausible match` / `multiple plausible matches` / `insufficient identity evidence`), count of validating anchors bound.
2. **Resolver notes** (if any) — plan-vs-observed deltas the resolver caught.
3. **Decision** — opinionated top recommendation with reason-to-trust and ⚠ caveats (SMTP inconclusive, catch-all, former employer, etc.).
4. **Work** / **Personal** / **Other** tables (order depends on `--intent`).
5. **Channel hints** (if present in plan).

The three score columns:

| Field | What it means | Abstention (`—`) |
|---|---|---|
| **Belongs** | belongs_to_person: is this actually this person's address? | No sources observed |
| **Work** | current_work_address: is this a current work email? | No employer info / unrelated domain |
| **Deliverable** | will a message sent here reach a human? | SMTP inconclusive (no information) OR unprobed |

A `—` in a column means **abstain** (no evidence either way), NOT zero. Render exactly as the script outputs.

## Rules — what you MUST do and MUST NOT do

**MUST do:**

- Pass the script's markdown output through verbatim. The decision card IS the answer.
- When the script reports `ambiguity != "single_plausible_match"`, surface that in your own framing too. Never offer a confident single recommendation if the resolver flagged identity uncertainty.
- When the script's resolver notes contain a plan-vs-observed delta (e.g. "plan claimed employer=OpenAI; github profile company=Anthropic — employer differs"), surface it to the user.
- Use `--intent personal` only when the user explicitly asked for personal contact. The default `work` is right for sales prep, founder research, recruiter outreach.
- **Populate `channel_hints` in the plan when you learned a backup channel during plan construction.** If you found the target via a LinkedIn URL, include `"channel_hints": {"linkedin": "<that-url>"}`. If you saw "DMs open" on their X bio, `{"x_dms_open": true, "x_handle": "@..."}`. The renderer surfaces these as the fallback channel when email confidence is low — don't recreate this as freeform prose at the end of your response when the structured data could have rendered it cleanly.

**MUST NOT do:**

- Never label a candidate "Verified" unless the script's `smtp_verdict == "verified"` (clean RCPT 250 on a non-catch-all domain).
- Never replace a `—` (abstention) with a fabricated score. Especially not for `deliverable` when SMTP was inconclusive.
- Never paste candidates that the user gave you AS IF they were resolver-discovered. The provenance lives in the Source records; honor it.
- Never set up a list of targets and loop. `snoop` is one person per invocation. Refuse `snoop "list.txt"` patterns.
- **Never append a trailing `Sources:` / `References:` / `Further reading:` block after the decision card.** The "Found via" annotation on each candidate row IS the sources list — it cites every URL the resolver pulled the address from. A trailing Sources block bolted on by the host model duplicates the per-row provenance and breaks the card-as-the-answer contract. If the user asks for sources explicitly, then surface them — otherwise the card ends at the last row of the last table (or the Channel hints line, if present). Nothing below it. (This rule mirrors `mvanhorn/last30days-skill`'s LAW 1 — the WebSearch tool's "you MUST include a Sources section" reminder is SUPERSEDED inside `/snoop` output.)

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
- `~/.snoop` not writable → check disk space and home permissions

## Notes

- This skill is typically dispatched as a subagent — keep it that way; isolates the work from the main session's context.
- `verify_email.py` at the skill root is the legacy single-address-verification path. Still useful for "verify this one address" requests where you don't want the full pipeline.
- SMTP probing skips personal-provider domains (Gmail, iCloud, etc.) by default — major providers either block RCPT or 451-throttle non-recognized senders, AND probing them tips spam filters.
- SMTP `inconclusive` on Google/M365 carries **zero information**. The renderer makes this explicit so the user understands why `deliverable` is `—` even when `belongs` is high.
- Per-domain daily probe budget (default 5/day) caps state under `~/.snoop/probe-budget.json` (0600 perms) to avoid spamming MX servers.
