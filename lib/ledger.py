"""lib/ledger.py — the local sensor-yield ledger (E1, offline-first).

One JSONL line per run recording **yield metadata only** — which sensors ran,
how long they took, the plan *shape* (booleans, not values), and the MX class —
so that over weeks of normal use we can answer "which sensor class wins for which
archetype?" *offline*, and (a separate, deliberate, evidence-cited step) retune
probe ordering. No auto-applied ordering ships here (that is phase 2, TODOS).

**It never stores target data.** No names, addresses, handles, domains, or URLs —
only the allowlisted keys below. The CI test in tests/test_ledger.py asserts the
schema by allowlist + value pattern, so a future edit that tries to log a target
field fails the build. Date-only timestamps (no time-of-day) further blunt
correlation.

Posture (user decision D14): on by default at `~/.snoop/ledger.jsonl` — outside
the repo, no git-accident path — written best-effort: a failure is a one-line
stderr warning, never a changed card or exit code (finding 1A). Appends are
single O_APPEND JSONL lines (atomic by construction, finding 4B); the reader
skips and counts malformed lines.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

LEDGER_VERSION = 1
DEFAULT_LEDGER_PATH = Path.home() / ".snoop" / "ledger.jsonl"
_MAX_LINE_BYTES = 4096

# 3C — the ONLY keys a ledger line may contain. The CI test enforces this; adding
# a target-bearing field (a name/address/handle/domain) is a build failure.
LEDGER_TOP_KEYS = frozenset({"v", "ts", "plan_shape", "mx_class", "candidates", "sensors"})
LEDGER_PLAN_SHAPE_KEYS = frozenset({"github", "hn", "personal_domains", "packages", "employer"})
LEDGER_SENSOR_KEYS = frozenset({"sensor", "status", "elapsed_ms", "outcome"})
_MX_CLASSES = frozenset({"google", "microsoft", "other", "none"})
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")            # date only, no time
_OUTCOME_RE = re.compile(r"^[a-z_]{1,24}$")            # short enum-like token
_SENSOR_RE = re.compile(r"^[a-z_]{1,32}$")


def build_record(
    *,
    plan_shape: dict[str, bool],
    mx_class: str,
    candidates: int,
    sensors: list[dict[str, Any]],
    today: str | None = None,
) -> dict[str, Any]:
    """Assemble a schema-conformant ledger line from yield metadata. `sensors`
    entries are filtered to the allowlisted RunRecord keys (dropping `reason`,
    which can carry error text); strings are bounded; nothing target-bearing is
    accepted."""
    shape = {k: bool(plan_shape.get(k, False)) for k in LEDGER_PLAN_SHAPE_KEYS}
    mx = mx_class if mx_class in _MX_CLASSES else "other"
    clean_sensors: list[dict[str, Any]] = []
    for s in sensors:
        sensor = str(s.get("sensor", ""))[:32]
        status = str(s.get("status", ""))
        outcome = str(s.get("outcome") or "")[:24]
        entry: dict[str, Any] = {"sensor": sensor, "status": status}
        if isinstance(s.get("elapsed_ms"), int):
            entry["elapsed_ms"] = s["elapsed_ms"]
        # Only persist an enum-like outcome; a richer free-text outcome (e.g.
        # "0 link(s)") is dropped so the ledger can't carry arbitrary text.
        if outcome and _OUTCOME_RE.match(outcome):
            entry["outcome"] = outcome
        clean_sensors.append(entry)
    return {
        "v": LEDGER_VERSION,
        "ts": today or date.today().isoformat(),
        "plan_shape": shape,
        "mx_class": mx,
        "candidates": int(candidates),
        "sensors": clean_sensors,
    }


def validate_record(rec: dict[str, Any]) -> list[str]:
    """Return a list of schema violations (empty = clean). Used by the CI test to
    prove the no-target-data contract — checks the key allowlist AND value shapes,
    so a stray name/address/domain can't ride along in an unexpected field."""
    errs: list[str] = []
    extra = set(rec) - LEDGER_TOP_KEYS
    if extra:
        errs.append(f"unexpected top-level keys: {sorted(extra)}")
    if rec.get("v") != LEDGER_VERSION:
        errs.append("bad version")
    if not isinstance(rec.get("ts"), str) or not _TS_RE.match(rec.get("ts", "")):
        errs.append("ts must be a bare YYYY-MM-DD date")
    shape = rec.get("plan_shape")
    if not isinstance(shape, dict) or set(shape) - LEDGER_PLAN_SHAPE_KEYS:
        errs.append("plan_shape has unexpected keys")
    elif any(not isinstance(v, bool) for v in shape.values()):
        errs.append("plan_shape values must be booleans")
    if rec.get("mx_class") not in _MX_CLASSES:
        errs.append("bad mx_class")
    if not isinstance(rec.get("candidates"), int):
        errs.append("candidates must be an int")
    sensors = rec.get("sensors")
    if not isinstance(sensors, list):
        errs.append("sensors must be a list")
    else:
        for s in sensors:
            if not isinstance(s, dict) or set(s) - LEDGER_SENSOR_KEYS:
                errs.append(f"sensor entry has unexpected keys: {s}")
                continue
            if not _SENSOR_RE.match(str(s.get("sensor", ""))):
                errs.append(f"sensor name not enum-like: {s.get('sensor')!r}")
            if s.get("status") not in ("ran", "skipped", "degraded"):
                errs.append(f"bad sensor status: {s.get('status')!r}")
            if "outcome" in s and not _OUTCOME_RE.match(str(s["outcome"])):
                errs.append(f"outcome not enum-like: {s.get('outcome')!r}")
            if "elapsed_ms" in s and not isinstance(s["elapsed_ms"], int):
                errs.append("elapsed_ms must be int")
    return errs


def append_run(rec: dict[str, Any], *, path: Path = DEFAULT_LEDGER_PATH) -> bool:
    """Append one JSONL line, best-effort. Returns True on success, False on any
    failure (caller logs a one-line stderr warning) — NEVER raises, never changes
    the card or exit code. The write is a single O_APPEND of a <4KB line, atomic
    by construction; oversize lines are dropped rather than truncated."""
    try:
        line = json.dumps(rec, separators=(",", ":"), default=str)
        blob = (line + "\n").encode("utf-8")
        if len(blob) > _MAX_LINE_BYTES:
            return False
        # ENG-5: the ledger dir is private (0700) and the open refuses to follow a
        # symlink at the final component (O_NOFOLLOW) — a planted symlink can't
        # redirect our append into someone else's file.
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(path), flags, 0o600)
        try:
            os.write(fd, blob)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def read_records(path: Path = DEFAULT_LEDGER_PATH) -> tuple[list[dict], int]:
    """Read the ledger, skipping (and counting) malformed lines. Returns
    (records, malformed_count). Missing file → ([], 0)."""
    records: list[dict] = []
    malformed = 0
    try:
        text = path.read_text()
    except OSError:
        return [], 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(obj, dict):
            records.append(obj)
        else:
            malformed += 1
    return records, malformed


def ledger_health(path: Path = DEFAULT_LEDGER_PATH) -> dict[str, Any]:
    """Summary for --diagnose: does it exist, is it writable, how many records,
    how many malformed lines."""
    exists = path.exists()
    records, malformed = read_records(path)
    writable = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        writable = os.access(path, os.W_OK) if exists else os.access(path.parent, os.W_OK)
    except OSError:
        writable = False
    return {
        "path": str(path),
        "exists": exists,
        "writable": writable,
        "records": len(records),
        "malformed": malformed,
    }
