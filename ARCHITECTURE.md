# Architecture

snoop is a **sensor**; the host model (Claude Code) is the **analyst**. snoop does
the I/O a model can't — git commits, the GitHub API, a personal site's `mailto:`,
the SMTP `RCPT` handshake, MX lookups, the Google People API — and emits a typed
**observation bundle**. Claude reasons over the bundle (picks the email, judges the
namesake, writes the prose); then `snoop --ground` deterministically checks that
every claim cites a real observation before the card renders.

```
plan → snoop --observations → (Claude reasons) → snoop --ground → present
```

This doc is the map of how the Python actually fits together. For the runtime loop
Claude follows, see [SKILL.md](SKILL.md); for the user-facing pitch, the
[README](README.md).

## The four layers

```
                 ┌──────────────────────────────────────────────┐
   entry point   │  snoop.py                                     │
                 │    argparse · mode dispatch · the fan-out and │
                 │    probe HARNESS (threads, deadlines, ledger) │
                 └───────────────┬──────────────────────────────┘
                                 │ imports
        ┌────────────────────────┼─────────────────────────┐
        ▼                        ▼                          ▼
  ┌───────────┐          ┌────────────────┐         ┌──────────────┐
  │ sensors   │          │ pipeline       │         │ analyst      │
  │ (I/O)     │          │ (pure brain)   │         │ (bundle/     │
  │           │          │                │         │  verify/     │
  │ git_emails│          │ binding        │         │  present)    │
  │ gh_profile│          │ candidates     │         │ reason       │
  │ personal_ │          │ probes         │         │ ground       │
  │  site …   │          │ + sensors.py   │         │ render       │
  └─────┬─────┘          │   (registry)   │         └──────┬───────┘
        │                └────────┬───────┘                │
        └──────────────┬──────────┴────────────────────────┘
                       ▼
                 ┌───────────────────────────────────────────┐
                 │ infra: fetch (SSRF-guarded) · _gh_api ·    │
                 │ normalize · schema · ledger · diagnose ·   │
                 │ chrome_cookies · person_resolve            │
                 └───────────────────────────────────────────┘

   dependency arrow is one-way: snoop.py → pipeline → sensors → infra.
   sensors and pipeline never import snoop.py.
```

| Layer | Modules | Job |
|---|---|---|
| **Sensors** (I/O) | `git_emails`, `gh_profile`, `personal_site`, `hn_profile`, `package_registry`, `pgp_keyserver`, `rel_me`, `google_account` (+`chrome_cookies`), `verify_smtp` | Read public sources, return typed readings. `pattern_gen` is a *synthesizer* (name×domain templates, no I/O); `gh_search` is an *identity helper* run inside `person_resolve`, not a fan-out sensor. |
| **Pipeline** (pure brain) | `lib/pipeline/{binding,candidates,probes}.py`, `lib/sensors.py` | The decision logic: does the person/address bind, how candidates cluster and rank, which candidate is eligible for which Phase-2 probe. No network, no stdin/stdout. |
| **Analyst** | `lib/reason.py` (`build_evidence`), `lib/ground.py`, `lib/render.py` | Flatten the resolved person + candidates into the bundle; deterministically drop uncited facts; render the card. |
| **Infra** | `lib/{fetch,_gh_api,normalize,schema,ledger,diagnose,chrome_cookies,person_resolve}.py` | SSRF-guarded HTTP, the GitHub caller, name parsing, the typed contract, local yield ledger, capability probes, cookie loading, identity resolution. |

## Data flow

The end-to-end sequence (driven by `main()` in `snoop.py`; the inline docstring
there is the step-by-step):

```
person_resolve.resolve_person          validate identity anchors, surface deltas
        │
fan-out (ThreadPoolExecutor-style daemon threads, one shared deadline)
   git_emails · gh_profile · hn_profile · personal_site · package_registry ·
   pattern_gen           ← which run is decided by lib/sensors.DISCOVERY_SENSORS
        │
pipeline.candidates.cluster_candidates  dedupe by address, merge sources
        │
pipeline.candidates.probe_rank          observed addresses before pure guesses
        │
slow probes (rel_me E2, pgp E3) under the same shared deadline
        │
Phase-2 probes (under the remaining deadline):
   pipeline.binding.candidate_is_bound  ← the bind gate (≥2 independent signals)
   google_account existence check       bound candidates + unbound Workspace guesses
   verify_smtp RCPT                      BOUND candidates only (it opens a socket)
        │
reason.build_evidence → the observation bundle (JSON)
        │
(Claude reasons, emits facts) → ground() drops uncited facts → render the card
```

## Two sensor phases

Not all sensors have the same shape. There are two kinds, and they're wired
differently:

- **Discovery sensors** return a `ResolverResult` (a typed list of candidates +
  status). They run in the Phase-1 fan-out, are timed and clustered uniformly, and
  are declared in the `lib/sensors.DISCOVERY_SENSORS` registry. Adding one is a
  registry row + a module + a test (see [CONTRIBUTING.md](CONTRIBUTING.md)).
- **Enrichers** (`verify_smtp`, `google_account`) take *prior* candidates and
  **mutate them in place** (set `smtp_verdict`, `account_exists`, `gaia_id`, …).
  They run in the Phase-2 probe harness over deep copies, with verdicts merged back
  only if the probe finishes inside the deadline. They are deliberately NOT in the
  discovery registry.

## Two verifiers, two axes

snoop's trustworthiness comes from policing two independent questions with two
independent mechanisms:

```
   IDENTITY  — does this address belong to the person?
               pipeline.binding.candidate_is_bound: ≥2 independent signals, at
               least one identity-bearing. SMTP fires ONLY on bound candidates,
               so snoop never RCPT-probes a same-named stranger.

   PROVENANCE — does each sentence cite something real?
               lib.ground.ground(): drops any fact whose citation doesn't point
               at an observation the sensors actually produced.
```

The binding rule is the stranger-proofing; it lives in `lib/pipeline/binding.py`
and is unit-tested in `tests/pipeline/test_binding.py` (the truth table, including
the load-bearing case that employer-match + a PGP key — two corroborating but
target-agnostic signals — must NOT bind).

## Why the harness stays in `snoop.py`

The pure decision logic (binding, clustering, ranking, probe-eligibility) lives in
`lib/pipeline/`. The fan-out and probe **harness** — `run_pipeline`,
`_probe_candidates`, `_run_phase2_probes`, the slow-probe threads — deliberately
stays in `snoop.py`.

The reason is the test contract. The harness calls sensor functions
(`fetch_git_emails`, `verify_candidates`, `resolve_person`, …) resolved from the
`snoop` module namespace, and the test suite stubs them with
`monkeypatch.setattr(snoop, "fetch_*", ...)`. Moving the harness into a submodule
would make those calls resolve from the submodule's namespace instead, silently
breaking the injection surface across ~50 test sites. So the split is by
testability, not by aesthetics: pure logic moves out and gets its own unit tests;
the monkeypatch-coupled plumbing stays where the patches can reach it. The moved
functions are re-exported from `snoop.py` under their prior names, so every
`snoop.X` reference still resolves.

## The observation bundle

The contract between snoop and Claude. Defined in `lib/schema.py`
(`BUNDLE_SCHEMA_VERSION`). Each observation has a stable `id`, a typed `content`
line, an optional structured `data` mirror, and a `source_url`. The verdict
vocabularies — `EMAIL_VERDICTS` (deliverability), `FACT_KINDS`, `BELONGS_MARKERS` —
live in `schema.py` as the single home so the graders, the renderer, and SKILL.md
don't each carry a drifting copy. `--ground` rejects a stale-schema bundle and
tells you to re-run `--observations`.
