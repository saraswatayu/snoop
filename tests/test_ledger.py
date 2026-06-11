"""Tests for lib/ledger.py — the local sensor-yield ledger (E1).

The headline test (finding 3C) is the schema allowlist: it proves the ledger can
NEVER carry target data (names, addresses, handles, domains). The rest cover the
best-effort/atomic append, the malformed-line-skipping reader, and health.
"""

from __future__ import annotations

import json

from lib.ledger import (
    LEDGER_SENSOR_KEYS,
    LEDGER_TOP_KEYS,
    append_run,
    build_record,
    ledger_health,
    read_records,
    validate_record,
)


def _shape(**kw):
    base = {"github": False, "hn": False, "personal_domains": False,
            "packages": False, "employer": False}
    base.update(kw)
    return base


def _record():
    return build_record(
        plan_shape=_shape(github=True, personal_domains=True),
        mx_class="google", candidates=3,
        sensors=[{"sensor": "git_emails", "status": "ran", "elapsed_ms": 120,
                  "outcome": "candidates", "reason": "should be dropped"},
                 {"sensor": "smtp", "status": "skipped", "reason": "--no-smtp"}],
        today="2026-06-11",
    )


# ---- 3C: the no-target-data schema (the privacy guarantee) -------------------


def test_build_record_is_schema_clean():
    assert validate_record(_record()) == []


def test_build_record_drops_non_allowlisted_sensor_keys():
    rec = _record()
    # 'reason' (which can carry error text / hostnames) is NOT persisted
    for s in rec["sensors"]:
        assert set(s) <= LEDGER_SENSOR_KEYS
        assert "reason" not in s


def test_record_has_only_allowlisted_top_keys():
    assert set(_record()) <= LEDGER_TOP_KEYS


def test_validate_rejects_a_smuggled_target_field():
    rec = _record()
    rec["name"] = "Mihika Kapoor"           # a target field sneaking in
    errs = validate_record(rec)
    assert any("unexpected top-level keys" in e for e in errs)


def test_validate_rejects_smuggled_sensor_field():
    rec = _record()
    rec["sensors"][0]["address"] = "x@y.com"
    assert any("unexpected keys" in e for e in validate_record(rec))


def test_validate_rejects_timestamp_with_time_of_day():
    rec = _record()
    rec["ts"] = "2026-06-11T14:15:22"        # must be date-only
    assert any("YYYY-MM-DD" in e for e in validate_record(rec))


def test_validate_rejects_bad_mx_class():
    rec = _record()
    rec["mx_class"] = "aws"
    assert any("mx_class" in e for e in validate_record(rec))


def test_mx_class_unknown_coerced_to_other():
    rec = build_record(plan_shape=_shape(), mx_class="weird", candidates=0,
                       sensors=[], today="2026-06-11")
    assert rec["mx_class"] == "other"


# ---- append / read (atomic, best-effort) ------------------------------------


def test_append_and_read_roundtrip(tmp_path):
    p = tmp_path / "ledger.jsonl"
    assert append_run(_record(), path=p) is True
    assert append_run(_record(), path=p) is True
    records, malformed = read_records(p)
    assert len(records) == 2 and malformed == 0
    # each line is a single compact JSON object
    lines = p.read_text().splitlines()
    assert len(lines) == 2 and all(json.loads(ln) for ln in lines)


def test_append_creates_parent_dir(tmp_path):
    p = tmp_path / "deep" / "nested" / "ledger.jsonl"
    assert append_run(_record(), path=p) is True
    assert p.exists()


def test_append_is_best_effort_on_bad_path(tmp_path):
    # a path whose parent is a file (not a dir) → write fails, returns False, no raise
    bad_parent = tmp_path / "afile"
    bad_parent.write_text("x")
    assert append_run(_record(), path=bad_parent / "ledger.jsonl") is False


def test_oversize_line_dropped(tmp_path):
    p = tmp_path / "ledger.jsonl"
    huge = build_record(plan_shape=_shape(), mx_class="none", candidates=0,
                        sensors=[{"sensor": "x" * 30, "status": "ran",
                                  "outcome": "y"} for _ in range(500)],
                        today="2026-06-11")
    assert append_run(huge, path=p) is False  # >4KB → dropped, not truncated


def test_reader_skips_malformed_lines(tmp_path):
    p = tmp_path / "ledger.jsonl"
    p.write_text(json.dumps(_record()) + "\n" + "{not json\n" + "[1,2,3]\n")
    records, malformed = read_records(p)
    assert len(records) == 1 and malformed == 2


def test_append_refuses_to_follow_a_symlink(tmp_path):
    """ENG-5: O_NOFOLLOW — a symlink planted at the ledger path must not redirect
    the append into the target file. The write fails best-effort (False, no raise)
    and the victim file is left untouched."""
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    link = tmp_path / "ledger.jsonl"
    link.symlink_to(victim)
    assert append_run(_record(), path=link) is False
    assert victim.read_text() == "untouched"


def test_health_reports_counts(tmp_path):
    p = tmp_path / "ledger.jsonl"
    append_run(_record(), path=p)
    h = ledger_health(p)
    assert h["exists"] and h["records"] == 1 and h["writable"]


def test_health_missing_file(tmp_path):
    h = ledger_health(tmp_path / "nope.jsonl")
    assert h["exists"] is False and h["records"] == 0
