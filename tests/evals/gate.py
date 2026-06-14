"""Two-axis gate aggregation (plan §6, brief D9). PURE: no I/O, no LLM, no
network — given the GradeResults the runner already collected across k trials,
it computes per-fixture pass^k, suite aggregates, and applies the gate policy.

The gate distinguishes two axes (the GradeResult.hard flag set by each grader):

  - HARD axes (g1_citation, g2_must_not_emit — the misattribution axis): ANY
    hard-axis GradeResult failing in ANY trial fails the gate IMMEDIATELY. No
    retry, no threshold. A 40-for-40 stochastic gate false-fails ~1/3 of the time
    at true 99% reliability and gets ignored (eng review issue 3), so only the
    safety-critical axis gets the zero-tolerance treatment.

  - SOFT axes (recall, verdict wording, marker caps, confidence ordering): a
    persistent failure across the initial trials marks the fixture for ONE full
    re-run. The rerun itself is the RUNNER's job (it spends tokens); gate.py only
    SIGNALS which fixtures need it via `fixtures_needing_rerun`, then a second
    call — finalize_soft_axis — consumes the rerun trials and renders the final
    soft-axis verdict.

Why two calls instead of one: the rerun is an expensive side effect the pure
aggregator must not perform. aggregate() decides everything it can from the
trials in hand and hands back a rerun list; the caller runs those fixtures again
and feeds the new trials to finalize_soft_axis(). Each call is pure and
independently testable.

Vocabulary:
  - "trial" = one model sample's full set of GradeResults (a list[GradeResult]).
  - "pass^k" = every trial of a fixture passed every grader (the τ-bench metric:
    k samples prove k samples, so a single failing trial drops pass^k to False).

Input shape (matches what the runner accumulates):
    trials_by_fixture: dict[str, list[list[GradeResult]]]
        fixture_id -> [trial0_results, trial1_results, ...]
A grader that crashed should be recorded by the runner as a failing GradeResult
(passed=False) rather than omitted, so the gate never silently ignores a missing
axis — but the aggregation tolerates an empty trial list (no trials => that
fixture is vacuously not-failing but contributes 0 to pass^k counts).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .graders import GradeResult


# --------------------------------------------------------------------------- #
# Per-fixture + suite aggregates
# --------------------------------------------------------------------------- #

@dataclass
class FixtureAggregate:
    """One fixture's roll-up across its k trials.

    `passed_k` is the strict pass^k (every trial passed every grader).
    `hard_failed` is True if ANY hard-axis grader failed in ANY trial — the gate
    keys its immediate-fail decision off this. `soft_failed_initial` is True if a
    SOFT-axis grader failed persistently (in EVERY trial) during the initial run;
    that is what marks the fixture for a single rerun (a flaky soft failure that
    passed at least once does not, per the soft-axis "persistent failure"
    policy)."""
    fixture_id: str
    trials: int = 0
    passed_k: bool = True
    hard_failed: bool = False
    soft_failed_initial: bool = False
    # The (grader, detail) of each hard-axis failure, for the report.
    hard_failures: list[tuple[str, str]] = field(default_factory=list)
    # Names of soft-axis graders that failed in EVERY trial (the persistent set).
    soft_persistent_graders: list[str] = field(default_factory=list)


@dataclass
class SuiteAggregate:
    """Counts across all fixtures in one aggregation pass."""
    fixtures: int = 0
    passed_k: int = 0
    hard_failed: int = 0
    soft_flagged: int = 0


@dataclass
class GateResult:
    """The structured result the runner consumes (plan §6).

    `passed` is the gate verdict THIS aggregation can decide: it is False as soon
    as any hard axis fails. When only soft axes are unresolved (fixtures_needing_
    rerun non-empty and no hard failure), `passed` stays True here — the soft
    verdict is deferred to finalize_soft_axis after the rerun. A run with neither
    hard failures nor rerun flags passes outright."""
    passed: bool
    hard_failures: list[tuple[str, str, str]]  # (fixture_id, grader, detail)
    fixtures_needing_rerun: list[str]
    per_fixture: dict[str, FixtureAggregate]
    suite_aggregates: SuiteAggregate


def _grade_trials(trials: list[list[GradeResult]],
                  fixture_id: str) -> FixtureAggregate:
    """Roll one fixture's trials into a FixtureAggregate. A trial is a
    list[GradeResult]; pass^k requires every grader of every trial to pass."""
    agg = FixtureAggregate(fixture_id=fixture_id, trials=len(trials))
    if not trials:
        # No trials ran: nothing failed, but pass^k over zero samples is not a
        # pass — the suite count below treats trials==0 as "not passed_k" so an
        # empty run never looks green.
        agg.passed_k = False
        return agg

    # Track, per soft-axis grader, whether it failed in EVERY trial (persistent).
    soft_grader_names: set[str] = set()
    soft_fail_counts: dict[str, int] = {}

    for trial in trials:
        for res in trial:
            if not res.passed:
                agg.passed_k = False
            if res.hard:
                if not res.passed:
                    agg.hard_failed = True
                    agg.hard_failures.append((res.grader, res.detail))
            else:
                soft_grader_names.add(res.grader)
                if not res.passed:
                    soft_fail_counts[res.grader] = soft_fail_counts.get(res.grader, 0) + 1

    # A soft grader is "persistent" only if it failed in EVERY trial — a soft
    # failure that passed at least once is flaky, not persistent, and does not
    # trigger the (token-spending) rerun.
    n = len(trials)
    agg.soft_persistent_graders = sorted(
        g for g in soft_grader_names if soft_fail_counts.get(g, 0) == n
    )
    agg.soft_failed_initial = bool(agg.soft_persistent_graders)
    return agg


def aggregate(
    trials_by_fixture: dict[str, list[list[GradeResult]]],
) -> GateResult:
    """First pass: roll every fixture's trials, apply the hard axis immediately,
    and SIGNAL (not perform) the soft-axis reruns.

    Returns a GateResult whose `passed` is False iff a hard axis failed in any
    trial. `fixtures_needing_rerun` lists fixtures whose ONLY unresolved issue is
    a persistent soft-axis failure (a fixture that already hard-failed is not
    re-run — the gate is decided). The runner re-runs those fixtures and feeds the
    new trials to finalize_soft_axis."""
    per_fixture: dict[str, FixtureAggregate] = {}
    hard_failures: list[tuple[str, str, str]] = []
    rerun: list[str] = []
    suite = SuiteAggregate(fixtures=len(trials_by_fixture))

    for fixture_id, trials in trials_by_fixture.items():
        agg = _grade_trials(trials, fixture_id)
        per_fixture[fixture_id] = agg
        if agg.passed_k:
            suite.passed_k += 1
        if agg.hard_failed:
            suite.hard_failed += 1
            for grader, detail in agg.hard_failures:
                hard_failures.append((fixture_id, grader, detail))
        elif agg.soft_failed_initial:
            # No hard failure but a persistent soft failure: this fixture earns a
            # rerun. A hard-failed fixture is NOT re-run (the gate already fails).
            suite.soft_flagged += 1
            rerun.append(fixture_id)

    return GateResult(
        passed=not hard_failures,
        hard_failures=hard_failures,
        fixtures_needing_rerun=sorted(rerun),
        per_fixture=per_fixture,
        suite_aggregates=suite,
    )


def finalize_soft_axis(
    first: GateResult,
    rerun_trials_by_fixture: dict[str, list[list[GradeResult]]],
) -> GateResult:
    """Second pass: consume the rerun trials for the soft-flagged fixtures and
    decide the soft axis. Per the policy (plan §6): one full re-run of the failing
    fixture; PERSISTENT failure (still failing a soft grader in every rerun trial)
    fails the gate.

    Inputs:
      - `first`: the GateResult from aggregate() (carries the original hard
        verdict, which finalize must preserve — a hard failure stays a failure).
      - `rerun_trials_by_fixture`: the NEW trials the runner gathered for the
        fixtures in first.fixtures_needing_rerun.

    A soft-flagged fixture passes the soft axis if its rerun no longer shows the
    persistent failure (any rerun trial passing the previously-failing soft
    grader clears it). A fixture still persistently failing a soft grader in the
    rerun fails the gate. The returned GateResult is the FINAL verdict."""
    # Preserve the hard verdict: a hard failure in the first pass is terminal.
    final_hard_failures = list(first.hard_failures)
    soft_failures: list[tuple[str, str, str]] = []

    for fixture_id in first.fixtures_needing_rerun:
        rerun = rerun_trials_by_fixture.get(fixture_id, [])
        if not rerun:
            # Fail closed (finding #5): a fixture flagged for a rerun whose rerun
            # trials never arrived has NO evidence it recovered. A measurement
            # instrument must not absolve a persistent soft failure on missing
            # data — the old code returned passed=True here.
            soft_failures.append(
                (fixture_id, "<rerun>",
                 "soft-flagged fixture had no rerun trials (failing closed)"))
            continue
        rerun_agg = _grade_trials(rerun, fixture_id)
        # Any soft grader still failing EVERY rerun trial is a confirmed,
        # gate-failing soft failure — INCLUDING one that newly persists in the
        # rerun, not only the original complaint (finding #5: the old
        # previously∩rerun intersection silently dropped a newly-persistent soft
        # failure observed in the rerun).
        for grader in rerun_agg.soft_persistent_graders:
            soft_failures.append(
                (fixture_id, grader,
                 "soft-axis failure persisted across the rerun"))

    passed = not final_hard_failures and not soft_failures
    return GateResult(
        passed=passed,
        hard_failures=final_hard_failures + soft_failures,
        fixtures_needing_rerun=[],  # resolved — nothing left to re-run
        per_fixture=first.per_fixture,
        suite_aggregates=first.suite_aggregates,
    )


def pass_k_rate(per_fixture: Iterable[FixtureAggregate]) -> float:
    """Fraction of fixtures that achieved pass^k (every trial passed every
    grader). A convenience for the report header; 0.0 over an empty set."""
    aggs = list(per_fixture)
    if not aggs:
        return 0.0
    return sum(1 for a in aggs if a.passed_k) / len(aggs)
