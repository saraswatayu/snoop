# snoop

[![skills.sh](https://skills.sh/b/saraswatayu/snoop)](https://skills.sh/saraswatayu/snoop)

A [Claude Code](https://claude.com/claude-code) **skill** that builds a
**contact profile for outreach** — who someone is, the best email to reach them,
and the context for a good first message.

**snoop is a sensor; the host model is the analyst.** Its irreducible job is the
I/O a model can't do itself — GitHub commits, the GitHub profile + README,
personal-site `mailto:` anchors, the SMTP `RCPT` handshake, the Google People
API, MX lookups, and (as a fallback) name × domain pattern guessing. Give it a
name + company (or a LinkedIn URL, or a bare email to verify), and it emits a
typed **observation bundle**: raw readings, each with a `source_url`, a
structured `data` field, and any probe verdict. The host model (Claude Code,
already running) reasons over that bundle — picks the email, judges the
namesake, writes the prose — and `snoop --ground` deterministically checks that
every claim cites a real observation before rendering the card.

```
plan → snoop --observations → (you reason) → snoop --ground → present
```

Scope is deliberately bounded: only self-published, real-identity, source-bound
facts. No pseudonym de-anonymization, no home-address / location / family
targeting, no sensitive-attribute inference, no photo/biometric matching. One
target per invocation, no bulk. SMTP `RCPT` probing is one verification signal
among many; **it never sends mail.**

## What's in here

| File | Purpose |
|---|---|
| `SKILL.md` | The skill itself — the loop Claude Code follows. |
| `snoop.py` | Entry point. `--observations` (sensor, the default) and `--ground` (verifier) are the modes; `--verify EMAIL` checks one address. |
| `lib/reason.py` | `build_evidence()` — flattens the resolved person + probed candidates into the observation bundle. |
| `lib/ground.py` | The deterministic verifier — drops facts whose citations don't reference a real observation. |
| `lib/render.py` | Renders the grounded card for `--ground`. |
| `lib/` | Sensors: `git_emails`, `gh_profile`, `gh_search`, `personal_site`, `pattern_gen`, `package_registry`, `hn_profile`, `rel_me`, `pgp_keyserver`, `google_account` (+ `chrome_cookies`), `verify_smtp`, `person_resolve`; plus `fetch` (SSRF-guarded HTTP), `ledger` (local yield metadata), `normalize`, `diagnose`, `schema`. |
| `tests/` | pytest suite (`python3 -m pytest tests/`, no network); `_http_harness.py` is the shared fetch fake; `calibration.py` is the manual measurement harness. |
| `requirements.txt` | One dependency: `dnspython`. |

## Install (as a Claude Code skill)

```bash
git clone https://github.com/saraswatayu/snoop.git ~/.claude/skills/snoop
pip install -r ~/.claude/skills/snoop/requirements.txt
```

The one dependency is `dnspython` (for MX lookups). If that `pip install` fails or
is skipped, snoop still runs — it **degrades honestly**: the SMTP sensor reports
`skipped: dependency dnspython missing` instead of silently returning nothing, and
`--diagnose` tells you how to fix it (`pip install --user dnspython`). Everything
that doesn't need MX (GitHub, personal-site, pattern, PGP, rel=me) works as usual.

snoop keeps a small local **ledger** at `~/.snoop/ledger.jsonl` (on by default):
one line per run recording yield metadata only — which sensors ran, how long they
took, the plan *shape* (booleans), and the MX class — **never** names, addresses,
handles, or domains (a CI test enforces that schema). It's how probe ordering gets
tuned from real use over time. Opt out per run with `--no-ledger`; `--diagnose`
reports its health.

Then in Claude Code just say things like:

- `snoop Jane Doe at acme.com`
- `find the email for <LinkedIn profile URL>`
- `verify jane.doe@acme.com`

The skill takes it from there: builds a `--person-plan`, runs the sensors,
reasons over the bundle, grounds the result, and returns the contact card.

## Run the sensors directly

```bash
python3 snoop.py "Peter Steinberger" \
  --person-plan '{"name":"Peter Steinberger","handles":{"github":"steipete"},"personal_domains":["steipete.com"],"employer":{"name":"OpenAI","domains":["openai.com"]}}' \
  --allow-google-account \
  --out /tmp/snoop-obs.json
```

This writes the observation bundle to the file and prints the ready-to-run
`--ground` command. Verify a single address with `snoop.py --verify
jane@acme.com` (or a bare email positional, `snoop.py jane@acme.com`).

Useful flags (full list in `--help` or `SKILL.md`):

| Flag | Purpose |
|---|---|
| `--out PATH` | Write the bundle to a file and print the `--ground` command (so the host model never re-types the bundle). |
| `--verify EMAIL` | Verify one address (repeatable); skip discovery. |
| `--ground` / `--observations-file PATH` | Read `{person, summary, facts}` on stdin, load observations from PATH, drop uncited facts, render the card. |
| `--known EMAIL=Full Name` | Repeatable. Same-company knowns for pattern inference. |
| `--no-smtp` | Skip SMTP verification. |
| `--no-pgp` | Skip the keys.openpgp.org owner-UID corroboration of discovered addresses. |
| `--no-ledger` | Don't append this run's yield metadata to `~/.snoop/ledger.jsonl`. |
| `--deadline SEC` | Shared wall-clock budget for the whole run (default 60s); a sensor still running at the deadline is abandoned and reports `deadline-exceeded`. |
| `--allow-google-account` | Opt-in: Google People API existence check on Google-hosted domains, via logged-in Chrome cookies. Always safe to pass — a no-op when there are no Google candidates or no cookies. |
| `--google-workspace-domain DOMAIN` | Rarely needed — Google MX is auto-detected. Force a domain that isn't already a candidate. |
| `--diagnose` | Capability probe (gh auth, dnspython, Google readiness, ledger health) and exit. |

## The observation bundle

`snoop --observations` emits JSON. Each observation has a stable `id`, a typed
`content` line, and (for email candidates) a structured `data` mirror:

```json
{
  "person": {"name": "Peter Steinberger", "ambiguity": "single_plausible_match"},
  "observations": [
    {"id": "o1", "type": "github_handle", "content": "github handle: steipete",
     "source_url": "https://github.com/steipete"},
    {"id": "o7", "type": "email_candidate",
     "content": "candidate email: pete@openai.com (smtp=verified, account_exists=verified, sources=git_commit,gh_profile, google_display_name=\"Peter Steinberger\", name_match=yes)",
     "data": {"address": "pete@openai.com", "smtp": "verified", "account_exists": "verified",
              "sources": [{"type": "git_commit", "url": "...", "detail": "..."},
                          {"type": "gh_profile", "url": "...", "detail": "..."}],
              "google_display_name": "Peter Steinberger", "name_match": true}}
  ]
}
```

The host model reads fields off `data`, picks the email, and hands its facts
(each citing observation `id`s) to `--ground`. The three verdict words the model
uses map to the evidence: **verified** (clean SMTP RCPT 250), **google-confirmed**
(Google People API confirms the account, SMTP couldn't), and **pattern-guess** (no
positive existence signal). When nothing is usable the model emits no email fact
at all — a **dead-end** — and suggests a channel from the hints; dead-end is an
outcome, not a value of the verdict field (`lib.schema.EMAIL_VERDICTS` has three).

## How SMTP verification works

Per domain: **one** MX lookup, **one** catch-all sentinel probe (RCPT a random
non-existent localpart), and **one** reused SMTP connection for all candidates.
Stops early on the first verified hit.

The pipeline skips personal-provider domains (Gmail, iCloud, Outlook, Proton,
etc.) by default — those either block `RCPT` or 451-throttle non-recognized
senders, and probing them tips spam filters. Google Workspace / Microsoft 365
commonly return `inconclusive` on `RCPT` — reported honestly, never as
"verified." For Workspace-hosted domains, `--allow-google-account` adds a Google
People API existence check that discriminates where SMTP can't.

A per-domain daily probe budget (default 5/day, JSON state under
`~/.snoop/probe-budget.json`, 0600 perms) caps SMTP probes. This uses SMTP
`RCPT` + catch-all detection, deliberately *not* the unreliable and widely
disabled SMTP `VRFY` command.

## Identity binding (why it won't email a namesake)

Before any deliverability probe, snoop decides whether an address actually
*belongs* to the target. A candidate **binds** only when ≥2 independent signals
agree — observed on a bound surface (a validated GitHub account, or a personal
site on a rel=me-verified domain), the resolved employer domain, rel=me domain
ownership, or a PGP owner-UID. **SMTP fires only on bound candidates** — it opens a
socket to the mailbox, so snoop never RCPT-probes a same-named stranger; when
nothing binds it opens no socket and renders an honest blank (what it checked, what
it didn't, and why). The **Google People API existence check splits off (ENG-9)**:
it's an authed call through the user's own cookies — no socket — and the only
disambiguator on a catch-all Workspace domain, so it *also* runs on the unbound
pattern guesses on a Google-hosted domain, collapsing them to the one account that
exists. A pure name×domain guess never binds (so never SMTP-probed), but on a
Workspace tenant its existence is still checkable. When several addresses verify,
snoop clusters them by **Gaia (Google account) id**: same id = aliases of one
person (collapse — no namesake), different ids = distinct accounts (a real
collision to split). That answers *same-person?* even on a locked tenant that
returns no display name — though never *the-right-person?* on its own, and locked
tenants return the id only intermittently. `--ground` then checks the citations
from the other side, so two independent verifiers police two axes — identity (does
this address belong to the person?) and provenance (does each claim cite a real
observation?).

## Calibration (how the numbers are measured)

snoop's hit rates are **measurements, not accuracy claims** — reported as "measured
on N targets of these classes, on DATE," never as a guarantee for the next lookup.
The harness (`python3 -m tests.calibration`) computes per-sensor hit rate and
precision-on-known over a labeled target set.

Privacy is built into the protocol: the committed fixture
(`tests/fixtures/calibration_targets.json`) holds only EXAMPLE/synthetic archetype
shapes with **null** ground truth — a CI test enforces that no real address ever
lands in the public file. The real N≥25 public-trail targets and their known
addresses live ONLY in the gitignored `tests/fixtures/calibration_targets.local.json`,
so the exact published numbers reproduce only against that local set. **Retention:**
a local ground-truth entry is deleted on the subject's request (remove their row).

> **Status:** the measured per-sensor table is produced by a human-supervised live
> run against the local N≥25 set (the run touches real people and the network, so
> it isn't part of the automated suite). Until that run is recorded here, treat the
> sensors as un-benchmarked rather than assuming a number.

## License

MIT — see [LICENSE](LICENSE).
