# snoop

A [Claude Code](https://claude.com/claude-code) **skill** that finds and
verifies a person's email address.

Give it a name + company (or a LinkedIn URL, or freeform text). It resolves
the person across public sources — GitHub commits, GitHub profile + README,
personal-site `mailto:` anchors, and (as a last resort) name × domain
pattern guessing — scores each candidate on three independent fields, and
emits a contact decision card.

SMTP `RCPT` probing is one verification signal among many. **It never sends
mail.** Use it for legitimate outreach and verification only.

## What's in here

| File | Purpose |
|---|---|
| `SKILL.md` | The skill itself — instructions Claude Code loads. |
| `snoop.py` | Entry point. Pipeline: resolve → fan-out resolvers → cluster → score → verify → render. |
| `lib/` | Resolvers (`git_emails`, `gh_profile`, `personal_site`, `pattern_gen`, `google_account`), scorer, renderer, diagnose, normalize, schema. |
| `verify_email.py` | Legacy single-address verifier. Standalone CLI, no pipeline. |
| `tests/` | pytest suite (`python3 -m pytest tests/`, no network). |
| `requirements.txt` | One dependency: `dnspython`. |

## Install (as a Claude Code skill)

```bash
git clone https://github.com/saraswatayu/snoop.git ~/.claude/skills/snoop
pip install -r ~/.claude/skills/snoop/requirements.txt
```

Then in Claude Code just say things like:

- `snoop Jane Doe at acme.com`
- `find the email for <LinkedIn profile URL>`
- `verify jane.doe@acme.com`

The skill takes it from there: builds a `--person-plan`, calls `snoop.py`,
returns the decision card.

## Run the pipeline directly

```bash
python3 snoop.py "Peter Steinberger" \
  --person-plan '{"name":"Peter Steinberger","handles":{"github":"steipete"},"personal_domains":["steipete.com"],"employer":{"name":"OpenAI","domains":["openai.com"]}}' \
  --known "sam@openai.com=Sam Altman" \
  --intent work
```

Useful flags (full list in `--help` or `SKILL.md`):

| Flag | Purpose |
|---|---|
| `--intent work\|personal\|either` | Default `work`. Controls section order and the decision-line recommendation. |
| `--known EMAIL=Full Name` | Repeatable. Same-company knowns for pattern inference. |
| `--no-smtp` | Skip SMTP verification entirely. |
| `--allow-google-account` | Opt-in: use Google's People API to verify candidates on Google-hosted domains. Reads logged-in Chrome session cookies. |
| `--google-workspace-domain DOMAIN` | Repeatable. Adds DOMAIN to the Google-API probe set (for Workspace tenants on non-google.com domains). |
| `--json` | Emit machine-readable JSON instead of the markdown card. |
| `--diagnose` | Print a capability probe (gh auth, dnspython, google_account readiness, etc.) and exit. |

## Output: the contact decision card

`snoop.py` emits a markdown card with:

1. **Header** — name, employer, identity ambiguity state, count of bound anchors.
2. **Resolver notes** — plan-vs-observed deltas (e.g. "plan claimed employer=OpenAI; github profile company=Anthropic").
3. **Decision** — opinionated top recommendation with a reason-to-trust line and ⚠ caveats (SMTP inconclusive, catch-all, former employer, etc.).
4. **Work / Personal / Other tables** — ranked candidates with three score columns.
5. **Channel hints** — when the plan included a backup channel (X DMs open, LinkedIn URL, etc.).

The three score columns:

| Field | Question | Abstention (`—`) |
|---|---|---|
| **Belongs** | Is this actually this person's address? | No sources observed |
| **Work** | Is this their current work email? | No employer info / unrelated domain |
| **Deliverable** | Will mail reach a human? | SMTP inconclusive AND no Google account verdict |

A `—` means **abstain**, not zero. The card never labels a candidate
"Verified" unless SMTP returned a clean 250 on a non-catch-all domain.

## How SMTP verification works

Per domain: **one** MX lookup, **one** catch-all sentinel probe (RCPT a
random non-existent localpart), and **one** reused SMTP connection for all
candidates. Stops early on the first verified hit.

The pipeline skips personal-provider domains (Gmail, iCloud, Outlook,
Proton, etc.) by default — those either block `RCPT` or 451-throttle
non-recognized senders, and probing them tips spam filters.

Google Workspace / Microsoft 365 commonly return `inconclusive` on `RCPT`
— deliberately reported honestly, never as "Verified." For
Workspace-hosted domains, `--allow-google-account` adds a Google People
API existence check that can discriminate where SMTP can't.

A per-domain daily probe budget (default 5/day, JSON state under
`~/.snoop/probe-budget.json`, 0600 perms) caps SMTP probes to avoid
spamming MX servers.

This uses SMTP `RCPT` + catch-all detection, deliberately *not* the
unreliable and widely disabled SMTP `VRFY` command.

## Legacy verifier (`verify_email.py`)

A standalone single-address verifier predates the pipeline. Still useful
for "just verify this one address" requests.

```bash
# Single address
python3 verify_email.py "jane.doe@acme.com"

# Batched, ranked list (stops at the first verified hit)
python3 verify_email.py a@acme.com b@acme.com c@acme.dev
python3 verify_email.py --file candidates.txt        # one per line
printf 'a@acme.com\nb@acme.com\n' | python3 verify_email.py --file -

# Infer the company format from a known address
python3 verify_email.py jsmith@acme.com jane.smith@acme.com \
    --for "Jane Smith" --known "bdoe@acme.com=Bob Doe"
```

Single mode exits with a verdict-mirroring code (`0` verified, `1`
invalid, `2` catch_all, `3` inconclusive, `4` bad_syntax, `5` no_mx).
Batch mode exits `0` only if a verified hit was found.

Each verdict carries a hand-weighted heuristic `score` (0–1) and an
`evidence` string. Treat `0.45` as "more than a coin-flip, far from
certain" — it's a defensible confidence number, not a calibrated
probability.

Sequential by design — one target per invocation. Not built for bulk.

## License

MIT — see [LICENSE](LICENSE).
