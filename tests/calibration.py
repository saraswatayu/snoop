"""Calibration measurement harness.

Per Codex c3 + Subagent F3: the hit-rate estimates in SKILL.md should be
replaced with MEASURED numbers from a labeled fixture. This harness:

  1. Loads the public fixture `tests/fixtures/calibration_targets.json`.
  2. Merges any local overrides from `tests/fixtures/calibration_targets.local.json`
     (gitignored; users add private ground truths here).
  3. Runs the full pipeline against each target.
  4. Computes per-resolver hit rate and (when ground truth is provided)
     per-resolver precision.
  5. Renders a markdown report.

NOT a pytest test in the default suite — hits real network. Run
manually:

    python3 -m tests.calibration

Or directly:

    cd ~/.claude/skills/snoop && python3 tests/calibration.py

Output: stdout markdown report. The numbers from this should replace
the vibes-based "60-70% on developer targets" etc. in SKILL.md once
N ≥ 25 (the post-P1 calibration target from the plan).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

import snoop  # noqa: E402


_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "calibration_targets.json"
_LOCAL_OVERRIDES_PATH = (
    Path(__file__).parent / "fixtures" / "calibration_targets.local.json"
)


@dataclass
class ResolverMetrics:
    """Aggregated per-resolver metrics across N targets."""
    name: str
    targets_attempted: int = 0
    targets_with_candidate: int = 0      # resolver returned ≥1 candidate
    targets_with_known_match: int = 0    # resolver candidate matched ground truth
    targets_with_known: int = 0          # targets where ground truth is available
    statuses: dict[str, int] = field(default_factory=dict)

    @property
    def hit_rate(self) -> float:
        if self.targets_attempted == 0:
            return 0.0
        return self.targets_with_candidate / self.targets_attempted

    @property
    def precision_on_known(self) -> float | None:
        if self.targets_with_known == 0:
            return None
        return self.targets_with_known_match / self.targets_with_known


def load_fixture(path: Path = _FIXTURE_PATH,
                 overrides_path: Path = _LOCAL_OVERRIDES_PATH) -> list[dict[str, Any]]:
    """Load public fixture; merge local overrides if present.

    Override merge rule: a target in the overrides file with the same `id`
    REPLACES the public target (overrides win). New ids in overrides are
    appended. Useful pattern: keep the public fixture for structure
    (accent, Eastern-order tests) and add your private ground-truth
    targets locally.
    """
    public_data = json.loads(path.read_text())
    targets_by_id: dict[str, dict[str, Any]] = {
        t["id"]: t for t in public_data.get("targets", []) if isinstance(t, dict)
    }
    if overrides_path.exists():
        local_data = json.loads(overrides_path.read_text())
        for t in local_data.get("targets", []):
            if isinstance(t, dict) and t.get("id"):
                targets_by_id[t["id"]] = t
    return list(targets_by_id.values())


def measure_one_target(target: dict[str, Any]) -> dict[str, Any]:
    """Run the pipeline against one target. Return per-resolver results
    plus the final candidate addresses for inspection."""
    from lib.person_resolve import resolve_person  # noqa: E402

    name = target["name"]
    plan = target.get("person_plan") or {}
    person = resolve_person(name, plan=plan)
    results = snoop.run_pipeline(person)
    candidates = snoop.cluster_candidates(results)
    candidates.sort(key=snoop._probe_rank)

    return {
        "id": target.get("id"),
        "name": name,
        "ambiguity": person.ambiguity,
        "anchors_bound": len([
            a for a in person.bound_anchors if a[0] != "github_handle_exists"
        ]),
        "resolver_results": [
            {
                "resolver": r.resolver,
                "status": r.status,
                "candidate_count": len(r.candidates),
                "candidate_addresses": [c.address for c in r.candidates],
                "elapsed_ms": r.elapsed_ms,
                "error_detail": r.error_detail,
            }
            for r in results
        ],
        "final_candidates": [
            {
                "address": c.address,
                "smtp_verdict": c.smtp_verdict,
                "account_exists": c.account_exists,
                "sources": sorted({s.type for s in c.sources}),
            }
            for c in candidates
        ],
        "ground_truth": target.get("ground_truth", {}),
    }


def aggregate(
    target_results: list[dict[str, Any]],
) -> dict[str, ResolverMetrics]:
    metrics: dict[str, ResolverMetrics] = {}
    for tr in target_results:
        gt = tr.get("ground_truth") or {}
        known_addrs = set()
        for v in gt.values():
            if isinstance(v, str) and v:
                known_addrs.add(v.lower())
        for rr in tr.get("resolver_results", []):
            name = rr["resolver"]
            m = metrics.setdefault(name, ResolverMetrics(name=name))
            m.targets_attempted += 1
            m.statuses[rr["status"]] = m.statuses.get(rr["status"], 0) + 1
            if rr["candidate_count"] > 0:
                m.targets_with_candidate += 1
            if known_addrs:
                m.targets_with_known += 1
                resolver_addrs = {a.lower() for a in rr["candidate_addresses"]}
                if resolver_addrs & known_addrs:
                    m.targets_with_known_match += 1
    return metrics


def format_report(
    target_results: list[dict[str, Any]],
    metrics: dict[str, ResolverMetrics],
) -> str:
    lines = [
        "# snoop calibration report",
        "",
        f"Targets measured: **{len(target_results)}**",
        f"Targets with ground truth: "
        f"**{sum(1 for tr in target_results if any(tr.get('ground_truth', {}).values()))}**",
        "",
        "## Per-resolver metrics",
        "",
        "| Resolver | Hit rate | Precision (on known) | Status breakdown |",
        "| --- | ---: | ---: | --- |",
    ]
    for name in sorted(metrics):
        m = metrics[name]
        hit = f"{m.hit_rate:.0%}"
        prec = (
            f"{m.precision_on_known:.0%}"
            if m.precision_on_known is not None else "—"
        )
        statuses = ", ".join(
            f"{k}={v}" for k, v in sorted(m.statuses.items())
        )
        lines.append(
            f"| `{name}` | {hit} | {prec} | {statuses} |"
        )
    lines.extend([
        "",
        "## Per-target detail",
        "",
    ])
    for tr in target_results:
        lines.append(f"### {tr['name']} (`{tr['id']}`)")
        lines.append(
            f"- Identity: {tr['ambiguity']} ({tr['anchors_bound']} anchors)"
        )
        for rr in tr.get("resolver_results", []):
            addrs = ", ".join(rr["candidate_addresses"][:3]) or "—"
            elapsed = f"{rr['elapsed_ms']}ms" if rr["elapsed_ms"] else "?"
            extra = ""
            if rr["status"] != "ok" and rr.get("error_detail"):
                extra = f" ({rr['error_detail'][:80]})"
            lines.append(
                f"- `{rr['resolver']}`: status={rr['status']}, "
                f"{rr['candidate_count']} candidates, {elapsed}{extra}"
            )
            if addrs != "—":
                lines.append(f"  - addresses: {addrs}")
        if tr["final_candidates"]:
            top = tr["final_candidates"][0]
            lines.append(
                f"- **Top candidate**: `{top['address']}` "
                f"(smtp={top['smtp_verdict']}, "
                f"account_exists={top['account_exists']}, "
                f"sources={','.join(top['sources']) or 'none'})"
            )
        gt = tr.get("ground_truth", {})
        if any(gt.values()):
            lines.append(f"- Ground truth: {gt}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    targets = load_fixture()
    if not targets:
        print("# snoop calibration: no targets")
        print("Add targets to tests/fixtures/calibration_targets.json (or")
        print("tests/fixtures/calibration_targets.local.json for private targets).")
        return 1

    target_results = []
    for t in targets:
        try:
            target_results.append(measure_one_target(t))
        except Exception as e:  # noqa: BLE001
            target_results.append({
                "id": t.get("id"),
                "name": t.get("name"),
                "ambiguity": "error",
                "anchors_bound": 0,
                "resolver_results": [],
                "final_candidates": [],
                "ground_truth": t.get("ground_truth", {}),
                "error": f"{type(e).__name__}: {e}",
            })

    metrics = aggregate(target_results)
    print(format_report(target_results, metrics))
    return 0


if __name__ == "__main__":
    sys.exit(main())
