"""Eval fixture builder — drives the REAL snoop pipeline against a spec.

NOT a pytest test (no test_ prefix; building fixtures is a maintainer action,
not a CI assertion). Run as:

    python3 -m tests.evals.make_fixture smoke
    python3 -m tests.evals.make_fixture smoke --variant reworded
    python3 -m tests.evals.make_fixture --all

For the requested spec(s) from tests/evals/specs.py it wires the shared sensor
mocks (_pipeline_mock.wire_pipeline via a directly instantiated
pytest.MonkeyPatch — no network, no ledger writes, byte-stable bundles), runs
`snoop.main([... , "--out", tmp])`, and wraps the produced bundle in the
fixture envelope:

    {"id": ..., "suite": ..., "bundle": {...}, "labels": {...}}

written to tests/evals/fixtures/<id>.json as sorted, indent=2 JSON with a
trailing newline — rebuilding an unchanged spec yields byte-identical files
(the determinism the infra tests pin).

Variants (capability-suite robustness, declared per spec): a variant build
deterministically shuffles observation order (seeded by "<id>:<variant>"),
renumbers ids densely o1..oN, remaps every label `evidence` list to follow,
and applies the variant's rephrase map to observation `content` prose. The
fixture id becomes "<id>--<variant>". `--all` builds every spec plus every
declared variant.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import random
import sys
import tempfile
from pathlib import Path
from typing import Any

_SKILL_ROOT = Path(__file__).resolve().parents[2]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

import pytest  # noqa: E402  (imported as a library for MonkeyPatch)

import snoop  # noqa: E402
from tests.evals._pipeline_mock import wire_pipeline  # noqa: E402
from tests.evals.specs import SPECS, FixtureSpec  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _run_pipeline(spec: FixtureSpec) -> dict[str, Any]:
    """Run the real `snoop.main()` Step-2 path under the spec's canned wiring
    and return the produced bundle dict."""
    mp = pytest.MonkeyPatch()
    chatter = io.StringIO()  # snoop.main narrates; keep the builder's own output clean
    try:
        wire_pipeline(mp, spec.pipeline)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "bundle.json"
            with contextlib.redirect_stdout(chatter), \
                    contextlib.redirect_stderr(chatter):
                rc = snoop.main([*spec.argv, "--out", str(out)])
            if rc != 0:
                raise RuntimeError(
                    f"snoop.main exited {rc} building {spec.id!r}:\n"
                    f"{chatter.getvalue()}")
            return json.loads(out.read_text())
    finally:
        mp.undo()


def _remap_evidence(node: Any, id_map: dict[str, str], fixture_id: str) -> Any:
    """Rewrite every label `evidence` list through the variant's id map.
    Unknown ids fail loudly — a label citing an observation the bundle doesn't
    carry is a broken fixture, not something to renumber around."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, val in node.items():
            if key == "evidence" and isinstance(val, list):
                missing = [i for i in val if i not in id_map]
                if missing:
                    raise ValueError(
                        f"fixture {fixture_id!r}: label evidence ids {missing} "
                        f"don't exist in the bundle")
                out[key] = [id_map[i] for i in val]
            else:
                out[key] = _remap_evidence(val, id_map, fixture_id)
        return out
    if isinstance(node, list):
        return [_remap_evidence(item, id_map, fixture_id) for item in node]
    return node


def _apply_variant(spec: FixtureSpec, bundle: dict[str, Any],
                   labels: dict[str, Any], variant: str
                   ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Deterministic perturbation: shuffle observations (seeded), renumber ids
    densely o1..oN in the new order, remap label evidence, and apply the
    variant's content rephrase map. Same inputs, same bytes — every time."""
    if variant not in spec.variants:
        raise ValueError(
            f"spec {spec.id!r} declares no variant {variant!r} "
            f"(declared: {sorted(spec.variants) or 'none'})")
    rephrase = spec.variants[variant]
    bundle = copy.deepcopy(bundle)

    observations = bundle.get("observations", [])
    random.Random(f"{spec.id}:{variant}").shuffle(observations)
    id_map = {obs["id"]: f"o{i}" for i, obs in enumerate(observations, start=1)}
    for obs in observations:
        obs["id"] = id_map[obs["id"]]
        content = obs.get("content", "")
        for old, new in rephrase.items():
            content = content.replace(old, new)
        obs["content"] = content

    return bundle, _remap_evidence(labels, id_map, f"{spec.id}--{variant}")


def build_fixture(spec: FixtureSpec, variant: str | None = None) -> dict[str, Any]:
    """Build the fixture envelope for a spec (optionally a declared variant)."""
    bundle = _run_pipeline(spec)
    labels = copy.deepcopy(spec.labels)
    fixture_id = spec.id
    if variant is not None:
        bundle, labels = _apply_variant(spec, bundle, labels, variant)
        fixture_id = f"{spec.id}--{variant}"
    return {"id": fixture_id, "suite": spec.suite,
            "bundle": bundle, "labels": labels}


def write_fixture(envelope: dict[str, Any],
                  out_dir: Path = FIXTURES_DIR) -> Path:
    """Write one envelope as <id>.json — sorted keys, indent=2, trailing
    newline, so rebuilds of an unchanged spec are byte-identical."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{envelope['id']}.json"
    path.write_text(json.dumps(envelope, indent=2, sort_keys=True) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m tests.evals.make_fixture",
        description="Build committed eval fixtures from the spec registry.")
    parser.add_argument("fixture_id", nargs="?",
                        help="spec id from tests/evals/specs.py")
    parser.add_argument("--all", action="store_true",
                        help="build every spec plus every declared variant")
    parser.add_argument("--variant", default=None,
                        help="build a declared variant of the single fixture id")
    parser.add_argument("--out-dir", type=Path, default=FIXTURES_DIR,
                        help=f"output directory (default: {FIXTURES_DIR})")
    args = parser.parse_args(argv)

    if args.all == bool(args.fixture_id):
        parser.error("pass exactly one of <fixture_id> or --all")
    if args.all and args.variant:
        parser.error("--variant needs a single <fixture_id>")

    if args.all:
        builds = [(spec, None) for spec in SPECS.values()]
        builds += [(spec, v) for spec in SPECS.values()
                   for v in sorted(spec.variants)]
    else:
        spec = SPECS.get(args.fixture_id)
        if spec is None:
            sys.stderr.write(
                f"unknown fixture id {args.fixture_id!r}; "
                f"known: {', '.join(sorted(SPECS))}\n")
            return 2
        builds = [(spec, args.variant)]

    for spec, variant in builds:
        path = write_fixture(build_fixture(spec, variant=variant), args.out_dir)
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
