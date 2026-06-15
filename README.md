# snoop

[![skills.sh](https://skills.sh/b/saraswatayu/snoop)](https://skills.sh/saraswatayu/snoop)

You found them on LinkedIn. Now you need the email — the real one, that reaches
*them* and not a namesake, and won't bounce on the cold first message you only
get to send once.

snoop is a [Claude Code](https://claude.com/claude-code) skill that finds it. It
does the I/O Claude can't — git commits, the GitHub API, a personal site's
`mailto:`, the SMTP `RCPT` handshake, MX lookups, the Google People API — and
hands Claude typed evidence. Claude reasons over it; snoop checks that every
claim cites something real before it renders the card.

snoop is the sensor. Claude is the analyst.

```
plan → snoop --observations → (Claude reasons) → snoop --ground → present
```

## Why you can trust the answer

Every email finder gives you one number. *Confidence: 85%.* That number fuses
three different questions into one digit:

- **Deliverability** — does the mailbox exist and accept mail?
- **Identity** — is it the right person, or a same-named stranger?
- **Provenance** — where did we learn this, and can we point at the source?

snoop keeps them apart, because they fail apart.

**Deliverability** is the verdict word on the email, and it means exactly one
thing:

- `verified` — a clean SMTP `RCPT` 250 from the mailbox.
- `google-confirmed` — the Google People API confirms the account exists *and* the name matches, where SMTP came back inconclusive.
- `pattern-guess` — a name×domain guess with no positive existence signal.

No blend, no percentage. When nothing is usable, snoop emits no email at all — an
honest blank that tells you what it checked, what it didn't, and why.

**Identity** is a separate question, answered before any probe fires (see
[Identity binding](#identity-binding--why-it-wont-email-a-namesake) below). snoop
won't open a socket to a same-named stranger's mailbox, and it won't hand you an
address it can't tie to the person.

**Provenance** is the third axis. Every fact carries citations, and a second,
deterministic pass — `snoop --ground` — drops any fact whose citations don't
resolve to a real observation, and flags any whose value doesn't appear in what it
cites. It checks the receipts, not the wording. Two independent verifiers, two
axes: identity decides whether an address belongs to the person; grounding decides
whether each fact is attributable at all.

The scope is bounded on purpose. snoop reads only self-published, real-identity,
source-bound facts. It won't de-anonymize a pseudonym, infer a home address or
location or family, guess a sensitive attribute, or match a face. One target per
invocation — no bulk. And it never sends mail: the SMTP `RCPT` handshake asks the
server whether a mailbox exists, then hangs up. It opens the envelope and never
puts a letter inside.

Restraint isn't a limitation here; it's the product.

## Install

```bash
npx skills add saraswatayu/snoop
```

That drops the skill where Claude Code looks for it. snoop has one optional Python
dependency — `dnspython`, for MX lookups:

```bash
pip install --user dnspython
```

Skip it and snoop still runs. It degrades honestly: the SMTP sensor reports
`skipped: dependency dnspython missing` instead of returning a silent blank, and
`--diagnose` tells you how to fix it. Everything that doesn't need MX — GitHub,
personal-site, pattern, PGP, rel=me — works either way.

snoop keeps a small local **ledger** at `~/.snoop/ledger.jsonl` (on by default):
one line per run, yield metadata only — which sensors ran, how long they took, the
plan *shape* (booleans), the MX class. Never names, addresses, handles, or
domains; a CI test enforces that schema. It's how probe ordering gets tuned from
real use. Opt out per run with `--no-ledger`; `--diagnose` reports its health.

Working on snoop itself? Clone it instead:

```bash
git clone https://github.com/saraswatayu/snoop.git ~/.claude/skills/snoop
pip install -r ~/.claude/skills/snoop/requirements.txt
```

## Use it in Claude Code

snoop is built to be driven by Claude, not typed at a terminal. Say what you want:

- `snoop Jane Doe at acme.com`
- `find the email for <LinkedIn profile URL>`
- `verify jane.doe@acme.com`

The skill takes it from there: it resolves who the person is, builds a
`--person-plan`, runs the sensors, hands Claude the bundle to reason over, grounds
the result, and returns the contact card. You read prose; Claude reads JSON.

Resolution is the engine. The more snoop knows going in — a personal domain, a
GitHub handle, a confirmed employer — the more it finds. A name plus the company's
email domain makes the sensors pattern-guess (the domain is what `pattern_gen`
needs); a found personal domain fires the personal-site `mailto:` sensor, often a
direct mailbox and a strong identity anchor. Claude does that resolution pass
first, then feeds it back in.

## What comes back

In Claude Code, Claude presents the result as prose. Underneath, `snoop --ground`
renders a plain grounded card — one line per fact, each line carrying its markers:

```text
Jane Doe — staff engineer at Acme; maintains acme-cli, writes at jane.dev.

Email:
  ✓ [+] janedoe@gmail.com — set public on her GitHub profile; Gmail, SMTP skipped by policy
  ✓ [+] jdoe@acme.com [google-confirmed] — the one acme.com pattern that resolves; name matches, the rest not_found

Social:
  ✓ [+] github.com/janedoe — validated; links back to jane.dev
  ~ [?] linkedin.com/in/janedoe — declared, not independently confirmed

Identity check:
  ✓ jane.dev ↔ github.com/janedoe (rel=me) — one person, no namesake
```

Three markers, three axes:

- The verdict tag — `[verified]` / `[google-confirmed]` / `[pattern-guess]` — is
  **deliverability**. No tag means snoop didn't probe it: a personal-provider
  address like the Gmail above is skipped by policy.
- **[+] / [?]** is **belonging** — is this fact attached to the target (bound), or
  only declared (unconfirmed)?
- **✓ / ~ / ·** is the analyst's **confidence** in the fact, auto-capped to `~`
  when identity is a genuine namesake toss-up.

Provenance — where a fact came from — isn't a glyph on the card; it's the citation
behind each fact, the thing `--ground` checks.

## Reference

Everything below is for driving the sensors by hand or understanding what they do.
In normal use, Claude handles it.

### Run the sensors directly

```bash
python3 snoop.py "Peter Steinberger" \
  --person-plan '{"name":"Peter Steinberger","handles":{"github":"steipete"},"personal_domains":["steipete.com"],"employer":{"name":"OpenAI","domains":["openai.com"]}}' \
  --allow-google-account \
  --out /tmp/snoop-obs.json
```

This writes the observation bundle to the file and prints the ready-to-run
`--ground` command, so the bundle never gets re-typed. Verify a single address
with `snoop.py --verify jane@acme.com` (or a bare email positional,
`snoop.py jane@acme.com`).

Useful flags (full list in `--help` or `SKILL.md`):

| Flag | Purpose |
|---|---|
| `--out PATH` | Write the bundle to a file and print the `--ground` command. |
| `--verify EMAIL` | Verify one address (repeatable); skip discovery. |
| `--person-plan JSON` | The resolved person the sensors run against — `{name, handles, personal_domains, employer{domains}}`. Inline JSON, `@file`, or a path; wins over the positional/`--domain`/`--github`. |
| `--domain DOMAIN` | Repeatable. Employer email domain(s) for `pattern_gen` and SMTP. Without it, most candidates collapse to low-confidence guesses. |
| `--github HANDLE` | Target's GitHub handle — enables `git_emails`, `gh_profile`, `gh_search`. A wrong handle is caught by binding and skipped. |
| `--ground` / `--observations-file PATH` | Read `{person, summary, facts}` on stdin, load observations from PATH, drop uncited facts, render the card. |
| `--known EMAIL=Full Name` | Repeatable. Same-company knowns for pattern inference. |
| `--no-smtp` | Skip SMTP verification. |
| `--no-pgp` | Skip the keys.openpgp.org owner-UID corroboration of discovered addresses. |
| `--no-ledger` | Don't append this run's yield metadata to `~/.snoop/ledger.jsonl`. |
| `--no-search` | Drop the host-supplied `work_search_results` from the bundle (the anchored sensor observations still emit). |
| `--deadline SEC` | Shared wall-clock budget for the whole run (default 60s); a sensor still running at the deadline is abandoned and reports `deadline-exceeded`. |
| `--allow-google-account` | Opt-in: Google People API existence check on Google-hosted domains, via logged-in Chrome cookies. Always safe to pass — a no-op when there are no Google candidates or no cookies. |
| `--google-workspace-domain DOMAIN` | Rarely needed — Google MX is auto-detected. Force a domain that isn't already a candidate. |
| `--diagnose` | Capability probe (gh auth, dnspython, Google readiness, ledger health) and exit. |

### The observation bundle

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

Claude reads fields off `data`, picks the email, and hands its facts — each citing
observation `id`s — to `--ground`. The verdict words map to the evidence:
**verified** (clean SMTP `RCPT` 250), **google-confirmed** (Google People API
confirms the account *and* the name matches, where SMTP couldn't), **pattern-guess**
(no positive existence signal). When nothing is usable, Claude emits no email fact
at all — a
**dead-end** — and suggests a channel from the hints instead. Dead-end is an
outcome, not a fourth verdict (`lib.schema.EMAIL_VERDICTS` holds three).

### How SMTP verification works

Per domain: **one** MX lookup, **one** catch-all sentinel probe (`RCPT` a random
non-existent localpart), and **one** reused SMTP connection for all candidates. A
catch-all result or an unreachable MX short-circuits the rest of that domain's
probes; a verified hit doesn't — there may be more candidates on the domain left
to check.

The pipeline skips personal-provider domains (Gmail, iCloud, Outlook, Proton) by
default — those either block `RCPT` or 451-throttle non-recognized senders, and
probing them tips spam filters. Google Workspace and Microsoft 365 commonly return
`inconclusive` on `RCPT` — reported honestly, never as "verified." For
Workspace-hosted domains, `--allow-google-account` adds the Google People API
existence check that discriminates where SMTP can't.

A per-domain daily probe budget (default 5/day, JSON state under
`~/.snoop/probe-budget.json`, `0600` perms) caps SMTP probes. The check uses
`RCPT` + catch-all detection, deliberately *not* the unreliable and widely
disabled SMTP `VRFY` command.

### Identity binding — why it won't email a namesake

Before any deliverability probe, snoop asks a prior question: does this address
actually *belong* to the target?

**A candidate binds only when ≥2 independent signals agree — and at least one ties
the address to *this* person.** The identity-bearing signals: an address observed
on a bound surface (a validated GitHub account, or a personal site on a
rel=me-verified domain), or rel=me domain ownership. The resolved employer domain
and a PGP owner-UID corroborate, but neither says *which* person — a domain
belongs to a company, a key only proves someone holds the inbox. So two
corroborating signals alone (employer + PGP) never bind. One coincidence is noise;
an identity-bearing signal plus a second is a person.

**SMTP fires only on bound candidates.** The `RCPT` probe opens a socket to the
mailbox, so snoop will not knock on a same-named stranger's door. When nothing
binds, it opens no socket and renders the honest blank — what it checked, what it
didn't, and why.

**The Google People API existence check is the exception, by design.** It's an
authed call through your own Chrome cookies — no socket — and on a catch-all
Workspace domain it's the only thing that can tell two addresses apart. So it
*also* runs on the unbound pattern guesses on a Google-hosted domain, collapsing a
column of plausible guesses to the one account that actually exists. A pure
name×domain guess never binds, so SMTP never touches it; but on a Workspace
tenant, whether it exists is still a fact snoop can check. The split is the point:
SMTP opens a socket and stays bind-gated, the existence check doesn't, so it
doesn't have to.

**When several addresses verify, snoop clusters them by Google account (Gaia)
id.** Same id means aliases of one person — collapse them, no namesake. Different
ids mean distinct accounts — a real collision to split. That answers *same
person?* even on a locked tenant that returns no display name. It never answers
*the right person?* on its own, and locked tenants hand back the id only
intermittently — so when it's absent, snoop falls to a rare-name prior or
abstains. It never guesses.

Then `--ground` checks the same card from the other side: each fact's citations
against the observations they name, dropping any that don't resolve. Two
independent verifiers, two axes — identity (does this address belong to the
person?) and provenance (is each fact attributable to a real observation?).

### Calibration — how the numbers are measured

snoop's hit rates are **measurements, not accuracy claims** — reported as "measured
on N targets of these classes, on DATE," never as a guarantee for the next lookup.
The harness (`python3 -m tests.calibration`) computes per-sensor hit rate and
precision-on-known over a labeled target set.

Privacy is built into the protocol. The committed fixture
(`tests/fixtures/calibration_targets.json`) holds only EXAMPLE/synthetic archetype
shapes with **null** ground truth — a CI test enforces that no real address ever
lands in the public file. The real N≥25 public-trail targets and their known
addresses live only in the gitignored
`tests/fixtures/calibration_targets.local.json`, so the published numbers
reproduce only against that local set. A local ground-truth entry is deleted on
the subject's request — remove their row.

> **Status:** the per-sensor table is produced by a human-supervised live run
> against the local N≥25 set. That run touches real people and the network, so it
> isn't part of the automated suite. Until it's recorded here, treat the sensors as
> un-benchmarked rather than assuming a number.

### What's in here

| File | Purpose |
|---|---|
| `SKILL.md` | The skill itself — the loop Claude Code follows. |
| `snoop.py` | Entry point. `--observations` (sensor, the default) and `--ground` (verifier) are the modes; `--verify EMAIL` checks one address. |
| `lib/reason.py` | `build_evidence()` — flattens the resolved person + probed candidates into the observation bundle. |
| `lib/ground.py` | The deterministic verifier — drops facts whose citations don't reference a real observation. |
| `lib/render.py` | Renders the grounded card for `--ground`. |
| `lib/pipeline/` | The pure decision brain, extracted from `snoop.py`: `binding` (does the person/address bind?), `candidates` (cluster + rank), `probes` (Phase-2 eligibility). See [ARCHITECTURE.md](ARCHITECTURE.md). |
| `lib/sensors.py` | The discovery-sensor registry — one `SensorSpec` table (gate, source type, skip reason) the fan-out and the skip-list both read. |
| `lib/` sensors | I/O sensors: `git_emails`, `gh_profile`, `personal_site`, `hn_profile`, `package_registry`, `pgp_keyserver`, `rel_me`, `google_account` (+ `chrome_cookies`), `verify_smtp`. `pattern_gen` is a *synthesizer* (name×domain templates, no I/O); `gh_search` is an *identity helper* run during `person_resolve`, not a fan-out sensor. |
| `lib/` shared | `fetch` (SSRF-guarded HTTP), `_gh_api` (the GitHub CLI/HTTP caller), `person_resolve` (identity resolution), `ledger` (local yield metadata), `normalize`, `diagnose`, `schema`. |
| `tests/` | pytest suite (`python3 -m pytest tests/`, no network); `_http_harness.py` is the shared fetch fake; `calibration.py` is the manual measurement harness. |
| `requirements.txt` | One dependency: `dnspython`. |

## The point

Finding an address was never the hard part of cold outreach. Being sure it's the
right one — and able to say why — is. snoop does the I/O, shows its sources, and
stops where they stop.

## License

MIT — see [LICENSE](LICENSE).
