"""make_fixture.py infra tests — the eval system judges Claude; these judge
the fixture builder (deterministic, in the default CI suite).

Everything builds into tmp_path: committed fixtures are a deliberate
maintainer action (`python3 -m tests.evals.make_fixture <id>`), never a test
side effect, and the smoke fixture only exists to prove the builder.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SKILL_ROOT = Path(__file__).resolve().parents[2]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from tests.evals.make_fixture import build_fixture, main, write_fixture  # noqa: E402
from tests.evals.specs import SPECS  # noqa: E402

_ENVELOPE_KEYS = {"id", "suite", "bundle", "labels"}


def _evidence_ids(node) -> set[str]:
    """Collect every id referenced by an `evidence` list anywhere in labels."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, val in node.items():
            if key == "evidence" and isinstance(val, list):
                found.update(val)
            else:
                found |= _evidence_ids(val)
    elif isinstance(node, list):
        for item in node:
            found |= _evidence_ids(item)
    return found


def test_every_registered_spec_builds_a_well_formed_envelope():
    """Every spec in the registry builds the exact fixture envelope shape:
    {id, suite, bundle, labels}, suite from the two-suite vocabulary, and a
    schema-2 bundle with observations (built by the real pipeline, so it's
    schema-true by construction)."""
    for spec in SPECS.values():
        envelope = build_fixture(spec)
        assert set(envelope) == _ENVELOPE_KEYS
        assert envelope["id"] == spec.id
        assert envelope["suite"] in {"regression", "capability"}
        bundle = envelope["bundle"]
        assert bundle["schema"] == 2
        assert bundle["observations"], "a fixture bundle must carry observations"
        assert bundle["person"]["name"] == spec.pipeline.person.name


def test_rebuilds_are_byte_identical():
    """Two independent builds of the same spec write byte-identical files —
    observation ids, warnings, elapsed_ms, and key order are all pinned, so a
    committed fixture only changes when its spec (or the pipeline) does."""
    spec = SPECS["smoke"]
    first = json.dumps(build_fixture(spec), indent=2, sort_keys=True)
    second = json.dumps(build_fixture(spec), indent=2, sort_keys=True)
    assert first == second


def test_label_evidence_ids_exist_in_the_bundle():
    """Every `evidence`-referenced observation id in a spec's labels exists in
    the bundle that spec builds — a label citing a ghost observation is a
    broken fixture."""
    for spec in SPECS.values():
        envelope = build_fixture(spec)
        obs_ids = {o["id"] for o in envelope["bundle"]["observations"]}
        cited = _evidence_ids(envelope["labels"])
        assert cited, f"{spec.id}: labels should pin at least one evidence id"
        assert cited <= obs_ids, f"{spec.id}: evidence ids {cited - obs_ids} missing"


def test_must_emit_values_appear_whole_in_a_cited_observation():
    """Each must_emit value appears verbatim in the content of one of its
    cited observations — the discipline that keeps --ground's byte-check on
    the whole-value path (the longest-token fallback is a trap)."""
    for spec in SPECS.values():
        envelope = build_fixture(spec)
        by_id = {o["id"]: o for o in envelope["bundle"]["observations"]}
        for entry in envelope["labels"].get("must_emit", []):
            cited = [by_id[i]["content"].lower() for i in entry.get("evidence", [])]
            assert any(entry["value"].lower() in c for c in cited), \
                f"{spec.id}: {entry['value']!r} not whole in any cited content"


def test_smoke_fixture_builds_via_the_cli_into_tmp_path(tmp_path, capsys):
    """The documented CLI shape (`python3 -m tests.evals.make_fixture smoke`)
    writes <id>.json with indent=2 and a trailing newline."""
    rc = main(["smoke", "--out-dir", str(tmp_path)])
    assert rc == 0
    path = tmp_path / "smoke.json"
    assert str(path) in capsys.readouterr().out
    raw = path.read_text()
    assert raw.endswith("}\n")
    assert json.loads(raw)["id"] == "smoke"


def test_cli_all_builds_every_spec_and_every_declared_variant(tmp_path):
    """--all writes one file per spec plus one per declared variant."""
    rc = main(["--all", "--out-dir", str(tmp_path)])
    assert rc == 0
    expected = {f"{s.id}.json" for s in SPECS.values()}
    expected |= {f"{s.id}--{v}.json" for s in SPECS.values() for v in s.variants}
    assert {p.name for p in tmp_path.glob("*.json")} == expected


def test_cli_rejects_an_unknown_fixture_id(tmp_path, capsys):
    """An id missing from the registry exits 2 with the known ids listed —
    no file written, no traceback."""
    rc = main(["no-such-fixture", "--out-dir", str(tmp_path)])
    assert rc == 2
    assert "smoke" in capsys.readouterr().err
    assert not list(tmp_path.glob("*.json"))


def test_variant_builds_are_byte_identical_across_reruns(tmp_path):
    """The variant perturbation is seeded by '<id>:<variant>': two builds of
    the same variant write byte-identical files."""
    spec = SPECS["smoke"]
    a = write_fixture(build_fixture(spec, variant="reworded"), tmp_path / "a")
    b = write_fixture(build_fixture(spec, variant="reworded"), tmp_path / "b")
    assert a.read_text() == b.read_text()


def test_variant_renumbers_ids_densely_and_remaps_label_evidence():
    """A variant's shuffled observations are renumbered o1..oN dense, and the
    label evidence follows the renumbering: the must_emit entry still cites
    the observation its value appears in."""
    spec = SPECS["smoke"]
    envelope = build_fixture(spec, variant="reworded")
    assert envelope["id"] == "smoke--reworded"
    observations = envelope["bundle"]["observations"]
    assert [o["id"] for o in observations] == \
        [f"o{i}" for i in range(1, len(observations) + 1)]
    by_id = {o["id"]: o for o in observations}
    entry = envelope["labels"]["must_emit"][0]
    cited = by_id[entry["evidence"][0]]
    assert entry["value"] in cited["content"]


def test_variant_rephrases_content_prose_but_not_anchors():
    """The variant's rephrase map rewrites connective prose in observation
    content while the grounding anchors (the address) survive verbatim."""
    envelope = build_fixture(SPECS["smoke"], variant="reworded")
    blob = "\n".join(o["content"] for o in envelope["bundle"]["observations"])
    assert "validated identity anchor" in blob   # rephrased
    assert "identity anchor validated" not in blob
    assert "perrin@saltworks.example" in blob    # anchor untouched


def test_an_undeclared_variant_fails_loudly():
    """Asking for a variant the spec doesn't declare raises (listing what IS
    declared) instead of silently building the base fixture."""
    with pytest.raises(ValueError, match="reworded"):
        build_fixture(SPECS["smoke"], variant="no-such-variant")
