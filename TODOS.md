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

## Held back (lower priority, available next)

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
