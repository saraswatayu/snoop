"""Gate-policy aggregation tests (plan §7 infra-test mandate, §6 two-axis policy).

The gate is the eval's verdict machine; these tests judge it. They pin the
two-axis policy with hand-built GradeResults (no fixtures, no model — gate.py is
pure aggregation):

  - HARD axis (hard=True): ANY trial failing => gate fails IMMEDIATELY, no rerun
    signalled (a hard-failed fixture is not re-run — the gate is already decided).
  - SOFT axis (hard=False): a PERSISTENT failure (every trial) flags ONE rerun;
    the rerun's verdict is decided by finalize_soft_axis — still-failing fails,
    cleared passes. A FLAKY soft failure (passed at least once) does not rerun.
  - pass^k: every trial of a fixture must pass every grader.
  - edge cases: empty trial list, all-pass.
"""

from __future__ import annotations

from . import gate
from .graders import GradeResult


def _hard_pass() -> GradeResult:
    return GradeResult("g2_must_not_emit", True, True, "ok")


def _hard_fail() -> GradeResult:
    return GradeResult("g2_must_not_emit", False, True, "emitted the rival")


def _soft_pass() -> GradeResult:
    return GradeResult("g2_must_emit_recall", True, False, "ok")


def _soft_fail() -> GradeResult:
    return GradeResult("g2_must_emit_recall", False, False, "must_emit omitted")


def _other_soft_fail() -> GradeResult:
    return GradeResult("g1_marker_caps", False, False, "marker not capped")


# --------------------------------------------------------------------------- #
# Hard axis — immediate fail, no rerun
# --------------------------------------------------------------------------- #

def test_hard_axis_failure_in_any_trial_fails_the_gate_with_no_rerun():
    """A single hard-axis (must_not_emit) failure in ONE of three trials fails the
    gate immediately, records the fixture+grader+detail, and does NOT signal a
    rerun — the misattribution axis gets no retry (plan §6)."""
    trials = {
        "namesake-tempting": [
            [_hard_pass(), _soft_pass()],
            [_hard_fail(), _soft_pass()],  # one trial leaks the rival
            [_hard_pass(), _soft_pass()],
        ],
    }
    result = gate.aggregate(trials)
    assert result.passed is False
    assert result.fixtures_needing_rerun == []
    assert len(result.hard_failures) == 1
    fixture_id, grader, detail = result.hard_failures[0]
    assert fixture_id == "namesake-tempting"
    assert grader == "g2_must_not_emit"
    assert "rival" in detail
    assert result.per_fixture["namesake-tempting"].hard_failed is True
    assert result.per_fixture["namesake-tempting"].passed_k is False


def test_hard_failed_fixture_is_not_rerun_even_with_a_soft_failure_too():
    """When a fixture fails BOTH a hard and a soft grader, only the hard failure
    counts — the fixture is not added to fixtures_needing_rerun (re-running a
    fixture the gate already failed wastes tokens)."""
    trials = {
        "namesake-tempting": [
            [_hard_fail(), _soft_fail()],
        ],
    }
    result = gate.aggregate(trials)
    assert result.passed is False
    assert result.fixtures_needing_rerun == []
    assert result.suite_aggregates.soft_flagged == 0


# --------------------------------------------------------------------------- #
# Soft axis — single rerun, then decide
# --------------------------------------------------------------------------- #

def test_soft_axis_persistent_failure_flags_one_rerun_without_failing_yet():
    """A soft grader failing in EVERY trial (persistent) and no hard failure:
    aggregate() does NOT fail the gate yet — it flags the fixture for one rerun
    and leaves `passed` True, deferring the verdict to finalize_soft_axis."""
    trials = {
        "happy-dev": [
            [_hard_pass(), _soft_fail()],
            [_hard_pass(), _soft_fail()],
        ],
    }
    result = gate.aggregate(trials)
    assert result.passed is True  # deferred, not failed
    assert result.fixtures_needing_rerun == ["happy-dev"]
    assert result.hard_failures == []
    assert result.suite_aggregates.soft_flagged == 1
    assert result.per_fixture["happy-dev"].soft_persistent_graders == \
        ["g2_must_emit_recall"]


def test_soft_axis_rerun_still_failing_fails_the_gate():
    """The flagged fixture's rerun still fails the soft grader in every trial:
    finalize_soft_axis confirms the persistent failure and fails the gate, naming
    the fixture+grader."""
    first = gate.aggregate({
        "happy-dev": [[_hard_pass(), _soft_fail()], [_hard_pass(), _soft_fail()]],
    })
    final = gate.finalize_soft_axis(first, {
        "happy-dev": [[_hard_pass(), _soft_fail()], [_hard_pass(), _soft_fail()]],
    })
    assert final.passed is False
    assert final.fixtures_needing_rerun == []
    assert any(fid == "happy-dev" and grader == "g2_must_emit_recall"
               for fid, grader, _ in final.hard_failures)


def test_soft_axis_rerun_clears_when_failure_does_not_persist():
    """The rerun no longer shows the persistent failure (a trial passes the
    previously-failing soft grader): finalize_soft_axis clears the fixture and the
    gate passes — the soft axis tolerates one flaky pass."""
    first = gate.aggregate({
        "happy-dev": [[_hard_pass(), _soft_fail()], [_hard_pass(), _soft_fail()]],
    })
    final = gate.finalize_soft_axis(first, {
        "happy-dev": [[_hard_pass(), _soft_pass()], [_hard_pass(), _soft_fail()]],
    })
    assert final.passed is True
    assert final.hard_failures == []


def test_finalize_catches_a_new_soft_grader_that_persists_in_the_rerun():
    """finding #5: the rerun must catch ANY soft grader persistent across it, not
    only the original complaint. A fixture flagged for g2_must_emit_recall whose
    rerun clears recall but now persistently fails g1_marker_caps fails the gate —
    the old previously∩rerun intersection silently dropped the new failure."""
    first = gate.aggregate({
        "happy-dev": [[_hard_pass(), _soft_fail()], [_hard_pass(), _soft_fail()]],
    })
    assert first.fixtures_needing_rerun == ["happy-dev"]
    final = gate.finalize_soft_axis(first, {
        "happy-dev": [
            [_hard_pass(), _soft_pass(), _other_soft_fail()],
            [_hard_pass(), _soft_pass(), _other_soft_fail()],
        ],
    })
    assert final.passed is False
    assert any(fid == "happy-dev" and grader == "g1_marker_caps"
               for fid, grader, _ in final.hard_failures)


def test_finalize_fails_closed_when_rerun_trials_are_missing():
    """finding #5: a soft-flagged fixture whose rerun trials never arrived has no
    evidence it recovered. finalize_soft_axis must FAIL it (fail closed), not
    silently clear it — the old code returned passed=True on an empty rerun."""
    first = gate.aggregate({
        "happy-dev": [[_hard_pass(), _soft_fail()], [_hard_pass(), _soft_fail()]],
    })
    assert first.fixtures_needing_rerun == ["happy-dev"]
    final = gate.finalize_soft_axis(first, {})  # no rerun trials supplied
    assert final.passed is False
    assert any(fid == "happy-dev" for fid, _, _ in final.hard_failures)


def test_finalize_preserves_an_earlier_hard_failure():
    """If the first pass already had a hard failure, finalize_soft_axis must keep
    the gate failed regardless of any rerun outcome — a hard failure is
    terminal."""
    first = gate.aggregate({
        "namesake-tempting": [[_hard_fail(), _soft_pass()]],
        "happy-dev": [[_hard_pass(), _soft_fail()], [_hard_pass(), _soft_fail()]],
    })
    assert first.passed is False
    final = gate.finalize_soft_axis(first, {
        "happy-dev": [[_hard_pass(), _soft_pass()], [_hard_pass(), _soft_pass()]],
    })
    assert final.passed is False
    assert any(grader == "g2_must_not_emit" for _, grader, _ in final.hard_failures)


def test_flaky_soft_failure_does_not_trigger_a_rerun():
    """A soft grader failing in only ONE of several trials (not persistent) drops
    pass^k to False but does NOT flag a rerun — the rerun is reserved for
    persistent soft failures, not flake."""
    trials = {
        "happy-dev": [
            [_hard_pass(), _soft_pass()],
            [_hard_pass(), _soft_fail()],  # flaky: fails once, passes once
            [_hard_pass(), _soft_pass()],
        ],
    }
    result = gate.aggregate(trials)
    assert result.passed is True
    assert result.fixtures_needing_rerun == []
    assert result.per_fixture["happy-dev"].passed_k is False
    assert result.per_fixture["happy-dev"].soft_persistent_graders == []


# --------------------------------------------------------------------------- #
# pass^k arithmetic across k trials
# --------------------------------------------------------------------------- #

def test_pass_k_true_only_when_every_trial_passes_every_grader():
    """pass^k arithmetic: a fixture green in all k trials is passed_k; flipping a
    single grader in a single trial drops it to False (the τ-bench metric — k
    samples prove k samples)."""
    all_green = {"happy-dev": [[_hard_pass(), _soft_pass()] for _ in range(4)]}
    res = gate.aggregate(all_green)
    assert res.per_fixture["happy-dev"].passed_k is True
    assert res.per_fixture["happy-dev"].trials == 4
    assert res.suite_aggregates.passed_k == 1

    one_flip = {"happy-dev": [[_hard_pass(), _soft_pass()] for _ in range(3)]
                + [[_hard_pass(), _soft_fail()]]}
    res2 = gate.aggregate(one_flip)
    assert res2.per_fixture["happy-dev"].passed_k is False


def test_pass_k_rate_helper_counts_fixtures_at_pass_k():
    """pass_k_rate is the fraction of fixtures at pass^k: two of three green here
    is 2/3."""
    res = gate.aggregate({
        "a": [[_hard_pass(), _soft_pass()], [_hard_pass(), _soft_pass()]],
        "b": [[_hard_pass(), _soft_pass()], [_hard_pass(), _soft_pass()]],
        "c": [[_hard_pass(), _soft_pass()], [_hard_pass(), _soft_fail()]],
    })
    assert gate.pass_k_rate(res.per_fixture.values()) == 2 / 3


# --------------------------------------------------------------------------- #
# Edge cases — empty trials, all-pass, empty suite
# --------------------------------------------------------------------------- #

def test_all_pass_run_passes_with_no_hard_failures_and_no_rerun():
    """The happy gate: every fixture green across every trial — passed True, no
    hard failures, nothing to rerun, suite counts all at pass^k."""
    trials = {
        "happy-dev": [[_hard_pass(), _soft_pass()], [_hard_pass(), _soft_pass()]],
        "ungrounded-skeptic": [[_hard_pass(), _soft_pass()]],
    }
    result = gate.aggregate(trials)
    assert result.passed is True
    assert result.hard_failures == []
    assert result.fixtures_needing_rerun == []
    assert result.suite_aggregates.fixtures == 2
    assert result.suite_aggregates.passed_k == 2


def test_empty_trial_list_is_not_pass_k_but_does_not_fail_the_gate():
    """A fixture with zero trials (the runner gathered none): pass^k over zero
    samples is NOT a pass, so it never looks green — but with nothing failing it
    does not fail the gate or flag a rerun either."""
    result = gate.aggregate({"happy-dev": []})
    agg = result.per_fixture["happy-dev"]
    assert agg.trials == 0
    assert agg.passed_k is False
    assert agg.hard_failed is False
    assert result.passed is True
    assert result.fixtures_needing_rerun == []
    assert result.suite_aggregates.passed_k == 0


def test_empty_suite_fails_closed():
    """No fixtures at all is a MISCONFIGURED run (wrong dir, a glob that matched
    nothing, an import error that skipped loading SPECS) — the gate must NOT read
    green when it measured nothing. It fails closed with a hard failure."""
    result = gate.aggregate({})
    assert result.passed is False
    assert result.hard_failures  # carries a "ran zero fixtures" hard failure
    assert result.suite_aggregates.fixtures == 0
    assert gate.pass_k_rate(result.per_fixture.values()) == 0.0


def test_roster_fails_closed_on_missing_or_empty_expected_fixture():
    """When the expected fixture roster is supplied, any expected fixture that
    is absent OR ran zero trials fails the gate — catching an under-populated
    run that would otherwise pass on the fixtures that did run."""
    trials = {"happy-dev": [[_hard_pass()]]}
    # 'm365-exec' is expected but never ran; 'dead-end' ran but with zero trials.
    result = gate.aggregate(
        {**trials, "dead-end": []},
        expected_fixtures=["happy-dev", "m365-exec", "dead-end"],
    )
    assert result.passed is False
    failed = {fid for fid, _grader, _detail in result.hard_failures}
    assert "m365-exec" in failed  # missing entirely
    assert "dead-end" in failed   # present but zero trials


def test_roster_passes_when_all_expected_fixtures_ran():
    trials = {
        "happy-dev": [[_hard_pass()]],
        "m365-exec": [[_hard_pass()]],
    }
    result = gate.aggregate(trials, expected_fixtures=["happy-dev", "m365-exec"])
    assert result.passed is True


def test_empty_roster_with_no_trials_fails_closed():
    """An EMPTY expected_fixtures list with zero trials is the same
    misconfiguration as no roster at all (SPECS failed to import -> the runner
    derives an empty roster AND runs nothing). `[] is not None` must NOT route
    this around the zero-fixture guard and read green on a run that measured
    nothing."""
    result = gate.aggregate({}, expected_fixtures=[])
    assert result.passed is False
    assert result.hard_failures
    assert any("zero fixtures" in detail for _f, _g, detail in result.hard_failures)
