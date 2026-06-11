---
name: snoop
description: Use when the user wants to find, guess, or verify someone's email address, or build a contact profile for outreach. Triggers include "snoop", "snoop NAME at COMPANY", "find this person's email", "what's so-and-so's email at company X", a pasted LinkedIn profile URL with "get their email", "figure out the email for X", or "verify jane@acme.com". snoop is the sensor — it does the I/O you can't (git commits, the GitHub API, personal-site mailto: anchors, the SMTP RCPT handshake, the Google People API, MX lookups) and emits a typed observation bundle. You are the analyst: you reason over the bundle and a tiny deterministic check (`--ground`) verifies your citations.
---

# snoop

## What this does

Build a **contact profile for outreach**: who the person is, the best email to
reach them, and the context for a good first message. The reachable email leads;
profile context follows.

**snoop is a sensor; you are the analyst.** snoop's irreducible job is the I/O
you cannot do yourself — git-commit emails, the GitHub REST API, personal-site
HTML, the SMTP `RCPT` handshake, the Chrome-cookie-authed Google People API, MX
lookups. It performs those and emits a **typed observation bundle**: raw
readings, each with a `source_url`, a structured `data` field, and any probe
verdict. **You** — already running, able to run WebSearch — reason over that
bundle: pick the email, judge the namesake, write the prose, mark the
provenance. `--ground` then checks that every claim cites a real observation.

Why the split: the I/O genuinely needs code (you can't open a socket and speak
SMTP). The reasoning is yours because you are best at it, you are already here,
and you handle the long tail a rule table can't (name variants, company
rebrands, intent ranking).

## The loop

```
1. You resolve the target  → a --person-plan JSON (+ optional WebSearch)
2. snoop --observations     → the sensor bundle (the I/O you can't do)
3. You reason over the bundle → facts (each citing observation ids) + prose
4. snoop --ground           → drops uncited facts, renders the grounded card
5. You present the card
```

`snoop.py` is in this skill's own directory — resolve that directory at runtime;
don't hardcode an absolute path.

## Step 1 — Resolve the person, then build the `--person-plan` (you)

**This is the highest-leverage step — don't skimp.** snoop has no bundled search;
the sensors only find what you feed them. Before sensing, do a focused
**resolution pass** with WebSearch (and WebFetch on the hits) to learn who this
person is and what they have online, then pour it into the plan:

- their **personal site / domain** → `personal_domains` (fires the `personal_site`
  `mailto:` sensor — often their address directly — and is a strong identity
  anchor). This is the single highest-yield thing to find.
- their **primary socials** (X, LinkedIn, GitHub, HN) → `handles` / `channel_hints`
  (WebFetch the public profile and match name + employer → pass it `confirmed`).
- their **current employer + domain**, with the URL you confirmed it from →
  `employer` + `employer.source_url`.
- a few **body-of-work** items (talks, articles, papers) → `work_search_results`.

A name + company alone (no site, no handle) leaves the sensors with only
pattern-guessing — so spend the searches here. You know context the sensors
don't: nicknames, employer chronology, handles. Pass it all structured. Minimum
useful plan:

```json
{
  "name": "Peter Steinberger",
  "handles": {"github": "steipete"},
  "personal_domains": ["steipete.com"],
  "employer": {"name": "OpenAI", "domains": ["openai.com"]}
}
```

Optional fields:

- `employer.source_url` / `former_employers[].source_url`: where you **confirmed
  the employer during resolution** (the news article, their profile, the company
  page). Set it whenever you learned the employer from WebSearch rather than the
  user — the role/employer facts then cite that corroboration instead of just
  your own declaration. You almost always resolve the employer by searching, so
  almost always set this.
- `former_employers`: `[{"name": "PSPDFKit", "domains": ["pspdfkit.com"], "until": "2023", "source_url": "https://..."}]`
- `handles.hn`: a Hacker News username → snoop reads the public email off their
  HN profile (high-yield on YC/founder targets). It's an untrusted hint, so
  facts from it are `[?]` at most.
- `packages`: `[{"registry": "npm"|"pypi", "name": "<package>"}]` — packages the
  person published. snoop pulls the publisher/author email from the registry
  (near-100% precision when present). Supply these when you know them.
- `channel_hints`: `{"x_dms_open": true, "linkedin": "<url>", "prefers": "x"}` —
  populate whenever you learned a backup channel while resolving the person. A
  bare URL value is a declaration. snoop has no LinkedIn/X sensor (deep profiles
  are auth-walled and scraping them is ToS-laden), but **you can WebFetch a
  public profile preview during resolution** and match it to the target (name +
  current employer). When you do, pass the confirmation so the channel fact can
  cite it instead of grounding as a bare declaration:
  `{"linkedin": {"url": "<url>", "confirmed_via": "public profile: name + employer match"}}`.
  A confirmed self-published profile is a strong cross-link — mark it `[+]`.
- `name_variants`: explicit overrides for non-Latin spellings normalization misses.
- `work_search_results`: the body-of-work feed. snoop has no bundled search
  provider — **you are the provider.** Run your built-in WebSearch (a query or
  two — published work is one of the blind resolution angles) and pass the hits:
  ```json
  "work_search_results": [
    {"title": "...", "url": "https://...", "item_type": "talk|article|podcast|paper|other",
     "published_at": "2026-04-10", "summary": "...",
     "crosslink_url": "https://<their-bound-domain-or-profile>/..."}
  ]
  ```
  Set `crosslink_url` only when you verified the page ties to THIS person; a
  result you can't tie to a bound signal is a namesake risk — mark any fact from
  it `[?]` at most.

**Any field can be `null`.** snoop re-derives independently and surfaces
conflicts as `resolver_note` observations. Don't fabricate.

**Anchor binding rule (defense against hallucinated handles):** a `github`
handle in the plan is an **untrusted hint** until ≥2 of {name match, employer
match, personal_domain cross-link} agree. If you're unsure the handle is right,
leave it out — the other sensors still run without it.

**Resolve from independent angles — keep them blind.** A stranger with a common
name is snoop's worst failure, and it's an *identity*-axis failure that `--ground`
(a provenance check) can't catch. So resolve the person from **independent
angles** — name+employer, personal domain, handle, published work — and don't let
one angle's guess seed another's search. Angles that never saw each other can't
cross-contaminate, so when they **agree** on the same domain/handle/address that
agreement is real corroboration, not an echo; when they disagree you have a
namesake to split, not a fact. On an easy target you do this in your head; on a
hard one it becomes the mechanical Tier-2 workflow (see **Tiered resolution**).

**Resolution is a bounded loop, not a single capped pass.** Resolve → sense → if
the bundle carries `resolution_gaps`, resolve the named gap and re-run. You're
done when the gaps go quiet, OR after **2 re-resolve rounds**, OR a round adds no
new observations — whichever comes first. Zero-friction means zero *human*
friction and maximal *host* resolution (finding a real personal domain routinely
turns ~22 observations into ~32). Don't ration yourself to one search — spend
what the gaps ask for, stop at the loop bound.

## Step 2 — Sense: get the observation bundle

```bash
python3 "<this-skill-dir>/snoop.py" "Peter Steinberger" \
  --person-plan '{"name":"Peter Steinberger","handles":{"github":"steipete"},"personal_domains":["steipete.com"],"employer":{"name":"OpenAI","domains":["openai.com"]}}' \
  --allow-google-account \
  --out /tmp/snoop-obs.json
```

This runs the full sensor pipeline (resolve → git/GitHub/personal-site/pattern
fan-out → Google/SMTP probes) and writes the bundle to the file. `--out` prints
the ready-to-run `--ground` command. (Omit `--out` to get the bundle on stdout.)

**Check `resolution_gaps` first — it drives the loop.** If the bundle carries a
`resolution_gaps` array, your Step-1 resolution was thin — snoop is telling you
what a richer pass would add (a personal domain you didn't find, missing handles,
an uncited employer). Resolve the named gap, fold it into the plan, and re-run
`--observations`. Repeat until the gaps go quiet, OR you've done 2 re-resolve
rounds, OR a round adds no new observations — whichever comes first (pathological
targets terminate in ≤3 passes). A second pass with a real personal domain
routinely beats a first pass without one.

Each observation has a stable `id` you cite, a `content` line, and — for
`email_candidate` — a structured `data` field:

```json
{"id": "o7", "type": "email_candidate",
 "content": "candidate email: pete@openai.com (smtp=verified, account_exists=verified, sources=git_commit,gh_profile, google_display_name=\"Peter Steinberger\", name_match=yes)",
 "data": {"address": "pete@openai.com", "smtp": "verified", "account_exists": "verified",
          "sources": [{"type": "git_commit", "url": "...", "detail": "..."},
                      {"type": "gh_profile", "url": "...", "detail": "..."}],
          "google_display_name": "Peter Steinberger", "name_match": true}}
```

Read fields off `data`; don't re-parse the sentence. `name_match` is the **text**
disambiguator on a common-name Workspace tenant — bind the `name_match=true`
candidate, drop `name_match=false` ones. A `google_photo` is a **human-review
artifact** only — surface it as a link a person eyeballs; never compute a face
match.

**Verify one address.** For "verify jane@acme.com", skip discovery:

```bash
python3 "<this-skill-dir>/snoop.py" --verify jane@acme.com --allow-google-account
```

This runs MX/SMTP/Google on that address only and emits its bundle. A bare email
positional (`snoop.py jane@acme.com`) does the same.

**Useful flags** (full list in `--help`):

| Flag | Purpose |
|---|---|
| `--out PATH` | Write the bundle to a file; print the `--ground` command. |
| `--verify EMAIL` | Verify one address (repeatable); skip discovery. |
| `--no-smtp` | Skip SMTP probing. `email_candidate` shows `smtp=unprobed`. |
| `--no-pgp` | Skip the keys.openpgp.org corroboration of discovered addresses (a hit there is an owner-verified address — a `pgp` source, strong `[+]`). |
| `--deadline SEC` | Shared wall-clock budget for the sensor fan-out (default 60s). A sensor still running at the deadline is abandoned and reports `deadline-exceeded`. |
| `--allow-google-account` | Opt-in: Google People API existence check on Google-hosted domains, via your logged-in Chrome cookies. Always safe to pass — a no-op when no cookies or no Google candidates. |
| `--google-workspace-domain DOMAIN` | Rarely needed — Google MX is auto-detected. Force a domain that isn't already a candidate. |
| `--known EMAIL=Full Name` | Repeatable. Same-company knowns for pattern inference. |
| `--no-search` | Drop the host-supplied `work_search_results` from the bundle. |
| `--diagnose` | Capability probe (gh auth, dnspython, Google readiness) and exit. |

## Step 3 — Reason (you)

Reason over the bundle and produce the profile. **Lead with the email answer**,
then profile context. Each fact has: `kind` (email | channel | social_link |
work_item | role | consistency_note), the value to show, a confidence, the
observation `id`s that support it, and a one-line reason.

1. **Cite or omit.** Every fact must trace to an observation `id`. If you can't
   tie a claim to one, it doesn't go in the output. Never invent ids or sources.
2. **Two separate axes — don't fuse them.** The *verdict word* (`verified` /
   `google-confirmed` / `pattern-guess`) describes **deliverability/existence**
   and is always truthful — a `data.smtp == "verified"` address is `verified`
   even if you're unsure whose it is. The *provenance marker* (`[+]`/`[?]`)
   describes **belongs-to-this-person**: `[+]` = bound-by-construction (validated
   GitHub surface, `manual_known`, a Google `name_match=yes`, or a source on a
   cross-link-bound personal domain); `[?]` = weaker binding. A domain merely
   declared in the plan is `[?]`, never `[+]`; a `web_search` observation is
   `[?]` at most.
   - `person.ambiguity == "multiple_plausible_matches"` (a real namesake — more
     than one person could fit): cap **every** marker to `[?]` and open with the
     loud "confirm WHO before relying" banner.
   - `person.ambiguity == "insufficient_identity_evidence"` (just no anchor
     bound yet — the common case for non-developers with no public GitHub): do
     **not** blanket-cap. State each fact's verdict word plainly (a verified
     email is `verified`) and mark belongs-to-person honestly — a Google/SMTP-
     verified address that is the unique match for the target on their employer
     domain is strong even without a display-name confirmation. Add the scoped
     caveat ("identity not independently anchored — confirm the person if it
     matters"), don't bury the result.
3. **Verdict vocabulary is precise — use the exact word the evidence supports:**
   - `verified` — ONLY when `data.smtp == "verified"` on a non-catch-all domain.
   - `google-confirmed` — `data.account_exists == "verified"` but SMTP was
     catch-all/inconclusive/unprobed. NOT the same as `verified`.
   - `pattern-guess` — no positive existence signal, just a name×domain template.
   - `dead-end` — no usable candidate; suggest a channel from the hints.
4. **Namesake safety — abstain over guess.** If the observations could describe
   more than one person, say so, downgrade every marker to `[?]`, and emit
   fewer facts. A missed fact is cheap; attributing a stranger's email is the
   failure to avoid.
5. **Refute before you bind (the identity-axis verifier).** Before a binding
   holds, argue the counter-case out loud: *"suppose this is a different person
   with the same name — what would I expect to see if so, and is it present?"*
   Then weigh what you turn up by whether it is **grounded**:
   - a **grounded** refutation — one that cites real counter-evidence in the
     bundle (a second GitHub account under the same name, a Google display-name
     delta, a WHOIS registrant that isn't the target) — splits the binding to
     ambiguity: downgrade to `~`/`[?]` or raise the namesake banner.
   - an **ungrounded** refutation — "what if it's just coincidence?" with nothing
     in the bundle behind it — is the skeptic's prior, not evidence. Discard it;
     it must not water down a well-cited fact.

   Weak/no grounded refutation → the binding holds; one grounded refutation →
   downgrade or split. This is the **identity** axis. `--ground` is the
   **provenance** axis (do the citations exist?). **Two verifiers, two axes, both
   kept** — refutation never replaces `--ground`; their failure modes stay
   independent (one is your reasoning, one is a deterministic byte check), which
   is exactly why running both catches what either alone would miss.
6. **Keep facts scannable.** The card renders `✓ / ~ / ·` + the fact, one line
   each — don't write a sentence per line. Put the **grounded anchor** in
   `value` (the thing that appears in the cited observation — an address, a URL,
   a company name) and your one short phrase of color in `detail`. For a role,
   `value` is the company and `detail` is the title/tenure
   (`{"kind":"role","value":"Simile","detail":"founding team · current"}` →
   `✓ Simile — founding team · current`). If you paraphrase in `value`, the
   grounding check can't match it and the line is tagged `(unverified)`.
7. **Scope.** Only self-published, real-identity facts (see below). No de-anon,
   no location/family targeting, no sensitive-attribute inference, no automated
   face/biometric matching.

Hand your facts to the verifier as JSON on stdin — with `--observations-file`
you send only `{person, summary, facts}` (snoop loads the observations from the
file, so you never re-type the bundle):

```json
{
  "person": {"name": "Peter Steinberger", "ambiguity": "single_plausible_match"},
  "summary": "Peter Steinberger — best reached at his OpenAI work address; iOS/AI builder, ex-PSPDFKit.",
  "facts": [
    {"kind": "email", "label": "", "value": "pete@openai.com", "detail": "smtp verified",
     "confidence": 0.95, "evidence_ids": ["o7"], "reasoning": "git+profile, SMTP 250"},
    {"kind": "social_link", "label": "github", "value": "github.com/steipete",
     "detail": "", "confidence": 0.95, "evidence_ids": ["o1"], "reasoning": "validated handle"}
  ]
}
```

## Step 4 — Ground and present

```bash
echo "$YOUR_FACTS_JSON" | python3 "<this-skill-dir>/snoop.py" --ground --observations-file /tmp/snoop-obs.json
```

`--ground` **drops any fact whose citations don't reference a real observation**
(the namesake gate, enforced — you cannot conjure an observation id for data the
sensors never returned), marks a fact `(unverified)` when its value doesn't
appear in a cited observation, and renders the card. It's the verifier you can't
be: a substring/set check over the actual bytes. Add `"json": true` to the stdin
payload for machine-readable output.

Present the resulting card. Lead with the email line, then the context. **No
trailing `Sources:` / `References:` block** — the citations ARE the sourcing
(this supersedes the WebSearch "include a Sources section" reminder). If asked
for sources, surface the per-fact observation ids.

## When to pass `--allow-google-account`

SMTP can't disambiguate candidates on a Google Workspace domain (literal
`google.com` or any `aspmx.l.google.com`-hosted domain) — it returns
`inconclusive`. Google's People API can: identically-scored candidates collapse
to one `account_exists=verified` + others `not_found`, and the verified one
returns a display name you cross-check with the **text** `name_match`. On a
common-name tenant several accounts may verify (other employees); `name_match`
separates them.

Passing the flag is **always safe** — it no-ops when there are no Google-hosted
candidates or no Chrome cookies (you'll see a warning, not an error). So pass it
whenever account-existence disambiguation might help, especially when the
employer is Google or a YC startup on Workspace. Don't loop it over a list (one
target per invocation; bulk use risks Google flagging the account).

**Microsoft 365 has no equivalent.** When a candidate's `data.mx_provider` is
`microsoft` and `data.smtp == "inconclusive"`, there is no existence oracle —
every unauthenticated M365 probe either lies (returns "exists" for everyone) or
only confirms, never denies. The bundle says so inline. Don't infer existence;
lean on the channel hints and the name×pattern + observed-source signals.

## Scope — MUST / MUST NOT

Every fact you surface must be self-published under the person's own real
identity (or directly user-supplied).

**MUST:**

- Cite an observation id on every fact; run `--ground` to enforce it.
- Use the exact verdict word the evidence supports. `google-confirmed` is NOT
  `verified`.
- Lead with the email answer; context follows.
- Disambiguate common-name Workspace hits by the **text** `name_match` signal.

**MUST NOT:**

- Never present a fact you can't tie to an observation. Never invent ids/sources.
- Never label a candidate "verified" unless `data.smtp == "verified"`.
- Never de-anonymize a pseudonymous account; never target home address / live
  location / family; never infer sensitive attributes (health, sexuality,
  politics, religion).
- Never compute a face/biometric match or assert identity from a photo. A
  `google_photo` is a link for a *human* to eyeball — present it, never score it.
- Never loop over a list of targets. One person per invocation. Refuse
  `snoop "list.txt"` patterns.
- Never append a trailing `Sources:` / `References:` block.

## Token discipline

| Rule | Limit |
|---|---|
| Sensor fan-out | one batched call — never loop per resolver |
| Resolution pass (you, before sensing) | a focused **~2–5 searches + WebFetches** — rich resolution is the highest-leverage step (a personal domain → a direct mailbox). Don't skimp to hit a number; don't spelunk past diminishing returns. |
| Sensor re-runs | re-run `--observations` while `resolution_gaps` keep naming something you can resolve — bounded at 2 re-resolve rounds (or a round that adds nothing new). A second pass with a real domain beats a first pass without one; don't stop short of a gap you can fill, don't loop past the bound |
| Pre-validate a handle yourself | only when the user EXPLICITLY asks; otherwise trust the anchor binding to flag bad handles |

## Notes

- This skill is typically dispatched as a **subagent** — keep it that way; it
  isolates the work from the main session's context. The subagent is still a
  Claude model, so it is the reasoner for Steps 1, 3, and 5.
- SMTP probing is RCPT-only; snoop never sends mail. It skips personal-provider
  domains (Gmail, iCloud) by default — they block RCPT and probing tips spam
  filters. A per-domain daily budget (default 5/day, state under `~/.snoop/`,
  0600) caps probes.
- If a P1 sensor (git_emails or gh_profile) shows `unavailable`, run
  `--diagnose` once. Common fixes: `gh auth login`; `pip install --user
  dnspython`; sign into Google in Chrome; check `~/.snoop` is writable.
