"""Tests for tests/calibration.py — the measurement harness.

Deterministic only — no network. Covers fixture loading, override
merge, aggregation arithmetic, and report formatting.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.calibration import (
    ResolverMetrics,
    aggregate,
    format_report,
    load_fixture,
)


def test_load_fixture_returns_public_targets(tmp_path: Path):
    pub = tmp_path / "fixture.json"
    pub.write_text(json.dumps({
        "version": 1,
        "targets": [
            {"id": "a", "name": "A"},
            {"id": "b", "name": "B"},
        ],
    }))
    targets = load_fixture(path=pub, overrides_path=tmp_path / "missing.json")
    assert {t["id"] for t in targets} == {"a", "b"}


def test_load_fixture_merges_overrides_by_id(tmp_path: Path):
    pub = tmp_path / "fixture.json"
    pub.write_text(json.dumps({
        "targets": [
            {"id": "a", "name": "Public A"},
            {"id": "b", "name": "Public B"},
        ],
    }))
    local = tmp_path / "fixture.local.json"
    local.write_text(json.dumps({
        "targets": [
            {"id": "a", "name": "Local-overridden A"},
            {"id": "c", "name": "Brand-new C"},
        ],
    }))
    targets = load_fixture(path=pub, overrides_path=local)
    by_id = {t["id"]: t for t in targets}
    assert by_id["a"]["name"] == "Local-overridden A"
    assert by_id["b"]["name"] == "Public B"
    assert by_id["c"]["name"] == "Brand-new C"


def test_aggregate_computes_hit_rate():
    target_results = [
        {
            "resolver_results": [
                {"resolver": "git_emails", "status": "ok", "candidate_count": 2,
                 "candidate_addresses": ["pete@x.com", "p@x.com"]},
            ],
            "ground_truth": {},
        },
        {
            "resolver_results": [
                {"resolver": "git_emails", "status": "empty", "candidate_count": 0,
                 "candidate_addresses": []},
            ],
            "ground_truth": {},
        },
    ]
    m = aggregate(target_results)
    assert m["git_emails"].targets_attempted == 2
    assert m["git_emails"].targets_with_candidate == 1
    assert m["git_emails"].hit_rate == 0.5


def test_aggregate_computes_precision_when_ground_truth_present():
    target_results = [
        {
            "resolver_results": [
                {"resolver": "git_emails", "status": "ok", "candidate_count": 1,
                 "candidate_addresses": ["pete@openai.com"]},
            ],
            "ground_truth": {"work": "pete@openai.com"},
        },
        {
            "resolver_results": [
                {"resolver": "git_emails", "status": "ok", "candidate_count": 1,
                 "candidate_addresses": ["wrong@guess.com"]},
            ],
            "ground_truth": {"work": "right@actual.com"},
        },
        # Target without ground truth — excluded from precision calc
        {
            "resolver_results": [
                {"resolver": "git_emails", "status": "ok", "candidate_count": 1,
                 "candidate_addresses": ["anything@example.com"]},
            ],
            "ground_truth": {},
        },
    ]
    m = aggregate(target_results)
    assert m["git_emails"].targets_with_known == 2
    assert m["git_emails"].targets_with_known_match == 1
    assert m["git_emails"].precision_on_known == 0.5


def test_aggregate_handles_empty_input():
    m = aggregate([])
    assert m == {}


def test_precision_on_known_is_none_when_no_ground_truth():
    """If no targets have ground truth, precision is meaningless → None."""
    target_results = [
        {
            "resolver_results": [
                {"resolver": "git_emails", "status": "ok", "candidate_count": 1,
                 "candidate_addresses": ["x@y.com"]},
            ],
            "ground_truth": {},
        },
    ]
    m = aggregate(target_results)
    assert m["git_emails"].precision_on_known is None


def test_aggregate_counts_status_breakdown():
    target_results = [
        {
            "resolver_results": [
                {"resolver": "git_emails", "status": "ok", "candidate_count": 1,
                 "candidate_addresses": []},
            ],
            "ground_truth": {},
        },
        {
            "resolver_results": [
                {"resolver": "git_emails", "status": "timeout", "candidate_count": 0,
                 "candidate_addresses": []},
            ],
            "ground_truth": {},
        },
        {
            "resolver_results": [
                {"resolver": "git_emails", "status": "timeout", "candidate_count": 0,
                 "candidate_addresses": []},
            ],
            "ground_truth": {},
        },
    ]
    m = aggregate(target_results)
    assert m["git_emails"].statuses == {"ok": 1, "timeout": 2}


def test_format_report_renders_metrics_table():
    metrics = {
        "git_emails": ResolverMetrics(
            name="git_emails",
            targets_attempted=10,
            targets_with_candidate=7,
            targets_with_known=5,
            targets_with_known_match=4,
            statuses={"ok": 7, "empty": 3},
        ),
    }
    target_results = [
        {
            "id": "t1", "name": "Test One",
            "ambiguity": "single_plausible_match",
            "anchors_bound": 2,
            "resolver_results": [
                {"resolver": "git_emails", "status": "ok", "candidate_count": 1,
                 "candidate_addresses": ["x@y.com"], "elapsed_ms": 250,
                 "error_detail": None},
            ],
            "final_candidates": [{
                "address": "x@y.com",
                "smtp_verdict": "verified",
                "account_exists": "unprobed",
                "sources": ["git_commit"],
            }],
            "ground_truth": {"work": "x@y.com"},
        },
    ]
    report = format_report(target_results, metrics)
    assert "# snoop calibration report" in report
    assert "git_emails" in report
    assert "70%" in report  # hit rate
    assert "80%" in report  # precision
    assert "Test One" in report
    assert "x@y.com" in report


def test_format_report_uses_em_dash_for_no_precision():
    metrics = {
        "git_emails": ResolverMetrics(
            name="git_emails",
            targets_attempted=5,
            targets_with_candidate=3,
            targets_with_known=0,  # no ground truth
        ),
    }
    report = format_report([], metrics)
    # Precision column should render "—" not "0%"
    assert "| `git_emails` | 60% | — |" in report


def test_committed_fixture_loads_and_parses():
    """The committed fixture itself should load cleanly — sanity check."""
    public_path = Path(__file__).parent / "fixtures" / "calibration_targets.json"
    if not public_path.exists():
        return  # not built yet
    data = json.loads(public_path.read_text())
    assert "version" in data
    assert isinstance(data.get("targets"), list)
    # Each target must have at least name + id + person_plan
    for t in data["targets"]:
        assert "id" in t
        assert "name" in t
        assert "person_plan" in t
        # ground_truth slots must exist (even if null)
        assert "ground_truth" in t
