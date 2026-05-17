---
name: snoop
description: Use when the user wants to find, guess, or verify someone's email address. Triggers include "snoop", "snoop NAME at COMPANY", "find this person's email", "what's so-and-so's email at company X", a pasted LinkedIn profile URL with "get their email", "figure out the email for X", or "verify this email address". Generates a ranked candidate list from name patterns plus light research, then verifies the whole list in ONE batched SMTP call and reports a verified hit or the best guess.
---

# snoop

## Overview

Find a person's work email and verify it. Two halves:

1. **Idea generation (you, the model)** — turn whatever the user gives you
   into a ranked list of candidate addresses, informed by *light* research.
2. **Verification (`verify_email.py`)** — verifies the whole ranked list in
   **one call** (batch mode): one MX lookup + one catch-all probe per domain,
   one reused SMTP connection, early-stop on the first verified hit. You do
   **not** loop.

It only does SMTP `RCPT` probing. It never sends mail. Use for legitimate
outreach / verification only.

## Token discipline

| Rule | Limit |
|---|---|
| Verification calls | exactly **one** batch call — never per-candidate |
| Web searches | ≤ 2 |
| `catch_all` / `inconclusive` result | stop, report best-guess — don't re-research |

## Inputs (flexible)

Accept whatever the user pastes:

- A LinkedIn profile URL → extract the person's name; infer the company.
- Freeform ("Peter Bogard-Johnson, works at Jane Street").
- Name + company/domain directly.
- A single address to just verify → one positional arg (single mode).

If the **target domain** isn't given, resolve it (company site → its mail
domain). Ask the user only if you genuinely cannot determine it. If the
company plausibly uses more than one domain (e.g. `.com` vs `.dev`/`.ai`),
include candidates for **each** — the batch call handles multiple domains in
one shot, so you don't pay extra turns to discover the right TLD.

## Step 1 — Light research

Spend at most ~1–2 searches to re-rank, not to be exhaustive:

- WebSearch the company's known email pattern (e.g. "acme.com email format",
  a published staff/press address that reveals the pattern).
- Note the MX provider if obvious. Google Workspace / Microsoft 365 usually
  block RCPT → expect to land on best-guess; don't over-invest.
- Capture name spelling variants (hyphenated surnames, anglicized first
  names, middle names, diacritics → ascii).

Then stop researching and move on.

## Step 2 — Generate the ranked list

Ordered, most-likely first, research-derived pattern on top. Cover the usual:

```
first.last  flast  first  firstl  f.last  last.first
firstlast  lastf  first_last  f-last  initials
```

Hyphenated / multi-part surnames: include each part alone, joined, and
hyphenated. Add candidates on every plausible domain. Cap ~15–25 total.

## Step 3 — One batched verification call

`verify_email.py` is bundled in **this skill's own directory** (the
directory this SKILL.md lives in). Resolve that directory and call the
script there — do not hardcode an absolute path, the skill may be installed
personally (`~/.claude/skills/`) or per-project (`.claude/skills/`).

You already know this skill's directory — it's where you read this
SKILL.md from. Use that path. Write the candidate list to a temp file
(one per line) and call once, e.g.:

```bash
python3 "<this-skill-dir>/verify_email.py" --file /tmp/snoop_candidates.txt
```

(or pass candidates as args instead of `--file`). Requires `dnspython`; if
the script reports it missing, install it for the current user only
(`pip install --user dnspython`) — do not install packages globally.

The script returns one JSON object: `result`
(`verified` | `catch_all` | `inconclusive` | `exhausted`), `hit` (the
verified address or null), and `tested` (per-candidate verdicts). It already
does the catch-all sentinel, connection reuse, early-stop on verified, and
skips dead domains. Do not re-run it candidate-by-candidate.

Only make a second call if the result is `exhausted` AND you have a
genuinely different, better-researched candidate set — not to retry the
same patterns.

## Step 4 — Report

**If there's a clear answer** (a verified hit): one line, nothing else —
`jane.doe@acme.com — Verified`.

**If no single answer is obvious** (catch-all / inconclusive / exhausted):
a table, one row per plausible candidate, ranked best first:

| Email | Confidence |
|---|---|
| jane.doe@acme.com | High |
| jdoe@acme.com | Medium |
| j.doe@acme.com | Low |

Confidence ∈ **Verified** / **High** / **Medium** / **Low**. No MX, no SMTP
codes, no ruled-out list, no prose — unless the user asks. Never label a
guess "Verified".

## Notes

- This skill is typically dispatched as a subagent — good, keep it that way;
  it isolates the work from the main session's context.
- Batch mode does one catch-all sentinel + one SMTP connection **per
  domain** and reuses it — far fewer probes than the old per-candidate loop.
- Sequential by design (no parallel hammering of a mail server — that looks
  abusive and triggers rate limiting).
- `verify_email.py` still supports single mode (one positional email) for
  ad-hoc "verify this address" requests.
- Verification uses SMTP `RCPT` probing + catch-all detection, deliberately
  not the unreliable (and widely disabled) SMTP `VRFY` command.
