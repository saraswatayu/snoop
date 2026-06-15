# Contributing

snoop is a Claude Code skill. The product is the **sensor + the analyst contract**,
not a human CLI — judge a change by whether it gives Claude better evidence or makes
the evidence easier to reason over. Read [ARCHITECTURE.md](ARCHITECTURE.md) first;
it's the map.

## Setup

```bash
git clone https://github.com/saraswatayu/snoop.git ~/.claude/skills/snoop
cd ~/.claude/skills/snoop
pip install -r requirements.txt          # one dep: dnspython (for MX lookups)
python3 -m pytest tests/                 # the gate — must stay green, no network
```

The whole suite is **network-free and deterministic**. `tests/conftest.py` has an
autouse fixture that stubs the unconditional network calls (PGP, rel=me, ledger
write); the shared HTTP fake is `tests/_http_harness.py`. Integration checks that
hit real services are marked `@pytest.mark.network` and run only with
`pytest -m network`. A change that makes the default suite touch the network is a
regression even if it passes.

## Test philosophy

Well-tested is non-negotiable, and tests mirror the source 1:1
(`tests/test_<module>.py` per `lib/<module>.py`). The pure pipeline brain has its
own direct-import unit tests under `tests/pipeline/`; the entry-point harness is
tested through `tests/test_snoop_entry.py` with the sensor functions monkeypatched
on the `snoop` module (see "Why the harness stays in snoop.py" in ARCHITECTURE.md).
Prefer too many tests to too few; cover the edge cases and the error paths, not just
the happy path.

## How to add a discovery sensor

A discovery sensor reads a public source and returns a `ResolverResult`. There are
four touch-points; the registry's job is to keep them from drifting (the gate is
declared **once**, and a test enforces the source type).

1. **The module.** `lib/<name>.py` with a public entry function
   `fetch_<name>(...) -> ResolverResult`. Do HTTP through `lib.fetch.fetch` (it's
   SSRF-guarded) or, for GitHub, `lib._gh_api.GhCaller`. Accept an injectable fetch
   seam (e.g. `http_get=None`, defaulting to the real one) so the unit test can stub
   it without a network round-trip. Classify your own `status`
   (`ok`/`empty`/`error`/…) and set `error_detail` on failure.

2. **The source type.** Add a literal to `SourceType` in `lib/schema.py`. Every
   `Source.type` a sensor emits must be a member; `tests/test_sensors_registry.py`
   fails if a registry entry names one that isn't.

3. **The registry row.** Add a `SensorSpec` to `DISCOVERY_SENSORS` in
   `lib/sensors.py`: `name`, `source_type`, a `gate` predicate over `SensorContext`
   (when should it run?), and the `skip_reason` shown when it's gated off. This is
   the **only** place the gate is written — `run_pipeline` reads it to decide what
   runs, and `_sensor_skips` reads the same spec to emit the skip. If your gate needs
   an input the context doesn't carry, add a field to `SensorContext`.

4. **The task builder.** In `run_pipeline` (snoop.py), add one line to the `builders`
   dict keyed by your sensor name: `"<name>": lambda: fetch_<name>(...)`. This stays
   in snoop.py on purpose — the call resolves `fetch_<name>` from the snoop module
   namespace, which is how the test suite stubs it via
   `monkeypatch.setattr(snoop, "fetch_<name>", ...)`. Add the corresponding
   `from lib.<name> import fetch_<name>` at the top.

If the source can **bind identity** (an address observed on it should count toward
the ≥2-signal rule), also wire it into `lib/pipeline/binding.py`: add the type to
`GITHUB_SURFACES` / `DOMAIN_SURFACES`, or extend `candidate_is_bound`. Be
conservative — binding is the stranger-proofing, and a new identity-bearing surface
is a security-relevant change. Add a case to `tests/pipeline/test_binding.py`.

Finally: `tests/test_<name>.py` (network-free, via `tests/_http_harness.py`).

`lib/pattern_gen.py` (a synthesizer, no I/O) and `lib/gh_search.py` (an identity
helper invoked inside `person_resolve`, not a fan-out sensor) are worth reading as
the two shapes that look like sensors but aren't — don't copy them as a template for
a new discovery sensor.

## How to add a Phase-2 enricher

Enrichers (`verify_smtp`, `google_account`) take prior candidates and **mutate them
in place** rather than returning a `ResolverResult`. They are NOT in the discovery
registry. They run in the probe harness (`_probe_candidates` / `_run_phase2_probes`
in snoop.py) over deep copies, with verdicts merged back by `_merge_probe_verdicts`
only if the probe finishes inside the shared deadline. If your enricher opens a
socket to the mailbox (like SMTP), it MUST run only on `candidate_is_bound`
candidates. If it's a no-socket existence check (like the Google People API), it may
also run on unbound Workspace pattern guesses — but document why.

## Conventions

- Match the surrounding code's style, comment density, and naming. Docstrings
  explain *why*, not just *what*.
- Embed ASCII diagrams in comments for non-obvious data flow, state, or pipelines —
  and keep them current when you change the code they describe. A stale diagram is
  worse than none.
- DRY: a vocabulary or rule lives in one place (`lib/schema.py` for the bundle
  contract). Don't hand-copy a literal that can drift.

## Privacy invariants (do not break)

- The ledger (`~/.snoop/ledger.jsonl`) stores yield metadata only — sensor timing,
  plan *shape* (booleans), MX class. **Never** names, addresses, handles, or
  domains. A CI test enforces the schema.
- The committed calibration fixture holds only synthetic shapes with null ground
  truth; real targets live only in the gitignored `*.local.json`.
- Eval fixtures that could carry real identities are gitignored
  (`tests/evals/fixtures/*.local.json`).
- Scope is bounded on purpose: self-published, real-identity, source-bound facts
  only. No de-anonymization, no location/family/sensitive-attribute inference, no
  face matching, no bulk. A change that widens scope is a product decision, not a
  refactor.
