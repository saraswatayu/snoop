# Changelog

All notable changes to snoop are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Internal refactors only — no behavior change. Same observation bundle, same
verdicts, same probe order; the full test suite stays green.

### Changed

- Extracted the pure decision "brain" out of the 1700-line `snoop.py` entry point
  into a `lib/pipeline/` package: `binding` (does the person, and the address,
  bind?), `candidates` (cluster + rank), `probes` (Phase-2 eligibility). `snoop.py`
  re-exports every moved symbol, so all call sites and the test monkeypatch surface
  resolve unchanged. The fan-out and probe *harness* stays in `snoop.py` by design
  (it calls sensor functions through the module namespace the tests stub).
- Drive the discovery fan-out from a `lib/sensors.py` `SENSORS` registry. Each
  sensor's gate, source type, and skip reason are now declared once, so the
  run-list and the skip-list can no longer drift apart.

### Added

- `tests/pipeline/` unit tests that import `lib.pipeline.*` directly, anchored by a
  binding truth table (including the case that employer-match + a PGP owner-UID
  must NOT bind), plus `tests/test_sensors_registry.py` asserting every registry
  `source_type` is a real `schema.SourceType`.
- `ARCHITECTURE.md` (the sensor / pipeline / analyst layering and the
  two-verifier design) and `CONTRIBUTING.md` (including a "how to add a sensor"
  recipe).

### Fixed (docs)

- `TODOS.md` no longer claims the Step-3 analyst has no tests; the eval substrate
  (`tests/evals/`) has landed. `README.md` documents `--no-search`, reflects the
  new `lib/pipeline/` and `lib/sensors.py`, and reclassifies `gh_search` (an
  identity helper) and `pattern_gen` (a no-I/O synthesizer).

## [0.2.0] — date unrecorded

The "stranger-proof sensor organ" line of work. Backfilled from git history;
entries are sourced from commit subjects.

### Added

- Identity engine (ENG-8/9/10): candidate binding requires ≥2 independent signals
  with at least one identity-bearing signal before any deliverability probe fires;
  SMTP runs only on bound candidates.
- Google People API existence check via logged-in Chrome cookies — the no-socket
  disambiguator that also runs on unbound pattern guesses on a Google-hosted
  domain; `gaia_id` clustering distinguishes aliases from namesakes.
- E1 local ledger (`~/.snoop/ledger.jsonl`): per-run yield metadata only, never
  target data.
- E2 rel=me / Bluesky reciprocal-backlink identity binding; E3 PGP
  (`keys.openpgp.org`) owner-UID corroboration.
- E6 shared wall-clock deadline across the sensor fan-out; rel=me and PGP slow
  probes run under the same budget.
- Analyst output contract: `verdict`/`marker` preserved through `--ground`, one
  vocabulary home; the belonging marker surfaces on the rendered card.
- Analyst-eval substrate under `tests/evals/`: synthetic fixtures, G1/G2 graders
  with SKILL.md drift lint, and a privacy gate.

### Changed

- `--ground` treats the observations file as authoritative and gates stdin bundles
  by schema version.
- Centralized the email regex/validator/noise-domain list and GitHub handle
  encoding (`_gh_api.quote_handle`); dropped the legacy scorer.

### Security

- SSRF hardening across the fetch surface: `is_global` requirement (rejects
  CGNAT/shared address space), a pinned validated MX IP closing a DNS-rebinding
  hole in `verify_smtp`, the `personal_site` default fetch routed through the
  guard, and bounded anonymous-HTTP reads.
- Bounded the email-regex quantifiers to remove an O(n²) ReDoS in `normalize`.
