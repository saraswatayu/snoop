# TODOS

## Render & Trust

### Decide + document the press-confirmed-role tier (✓ vs ~)

**What:** Write the rule for whether a press-confirmed (not self-published) role renders ✓ or ~, as part of the per-sensor evidence-tier table; pin with 2 tests.

**Why:** The one taste call left open after the prime-time dogfood ("I leaned ✓ with a cited basis; reasonable people could differ"). Unwritten taste calls get re-litigated on every render change.

**Context:** Three-axes framing: press confirmation is provenance strength, not identity-binding. Natural home = the evidence-tier table being written for the rel=me/PGP sensors (eng review T-minor C / ENG-6, docs/designs/stranger-proof-sensor-organ.md).

**Effort:** S (human) / S (CC)
**Priority:** P2
**Depends on:** the per-sensor evidence-tier table (same branch)

## Sensors & Learning

### Ledger phase 2 — auto-applied probe ordering + retire interim heuristic

**What:** Implement auto-applied probe ordering from ledger evidence, and delete the interim ordering heuristic (non-pattern source > pattern-only, then source count).

**Why:** The deliberately-deferred half of the E1 ledger decision (CEO review 2026-06-11, T1.2): ordering machinery ships only when the data proves ordering matters. The interim heuristic is planned scaffolding that must die on schedule, not fossilize.

**Context:** The ledger (`~/.snoop/ledger.jsonl`) records RunRecord fields + `plan_shape` booleans + `mx_class` from day one, so the evidence accumulates with normal use. Trigger condition: offline analysis shows a sensor class consistently winning for an archetype (e.g., HN-first for `{hn:1}` plans). The heuristic lives at the probe-target pick in snoop.py. Full design rationale: the T1 ledger design memo in `~/.gstack/projects/saraswatayu-snoop/ceo-plans/2026-06-11-stranger-proof-sensor-organ.md`.

**Effort:** M (human) / S (CC)
**Priority:** P3
**Depends on:** ≥4–6 weeks of ledger data + the N≥25 calibration baseline

### Meeting-prep render mode — second bundle consumer

**What:** A SKILL.md section (near-zero code) that drives the same sensor loop for "who am I meeting at 2pm" — emphasis on role context and body of work over reachable email.

**Why:** Cheapest possible validation of the platform premise: a second consumer proves the observation-bundle contract generalizes beyond outreach.

**Context:** Held back at the 2026-06-11 expansion ceremony. The bundle already carries everything needed; the work is prompt contract + a card emphasis variant.

**Effort:** S (human) / S (CC)
**Priority:** P3
**Depends on:** None

### GitLab sensor — profile + commit emails

**What:** GitLab analog of `gh_profile`/`git_emails` via the public GitLab API.

**Why:** Moderate yield on infra/devops targets whose work lives on gitlab.com instead of GitHub.

**Context:** Held back at the 2026-06-11 expansion ceremony. Follows the existing resolver conventions (module + `_default_*_caller` seam + network-free tests); anchor-binding rules apply unchanged.

**Effort:** M (human) / S (CC)
**Priority:** P3
**Depends on:** None

### Personal-site channel-link extraction

**What:** Emit observations for self-published channel links (Calendly, LinkedIn, X, Bluesky) found on pages `personal_site` already fetches.

**Why:** Reachability-map data with no new fetch surface — the page is already in hand; today only `mailto:` is harvested.

**Context:** Held back at the 2026-06-11 expansion ceremony. Pure link extraction, no judgment; tier mapping per the per-sensor evidence-tier table (T-minor C).

**Effort:** S (human) / S (CC)
**Priority:** P3
**Depends on:** None

### Marketplace / plugin packaging

**What:** Package snoop for a Claude Code plugin marketplace listing (one-command discoverable install).

**Why:** Distribution beyond clone-the-repo; reaches users who browse the marketplace.

**Context:** Held back at the 2026-06-11 expansion ceremony. Revisit after the calibration numbers give the README its credibility line — ship the measured story, not the vibes story.

**Effort:** M (human) / S (CC)
**Priority:** P3
**Depends on:** Calibration baseline published in README
