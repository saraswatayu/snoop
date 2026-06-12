# snoop first-run setup wizard — design

**Date:** 2026-06-12
**Status:** approved (brainstormed + /devex-review D1 → build)
**Driver:** DX audit scored first-time onboarding 6.5/10. Top findings: the
undocumented macOS Keychain prompt on first Google-cookie use (the "Keychain
cliff"), consent decided by skill policy instead of the user, and silent sensor
degradation with no first-run verification (a broken `gh` went unnoticed on the
author's own machine).

## Decisions already made

| Decision | Choice |
|---|---|
| Timing | Upfront first-run wizard (not lazy per-capability consent) |
| Scope | Consent + privacy, diagnose-driven: Google cookies, SMTP, ledger |
| Persistence | `~/.snoop/config.json`, CLI-enforced; flags override per-run |
| Build vs docs-only | Build (devex-review D1) |

## Flow

A new **Step 0** at the top of SKILL.md, run in the **main session** (subagents
cannot ask the user anything):

1. `test -f ~/.snoop/config.json` — one cheap Bash check before dispatching any
   lookup.
2. If missing: run `snoop.py --diagnose`, then ask the wizard questions via one
   AskUserQuestion call, then write the config via `snoop.py --init-config`.
3. Report pure setup nudges from diagnose as text (e.g. `gh auth login`,
   `pip install dnspython` with a venv note). Nudges are not questions.
4. Proceed with the lookup (dispatch the subagent as usual).

**Backstop (robust to usage gaps):** when `snoop.py` runs with no config file,
the bundle leads with a `first_run` entry in `resolution_gaps`:
"no ~/.snoop/config.json — defaults in effect; run the first-run wizard."
A host that skips Step 0 gets coached into it by the bundle itself, same
pattern as existing resolution gaps.

## The wizard (one AskUserQuestion call, diagnose-driven)

At most three toggles. Each appears **only if `--diagnose` says it is relevant
on this machine**:

1. **Google-account probing** (only if Chrome-family cookies were found):
   "Use your logged-in Chrome session to check whether Google-hosted addresses
   exist? First use pops a one-time macOS Keychain prompt — 'python wants to
   access Chrome Safe Storage' — that's snoop reading your own cookies."
   Recommended: enable.
2. **SMTP probing** (only if dnspython is installed): "RCPT-only, never sends
   mail, probes from this machine's IP." Recommended: enable.
3. **Ledger:** "local-only yield metadata (~/.snoop/ledger.jsonl) — sensor
   timings and plan shape, never names/addresses/domains." Recommended: enable.

The full wizard question text lives in a reference file
(`references/first-run.md`); SKILL.md Step 0 stays ~15 lines (slim-SKILL.md
principle).

## Config file

`~/.snoop/config.json`, 0600 perms:

```json
{
  "schema": 1,
  "configured_at": "2026-06-12T16:00:00Z",
  "google_account": true,
  "smtp": true,
  "ledger": true
}
```

Written only through a new `snoop.py --init-config '<json>'` entry point so
schema validation and permissions stay in Python; the host model never
hand-writes the file. `--init-config` validates keys/types, rejects unknown
schema versions, writes atomically, sets 0600.

## CLI behavior

**Precedence: per-run flag > config > built-in default.**

- A typed `--allow-google-account` works with or without config (typing the
  flag is consent). `--no-smtp` / `--no-ledger` override per-run.
- **Unconfigured (no file):** built-in defaults are exactly today's behavior
  (smtp on, google off, ledger on) plus the `first_run` gap. Existing CLI
  users and scripts break zero.
- **Configured:** the file's values are the defaults. `google_account: false`
  means the flag-less run never touches cookies; `smtp: false` means flag-less
  runs skip SMTP; etc.
- A key **absent** from the config (e.g. a new gated sensor ships later) is
  treated as unset: built-in default applies and the bundle emits a one-key
  consent gap, so new capabilities get asked about lazily without re-running
  the whole wizard.

## Reconfiguration

- "snoop config" / "reconfigure snoop" → SKILL.md instructs re-running the
  wizard pre-filled from current values (read the file, show current choices).
- Deleting `~/.snoop/config.json` resets to first-run.
- Documented in README.

## Docs changes (in scope)

- README install section gains step 3: first lookup runs a one-time setup;
  CLI-only users can run `--diagnose` themselves. Add a venv/PEP-668 note to
  the pip install line.
- SKILL.md: Step 0 (~15 lines) + the reference file with wizard text.
- The Keychain warning text lives in the wizard question itself, where it
  prevents the Deny.

## Companion fix (separate change, same milestone)

`--ground` currently drops uncited facts silently (`facts: []`, no report).
Add a `dropped` array to both text and JSON output: one entry per dropped or
downgraded fact with a reason (`unknown_observation_id`, `value_not_found`).
This is analyst-loop feedback, independent of the wizard, and ships as its own
commit.

## Out of scope

- Lazy/hybrid consent timing (explicitly decided against).
- Per-sensor enable/disable beyond the three gated capabilities.
- Any LinkedIn/X sensor, bulk mode, or changes to identity-binding rules.
- Migrating existing `~/.snoop/` state (ledger, probe budget) — untouched.

## Testing

- Precedence matrix: flag > config > default for all three keys.
- `--init-config`: valid write (0600, atomic), unknown key rejected, bad type
  rejected, unknown schema rejected.
- `first_run` gap emitted when unconfigured; absent when configured.
- Absent-key → one-key consent gap emission.
- Unconfigured behavior byte-identical to today's defaults (regression guard).
- `dropped` report: bad id, value mismatch, and clean-pass cases.

## Open questions

None. All forks were decided in brainstorming or D1.
