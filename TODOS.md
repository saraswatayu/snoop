# TODOS

## Done on `refactor/prime-time` (the stranger-proof sensor organ)

The full in-scope plan landed: E1 ledger, E2 rel=me/Bluesky, E3 PGP, E4 worked
archetypes, E6 deadline, the ENG-8/9/10 identity engine (probe-gate + blind
multi-angle + refutation + tiered workflows), ENG-1–7 amendments, the per-sensor
evidence-tier table (T-minor C), the press-confirmed-role tier (`~` with a cited
basis, never `✓`), the 4A honest-blank card, T-minor B M365 render separation, the
ENG-7 acceptance test + shared fetch harness, the distribution/README story, and
the meeting-prep render mode. The only Assignment step left is human-gated (below).

## Human-gated (not codeable autonomously)

### Run the supervised calibration N≥25 and publish the numbers

**What:** Author ≥25 real public-trail targets + their known addresses in the
gitignored `tests/fixtures/calibration_targets.local.json`, run
`python3 -m tests.calibration` (baseline + final), and paste the measured
per-sensor hit-rate table into the README's calibration section.

**Why:** Premise 6 — credibility requires measurement. The harness, the privacy
CI gate, and the README methodology framing are all in place and tested; only the
run itself remains, and it touches real people + the live network, so it can't be
part of the automated suite or fabricated.

**Effort:** M (human) — data gathering + one supervised run.
**Blocks:** marketplace packaging (below).

### Register the eval-fixture GitHub accounts

**What:** Create the 5–10 roster accounts (`snoop-fixture-*`) on GitHub and record
them as owned in `tests/evals/fixtures/personas.md`.

**Why:** Eng review 2026-06-12, issue 4: GitHub usernames can't be RFC-reserved,
so an unregistered "fictional" handle in a committed eval fixture can collide
with (or later be claimed by) a real person — a real-identity association in a
public privacy-focused repo. Registering closes the hole permanently and gives
the privacy gate a closed allowlist.

**Effort:** S (human, ~15min) — manual signups, not codeable autonomously.
**Blocks:** the `happy-dev` P0 seed fixture of the analyst-evals workstream (below).

## Held back (lower priority, available next)

### Analyst-layer evals — corpus, graders, harness

**What:** Eval system for the Step-3 reasoning layer (the host model reading
SKILL.md): ~24–28 synthetic fixture bundles (incl. injection/forgery cases),
deterministic G1/G2 graders with SKILL.md quote-anchored drift lint, privacy
gate with negative self-test, concurrent k-trial runner with provenance stamps
and a two-axis merge gate (hard misattribution axes strict; soft axes one retry).

**Why:** SKILL.md Step 3 is the product's most important module with zero
coverage — every prose edit is an untested production change. Sensors have 9k
lines of tests; the analyst has none.

**Context:** Full reviewed plan in `docs/plans/2026-06-12-analyst-evals.md`
(local-only — this entry is the committed pointer); task breakdown in
`~/.gstack/projects/saraswatayu-snoop/tasks-eng-review-20260612-111424.jsonl`
(T1–T7). Phases: P0 fixtures+graders (no LLM cost) → P1 runner+corpus →
P2 judge (only if G1/G2 prove insufficient). Eng review 2026-06-12: 9 findings
+ 4 Codex tensions, all folded. The ledger-failure-distillation loop shares the
4–6-week ledger-data dependency with Ledger phase 2 (below).

**Effort:** M–L (human ~1wk) / M (CC ~1–2d). **Priority:** P1.
**Depends on:** the human-gated fixture-account registration (above) for the
`happy-dev` fixture; nothing else.

### Promote the verdict-word check into `--ground` (runtime)

**What:** Once `verdict` is a first-class fact field, `--ground` checks each
fact's verdict against its cited observations' `data` fields
(`smtp`/`account_exists`/catch-all) and downgrades + annotates mismatches —
never upgrades. Separate small PR: `lib/ground.py`, `snoop.py`, SKILL.md docs.

**Why:** Eng review 2026-06-12, tension T4 (the eval design exposed a missing
production check): verdict↔evidence consistency is a mechanical lookup, the
same class as the citation byte-check — production users shouldn't rely on
prose compliance for the highest-stakes word (`verified`).

**Context:** Spec is §9 of the analyst-evals plan; boundary discipline there
defines what must NOT move into code (abstention, refutation, recall, markers
stay prose+eval). Evals keep grading pre-ground output so raw analyst behavior
stays measured.

**Effort:** S (human ~0.5d) / S (CC ~30min). **Priority:** P2.
**Depends on:** the `verdict` field name agreed with the eval graders
(analyst-evals plan §4).

### GitLab sensor — profile + commit emails

**What:** GitLab analog of `gh_profile`/`git_emails` via the public GitLab API.

**Context:** The profile surface (`GET /api/v4/users?username=…` → `public_email`,
name, `website_url`) is a clean high-precision yield and mirrors `gh_profile`
directly (module + `_default_*_caller` seam + network-free tests via the shared
`tests/_http_harness.py`; add `gitlab_profile`/`gitlab_commit` to the `SourceType`
Literal; integrate into `run_pipeline` gated on a `gitlab` handle; anchor-binding
rules unchanged). Note: commit-author emails are NOT reliably exposed by the
public GitLab API without auth (unlike GitHub's `/users/{h}/events`), so the
commit-mining half is a project-walk follow-up, not a freebie — scope v1 to the
profile surface and say so.

**Effort:** M (human) / M (CC). **Priority:** P3. **Depends on:** None.

### Personal-site channel-link extraction

**What:** Emit observations for self-published channel links (Calendly, LinkedIn,
X, Bluesky) found on pages `personal_site` already fetches.

**Why:** Reachability-map data with no new fetch surface — the page is already in
hand; today only `mailto:` is harvested.

**Context:** Needs `fetch_personal_site` to return channel links alongside email
candidates and `snoop.py` to emit them as `channel_hint` observations (a little
more than "pure extraction" — it touches the sensor's return shape + the bundle
builder). Tier mapping per the evidence-tier table (a bare link is `[?]` until
confirmed). Held back at the 2026-06-11 expansion ceremony.

**Effort:** S (human) / S–M (CC). **Priority:** P3. **Depends on:** None.

### Marketplace / plugin packaging

**What:** Package snoop for a Claude Code plugin marketplace listing.

**Why:** Distribution beyond clone-the-repo.

**Context:** Ship the measured story, not the vibes story — revisit after the
supervised calibration run gives the README its credibility line.

**Effort:** M (human) / S (CC). **Priority:** P3.
**Depends on:** the human-gated calibration run above.

### Ledger phase 2 — auto-applied probe ordering + retire interim heuristic

**What:** Implement auto-applied probe ordering from ledger evidence, and delete
the interim ordering heuristic (the within-bound-set `_probe_rank` tiebreak).

**Why:** The deliberately-deferred half of the E1 ledger decision (CEO review
2026-06-11, T1.2): ordering machinery ships only when the data proves ordering
matters. The interim heuristic is planned scaffolding that must die on schedule.

**Context:** The ledger (`~/.snoop/ledger.jsonl`) records RunRecord fields +
`plan_shape` booleans + `mx_class` from day one, so evidence accumulates with
normal use. Trigger: offline analysis shows a sensor class consistently winning
for an archetype (e.g. HN-first for `{hn:1}` plans).

**Effort:** M (human) / S (CC). **Priority:** P3.
**Depends on:** ≥4–6 weeks of ledger data + the N≥25 calibration baseline.
