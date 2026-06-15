# snoop

[![skills.sh](https://skills.sh/b/saraswatayu/snoop)](https://skills.sh/saraswatayu/snoop)

**Find the person. Prove the email.**

You found them on LinkedIn. Now you need the email — the real one, that reaches
*them* and not a namesake, and won't bounce on the cold message you only get to
send once.

Other finders hand you that address with a number attached — *confidence: 85%* —
and ask you to trust it. snoop hands you the address *and the case for it*: who
they are, where each line came from, and a second pass that deletes any claim it
can't cite. It reads only what people published under their own name, and stops
where the trail stops.

It's named for prying; it does the opposite. **No source, no sentence** — snoop is
the sensor, Claude is the analyst, and neither one gets to guess.

snoop is a [Claude Code](https://claude.com/claude-code) skill. It does the I/O
Claude can't — git commits, the GitHub API, a personal site's `mailto:`, the SMTP
`RCPT` handshake, MX lookups, the Google People API — and hands Claude typed
evidence to reason over.

```
plan → snoop --observations → (Claude reasons) → snoop --ground → present
```

## Why you can trust the answer

That confidence score fuses three different questions into one digit:

- **Deliverability** — does the mailbox exist and accept mail?
- **Identity** — is it the right person, or a same-named stranger?
- **Provenance** — where did we learn this, and can we point at the source?

snoop keeps them apart, because they fail apart.

**Deliverability** is the verdict word on the email, and it means exactly one
thing:

- `verified` — a clean SMTP `RCPT` 250 from the mailbox.
- `google-confirmed` — the Google People API confirms the account exists *and* the
  name matches, where SMTP came back inconclusive.
- `pattern-guess` — a name×domain guess with no positive existence signal.

No blend, no percentage. When nothing is usable, snoop emits no email at all — an
honest blank that tells you what it checked, what it didn't, and why.

**Identity** is answered before any probe fires. A candidate binds only when ≥2
independent signals agree, at least one tying the address to *this* person. SMTP
fires only on bound candidates, so snoop won't open a socket to a same-named
stranger's mailbox. (The binding rule and the namesake-clustering live in
[ARCHITECTURE.md](ARCHITECTURE.md).)

**Provenance** is the third axis. Every fact carries citations, and a second,
deterministic pass — `snoop --ground` — drops any fact whose citations don't
resolve to a real observation. Two independent verifiers, two axes: identity
decides whether an address belongs to the person; grounding decides whether each
fact is attributable at all.

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

Skip it and snoop still runs — it degrades honestly: the SMTP sensor reports
`skipped: dependency dnspython missing` instead of a silent blank, and everything
that doesn't need MX (GitHub, personal-site, pattern, PGP, rel=me) works either
way.

snoop keeps a small local **ledger** at `~/.snoop/ledger.jsonl` (on by default):
one line per run, yield metadata only — which sensors ran, how long they took, the
plan *shape* (booleans), the MX class. Never names, addresses, handles, or domains;
a CI test enforces that schema. Opt out per run with `--no-ledger`.

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
email domain only lets the sensors pattern-guess; a found personal domain fires the
personal-site `mailto:` sensor, often a direct mailbox and a strong identity
anchor. Claude does that resolution pass first, then feeds it back in.

## What comes back

In Claude Code, Claude presents the result as prose. Underneath, `snoop --ground`
renders a plain grounded card — one line per fact, each carrying its markers:

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

Three markers, three axes: the verdict tag (`[verified]` / `[google-confirmed]` /
`[pattern-guess]`) is **deliverability**; **[+] / [?]** is **belonging** (is this
fact tied to the target, or only declared?); **✓ / ~ / ·** is the analyst's
**confidence**, auto-capped to `~` when identity is a genuine namesake toss-up.

## Under the hood

- **Driving the sensors by hand** — the full flag list, the `--person-plan` shape,
  and the observation-bundle schema: see [SKILL.md](SKILL.md) (the loop Claude
  follows) and `python3 snoop.py --help`.
- **How the Python fits together** — the four layers, the two-verifier design, the
  file-by-file map, and how SMTP verification works: see
  [ARCHITECTURE.md](ARCHITECTURE.md).
- **The numbers** — snoop's hit rates are *measurements, not accuracy claims*,
  reported as "measured on N targets of these classes, on DATE," never a guarantee
  for the next lookup. The published numbers reproduce only against the gitignored
  `tests/fixtures/calibration_targets.local.json`; a local ground-truth entry is
  deleted on the subject's request. The per-sensor table isn't published yet —
  until it is, treat the sensors as un-benchmarked.

## The point

Finding an address was never the hard part of cold outreach. Being sure it's the
right one — and able to say why — is. snoop does the I/O, shows its sources, and
stops where they stop.

## License

MIT — see [LICENSE](./LICENSE).
