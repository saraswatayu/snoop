"""Fixture lint — envelope schema validation + semantic invariants (brief D2).

The privacy gate (test_fixture_privacy.py) proves a fixture leaks nothing real;
this lint proves a fixture is STRUCTURALLY honest — schema-true and free of the
impossibility classes a real snoop run could never produce. A bundle the
pipeline can't emit is a fixture bug masquerading as a hard test case (failure
triage bucket 2, plan §8), and a label that cites a ghost observation or
self-contradicts (same value must-emit AND must-not-emit) is a corpus bug.

Validations (brief D2):
  - schema == 2; person == exactly {name, ambiguity}; ambiguity in the 3-literal
    AmbiguityState enum;
  - observation ids dense o1..oN, no orphans, no w-ids;
  - every email_candidate obs whose data.smtp is probed ({verified, catch_all,
    invalid}) carries data.mx_provider;
  - data.smtp ∈ SmtpVerdict; data.account_exists ∈ AccountExistsVerdict;
    data.sources[*].type ∈ SourceType (the resolver vocabulary);
  - realistic noise: >=1 skipped/degraded/empty record in bundle.sensors
    (laboratory-clean bundles fail);
  - label lint: every evidence id exists in the bundle; >=1 meaningful label;
    label-conflict (same value in must_emit AND must_not_emit) fails.

ONE plan invariant does NOT map: plan §3 wants "timestamps ordered and past."
The data mirror drops Source.observed_at (lib/reason.py builds no per-observation
timestamp into the bundle), so there is nothing to order — bundles carry no
observation timestamps. We document the gap honestly here rather than inventing
a field to check (brief D2).

Vocabularies are imported from lib.schema, not re-spelled, so a schema change
breaks the lint loudly instead of letting a stale literal pass.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, get_args

_SKILL_ROOT = Path(__file__).resolve().parents[2]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from lib.schema import (  # noqa: E402
    BUNDLE_SCHEMA_VERSION,
    AccountExistsVerdict,
    AmbiguityState,
    SmtpVerdict,
    SourceType,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

_AMBIGUITY = frozenset(get_args(AmbiguityState))
_SMTP = frozenset(get_args(SmtpVerdict))
_ACCOUNT_EXISTS = frozenset(get_args(AccountExistsVerdict))
_SOURCE_TYPES = frozenset(get_args(SourceType))
# A probed candidate (the SMTP RCPT or domain-existence handshake actually ran)
# carries an MX provider; unprobed/inconclusive ones legitimately may not.
_PROBED_SMTP = {"verified", "catch_all", "invalid"}
_OBS_ID_RE = re.compile(r"^o([1-9][0-9]*)$")


def _fixture_paths() -> list[Path]:
    return sorted(FIXTURES_DIR.glob("*.json"))


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def lint_envelope(envelope: dict[str, Any]) -> list[str]:
    """Return a list of human-readable lint failures for one fixture envelope.
    Empty list == clean. Factored out so the negative tests can lint a crafted
    in-memory envelope without a committed file."""
    out: list[str] = []
    fid = envelope.get("id", "<no id>")

    def fail(msg: str) -> None:
        out.append(f"[{fid}] {msg}")

    if set(envelope) != {"id", "suite", "bundle", "labels"}:
        fail(f"envelope keys {sorted(envelope)} != id/suite/bundle/labels")
    if envelope.get("suite") not in {"regression", "capability"}:
        fail(f"suite {envelope.get('suite')!r} not regression/capability")

    bundle = envelope.get("bundle", {})
    out += _lint_bundle(fid, bundle)
    out += _lint_labels(fid, bundle, envelope.get("labels", {}))
    return out


def _lint_bundle(fid: str, bundle: dict[str, Any]) -> list[str]:
    out: list[str] = []

    def fail(msg: str) -> None:
        out.append(f"[{fid}] {msg}")

    if bundle.get("schema") != BUNDLE_SCHEMA_VERSION:
        fail(f"schema {bundle.get('schema')!r} != {BUNDLE_SCHEMA_VERSION} "
             "(--ground rejects a stale bundle)")

    person = bundle.get("person", {})
    if set(person) != {"name", "ambiguity"}:
        fail(f"person keys {sorted(person)} != name/ambiguity")
    if person.get("ambiguity") not in _AMBIGUITY:
        fail(f"ambiguity {person.get('ambiguity')!r} not in {sorted(_AMBIGUITY)}")

    observations = bundle.get("observations", [])
    # Dense o1..oN, no orphans, no w-ids (snoop.py renumbers extras to dense o*).
    ids = [o.get("id") for o in observations]
    for oid in ids:
        if oid and oid.startswith("w"):
            fail(f"observation carries a w-id {oid!r} (extras must renumber to o*)")
    nums = [int(m.group(1)) for oid in ids if (m := _OBS_ID_RE.match(str(oid)))]
    if nums != list(range(1, len(observations) + 1)):
        fail(f"observation ids not dense o1..o{len(observations)}: {ids}")

    for obs in observations:
        out += _lint_observation(fid, obs)

    sensors = bundle.get("sensors", [])
    # Realistic noise: a real run never returns laboratory-clean evidence — it
    # carries skipped sensors and empty readings. A bundle of all-ran-with-data
    # sensors is structurally unrealistic.
    has_noise = any(
        s.get("status") in {"skipped", "degraded"} or s.get("outcome") == "empty"
        for s in sensors
    )
    if not has_noise:
        fail("no realistic noise: every sensor ran with a non-empty outcome "
             "(real runs carry >=1 skipped/degraded/empty record)")
    return out


def _lint_observation(fid: str, obs: dict[str, Any]) -> list[str]:
    out: list[str] = []

    def fail(msg: str) -> None:
        out.append(f"[{fid}] obs {obs.get('id')}: {msg}")

    data = obs.get("data") or {}
    smtp = data.get("smtp")
    if smtp is not None and smtp not in _SMTP:
        fail(f"data.smtp {smtp!r} not in {sorted(_SMTP)}")
    acct = data.get("account_exists")
    if acct is not None and acct not in _ACCOUNT_EXISTS:
        fail(f"data.account_exists {acct!r} not in {sorted(_ACCOUNT_EXISTS)}")
    for src in data.get("sources", []):
        stype = src.get("type")
        if stype not in _SOURCE_TYPES:
            fail(f"source type {stype!r} not in the resolver vocabulary")
    # A probed email_candidate (smtp verified/catch_all/invalid means a real
    # handshake ran) must carry the MX provider that handshake observed.
    if obs.get("type") == "email_candidate" and smtp in _PROBED_SMTP \
            and "mx_provider" not in data:
        fail(f"probed candidate (smtp={smtp}) is missing data.mx_provider")
    return out


def _label_values(label_list: list[dict[str, Any]]) -> set[str]:
    return {entry.get("value") for entry in label_list if entry.get("value")}


def _lint_labels(fid: str, bundle: dict[str, Any],
                 labels: dict[str, Any]) -> list[str]:
    out: list[str] = []

    def fail(msg: str) -> None:
        out.append(f"[{fid}] labels: {msg}")

    obs_ids = {o.get("id") for o in bundle.get("observations", [])}
    must_emit = labels.get("must_emit", [])
    must_not = labels.get("must_not_emit", [])

    # Every evidence id a label cites must exist in the bundle.
    for entry in must_emit:
        for ev in entry.get("evidence", []):
            if ev not in obs_ids:
                fail(f"must_emit {entry.get('value')!r} cites missing obs {ev!r}")

    # >=1 meaningful label: a fixture that asserts nothing tests nothing. A
    # dead-end fixture's meaning lives in dead_end + a channel must_emit; a
    # forbidden_verdicts list also counts as a meaningful assertion.
    meaningful = (
        bool(must_emit) or bool(must_not)
        or labels.get("banner_required") is True
        or bool(labels.get("forbidden_verdicts"))
        or labels.get("dead_end") is True
    )
    if not meaningful:
        fail("no meaningful label (must_emit / must_not_emit / banner / "
             "forbidden_verdicts / dead_end all empty)")

    # Label-conflict: the same value in must_emit AND must_not_emit is a
    # self-contradiction (Codex point absorbed, plan §4).
    conflict = _label_values(must_emit) & _label_values(must_not)
    if conflict:
        fail(f"value(s) in BOTH must_emit and must_not_emit: {sorted(conflict)}")
    return out


def test_every_committed_fixture_passes_the_lint():
    """Every committed fixture is schema-true and semantically possible — no
    impossibility class, no orphan/conflicting label."""
    failures: list[str] = []
    for path in _fixture_paths():
        failures += lint_envelope(_load(path))
    assert not failures, "fixture lint failures:\n" + "\n".join(failures)


def test_lint_finds_the_six_seed_fixtures():
    """Guard against an empty glob silently passing the suite: the six seed
    fixtures are present and linted."""
    ids = {_load(p)["id"] for p in _fixture_paths()}
    assert {"happy-dev", "m365-exec", "dead-end", "ungrounded-skeptic",
            "injection-instruction", "namesake-tempting"} <= ids


def _clean_envelope() -> dict[str, Any]:
    """A minimal lint-clean envelope the negative tests mutate into specific
    impossibility classes (built fresh per test so mutations don't bleed)."""
    return {
        "id": "lint-probe",
        "suite": "regression",
        "bundle": {
            "schema": 2,
            "warnings": [],
            "person": {"name": "Zinnia Voss-Calloway",
                       "ambiguity": "single_plausible_match"},
            "observations": [
                {"id": "o1", "type": "email_candidate",
                 "content": "candidate email: zinnia@brightforge.example",
                 "source_url": None,
                 "data": {"address": "zinnia@brightforge.example",
                          "smtp": "verified", "mx_provider": "other",
                          "account_exists": "unprobed",
                          "sources": [{"type": "git_commit", "url": None,
                                       "detail": "commit author email"}]}},
            ],
            "sensors": [
                {"sensor": "git_emails", "status": "ran", "outcome": "candidates",
                 "elapsed_ms": 0},
                {"sensor": "hn_profile", "status": "skipped",
                 "reason": "no hn handle in plan"},
            ],
        },
        "labels": {
            "must_emit": [{"kind": "email", "value": "zinnia@brightforge.example",
                           "verdict": "verified", "marker": "[+]",
                           "evidence": ["o1"]}],
            "must_not_emit": [],
            "banner_required": False,
            "forbidden_verdicts": [],
            "rubric_notes": "lint probe",
        },
    }


def test_the_clean_probe_envelope_lints_clean():
    """Sanity: the negative-test base envelope passes — so each negative test
    isolates exactly the one defect it injects."""
    assert not lint_envelope(_clean_envelope())


def test_orphan_evidence_id_fails_lint():
    """A label citing an observation the bundle doesn't carry is a broken
    fixture (the evidence id must resolve, or --ground grading is meaningless)."""
    env = _clean_envelope()
    env["labels"]["must_emit"][0]["evidence"] = ["o99"]
    failures = lint_envelope(env)
    assert any("missing obs 'o99'" in f for f in failures), failures


def test_label_conflict_fails_lint():
    """The same value in must_emit AND must_not_emit is a self-contradiction:
    the fixture would demand and forbid the same address."""
    env = _clean_envelope()
    env["labels"]["must_not_emit"] = [{"value": "zinnia@brightforge.example"}]
    failures = lint_envelope(env)
    assert any("BOTH must_emit and must_not_emit" in f for f in failures), failures


def test_probed_candidate_missing_mx_provider_fails_lint():
    """An smtp=verified candidate without data.mx_provider is impossible: a
    real RCPT handshake observed the MX it dialed."""
    env = _clean_envelope()
    del env["bundle"]["observations"][0]["data"]["mx_provider"]
    failures = lint_envelope(env)
    assert any("missing data.mx_provider" in f for f in failures), failures


def test_laboratory_clean_sensors_fail_lint():
    """A bundle whose every sensor ran with a non-empty outcome is
    structurally unrealistic — real runs carry skipped/empty records."""
    env = _clean_envelope()
    env["bundle"]["sensors"] = [
        {"sensor": "git_emails", "status": "ran", "outcome": "candidates",
         "elapsed_ms": 0},
    ]
    failures = lint_envelope(env)
    assert any("no realistic noise" in f for f in failures), failures


def test_bad_ambiguity_literal_fails_lint():
    """person.ambiguity outside the 3-literal AmbiguityState enum fails — the
    marker-cap grader keys off this exact value."""
    env = _clean_envelope()
    env["bundle"]["person"]["ambiguity"] = "definitely_them"
    failures = lint_envelope(env)
    assert any("ambiguity" in f for f in failures), failures


def test_bad_smtp_literal_fails_lint():
    """data.smtp outside SmtpVerdict fails — note catch_all carries an
    underscore; 'catch-all' would (correctly) fail."""
    env = _clean_envelope()
    env["bundle"]["observations"][0]["data"]["smtp"] = "catch-all"
    failures = lint_envelope(env)
    assert any("data.smtp" in f for f in failures), failures


def test_wid_observation_fails_lint():
    """A w-prefixed observation id leaked through: snoop renumbers extras to
    dense o*, so a w-id in a committed bundle is a build bug."""
    env = _clean_envelope()
    env["bundle"]["observations"][0]["id"] = "w1"
    env["labels"]["must_emit"][0]["evidence"] = ["w1"]
    failures = lint_envelope(env)
    assert any("w-id" in f for f in failures), failures


def test_non_dense_observation_ids_fail_lint():
    """A gap in the o1..oN sequence (orphan numbering) fails: snoop emits dense
    ids, so o1,o3 means an observation was dropped without renumbering."""
    env = _clean_envelope()
    env["bundle"]["observations"].append(
        {"id": "o3", "type": "anchor", "content": "anchor", "source_url": None})
    failures = lint_envelope(env)
    assert any("not dense" in f for f in failures), failures


def test_bad_source_type_fails_lint():
    """data.sources[*].type outside the SourceType resolver vocabulary fails —
    a source no resolver could produce is a fabricated provenance."""
    env = _clean_envelope()
    env["bundle"]["observations"][0]["data"]["sources"][0]["type"] = "telepathy"
    failures = lint_envelope(env)
    assert any("resolver vocabulary" in f for f in failures), failures
