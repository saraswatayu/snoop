"""tests/evals/_pipeline_mock.py infra tests — the eval system judges Claude;
these judge the eval system's wiring.

The shared mock has two consumers with different lifecycles: the acceptance
test (pytest `monkeypatch` fixture) and make_fixture.py (a script that
instantiates `pytest.MonkeyPatch()` directly, outside any fixture). These tests
pin the script path explicitly, prove the wiring is self-sufficient without
conftest's autouse network stubs, and prove a non-default spec actually steers
the produced bundle.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SKILL_ROOT = Path(__file__).resolve().parents[2]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

import snoop  # noqa: E402
from lib.schema import EmailCandidate, Employer, Person, ResolverResult, Source  # noqa: E402
from tests.evals._pipeline_mock import (  # noqa: E402
    NOW,
    PipelineSpec,
    ProbeOutcome,
    documented_spec,
    wire_pipeline,
)


def _fictional_spec(smtp_verdict: str = "verified") -> PipelineSpec:
    """A fully fictional analogue of the documented spec (reserved domain,
    roster-style handle) with a parameterizable SMTP verdict."""
    return PipelineSpec(
        person=Person(
            name="Avery Example", handles={"github": "snoop-fixture-avery"},
            employer=Employer(name="Acme", domains=["acme.example"]),
            ambiguity="single_plausible_match",
            bound_anchors=[("github_name_match", "Avery Example"),
                           ("github_handle_exists", "snoop-fixture-avery")],
        ),
        git_emails=ResolverResult(
            resolver="git_emails",
            candidates=[EmailCandidate(
                address="avery@acme.example", employer_match=True,
                sources=[Source(type="git_commit",
                                url="https://github.com/snoop-fixture-avery",
                                observed_at=NOW, detail="commit author email")])],
            status="ok"),
        probes={"avery@acme.example": ProbeOutcome(smtp_verdict=smtp_verdict,
                                                   mx_provider="other")},
    )


def _email_obs(bundle: dict, address: str) -> dict:
    return next(o for o in bundle["observations"]
                if o["type"] == "email_candidate" and address in o["content"])


def test_wire_pipeline_works_with_a_directly_instantiated_monkeypatch(tmp_path):
    """The make_fixture.py path: wire_pipeline accepts a pytest.MonkeyPatch()
    instantiated outside any fixture, and the real CLI then produces the
    documented bundle (steipete, SMTP-verified on the employer domain)."""
    mp = pytest.MonkeyPatch()
    try:
        wire_pipeline(mp, documented_spec())
        out = tmp_path / "bundle.json"
        rc = snoop.main(["Peter Steinberger", "--no-pgp", "--out", str(out)])
        assert rc == 0
        bundle = json.loads(out.read_text())
        assert _email_obs(bundle, "pete@openai.com")["data"]["smtp"] == "verified"
    finally:
        mp.undo()


def test_wire_pipeline_is_self_sufficient_without_the_conftest_stubs(tmp_path, monkeypatch):
    """wire_pipeline must stub pgp/rel_me/ledger ITSELF: make_fixture.py runs
    outside pytest, where conftest's autouse network fixture doesn't exist.
    Booby-trap those three seams, wire over them, and run the full pipeline
    with pgp + rel=me + ledger all armed — none of the traps may fire."""
    def boom(*a, **kw):
        raise AssertionError("unstubbed network/ledger seam was called")
    monkeypatch.setattr(snoop, "fetch_pgp_emails", boom)
    monkeypatch.setattr(snoop, "verify_rel_me", boom)
    monkeypatch.setattr(snoop, "append_run", boom)

    mp = pytest.MonkeyPatch()
    try:
        wire_pipeline(mp, documented_spec())
        out = tmp_path / "bundle.json"
        # No --no-pgp / --no-ledger, and the spec has a personal domain, so all
        # three booby-trapped paths are reached unless wire_pipeline re-stubbed.
        rc = snoop.main(["Peter Steinberger", "--out", str(out)])
        assert rc == 0
        assert out.exists()
    finally:
        mp.undo()


def test_a_non_default_spec_steers_the_bundle(tmp_path, monkeypatch):
    """A spec with a different per-address probe outcome shows up in the
    produced bundle: catch_all in the spec, catch_all in data.smtp."""
    wire_pipeline(monkeypatch, _fictional_spec(smtp_verdict="catch_all"))
    out = tmp_path / "bundle.json"
    rc = snoop.main(["Avery Example", "--out", str(out)])
    assert rc == 0
    bundle = json.loads(out.read_text())
    assert _email_obs(bundle, "avery@acme.example")["data"]["smtp"] == "catch_all"


def test_hn_profile_patch_point_serves_the_canned_reading(tmp_path, monkeypatch):
    """A spec person with an `hn` handle reaches the canned hn_profile result
    (a patch point the old acceptance helper never covered) — no HTTP call to
    news.ycombinator.com."""
    def boom(*a, **kw):
        raise AssertionError("real fetch_hn_profile was called")
    monkeypatch.setattr(snoop, "fetch_hn_profile", boom)

    spec = PipelineSpec(
        person=Person(name="Casey Example", handles={"hn": "snoop-fixture-casey"},
                      ambiguity="insufficient_identity_evidence"),
        hn_profile=ResolverResult(
            resolver="hn_profile",
            candidates=[EmailCandidate(
                address="casey@example.org",
                sources=[Source(type="hn_profile",
                                url="https://news.ycombinator.com/user?id=snoop-fixture-casey",
                                observed_at=NOW, detail="public email on HN profile")])],
            status="ok"),
    )
    wire_pipeline(monkeypatch, spec)
    out = tmp_path / "bundle.json"
    rc = snoop.main(["Casey Example", "--out", str(out)])
    assert rc == 0
    bundle = json.loads(out.read_text())
    assert _email_obs(bundle, "casey@example.org")


def test_package_emails_patch_point_serves_the_canned_reading(tmp_path, monkeypatch):
    """A plan with `packages` reaches the canned package_registry result (the
    other patch point the old acceptance helper never covered) — no registry
    HTTP call."""
    def boom(*a, **kw):
        raise AssertionError("real fetch_package_emails was called")
    monkeypatch.setattr(snoop, "fetch_package_emails", boom)

    spec = PipelineSpec(
        person=Person(name="Robin Example",
                      ambiguity="insufficient_identity_evidence"),
        package_emails=ResolverResult(
            resolver="package_registry",
            candidates=[EmailCandidate(
                address="robin@example.com",
                sources=[Source(type="package_registry",
                                url="https://www.npmjs.com/package/snoop-fixture-pkg",
                                observed_at=NOW, detail="npm publisher email")])],
            status="ok"),
    )
    wire_pipeline(monkeypatch, spec)
    plan = json.dumps({"name": "Robin Example",
                       "packages": [{"registry": "npm", "name": "snoop-fixture-pkg"}]})
    out = tmp_path / "bundle.json"
    rc = snoop.main(["Robin Example", "--person-plan", plan, "--out", str(out)])
    assert rc == 0
    bundle = json.loads(out.read_text())
    assert _email_obs(bundle, "robin@example.com")


def test_google_hosted_domains_arm_the_existence_check_hermetically(tmp_path, monkeypatch):
    """A spec with google_ready + google_hosted_domains drives the Google
    existence check to its mocked verdict with NO live MX lookup. The real
    _autodetect_workspace_domains binds is_google_hosted as a default parameter
    (so patching the name is useless) and would hit the network on .example
    domains — booby-trap it to prove the mock stubs the autodetect itself."""
    def boom(domain, *a, **kw):
        raise AssertionError("live MX lookup (is_google_hosted) was called")
    monkeypatch.setattr(snoop, "is_google_hosted", boom)

    spec = PipelineSpec(
        person=Person(
            name="Avery Example", handles={"github": "snoop-fixture-avery"},
            employer=Employer(name="Acme", domains=["acme.example"]),
            ambiguity="single_plausible_match",
            bound_anchors=[("github_name_match", "Avery Example"),
                           ("github_handle_exists", "snoop-fixture-avery")],
        ),
        git_emails=ResolverResult(
            resolver="git_emails", status="ok",
            candidates=[EmailCandidate(
                address="avery@acme.example", employer_match=True,
                gaia_id="GAIA-AVERY-0001",
                sources=[Source(type="git_commit",
                                url="https://github.com/snoop-fixture-avery",
                                observed_at=NOW, detail="commit author email")])]),
        google_ready=True,
        google_hosted_domains={"acme.example"},
        probes={"avery@acme.example": ProbeOutcome(
            smtp_verdict="verified", mx_provider="google",
            account_exists="verified", account_display_name="Avery Example")},
    )
    wire_pipeline(monkeypatch, spec)
    out = tmp_path / "bundle.json"
    rc = snoop.main(["Avery Example", "--allow-google-account", "--out", str(out)])
    assert rc == 0
    bundle = json.loads(out.read_text())
    assert _email_obs(bundle, "avery@acme.example")["data"]["account_exists"] == "verified"


def test_google_burst_stays_disarmed_when_no_hosted_domains(tmp_path, monkeypatch):
    """The default empty google_hosted_domains classifies every domain as
    non-Workspace, so the Google burst never arms even with
    --allow-google-account — the safety that keeps non-Google fixtures off the
    network. account_exists stays unprobed."""
    def boom(domain, *a, **kw):
        raise AssertionError("live MX lookup (is_google_hosted) was called")
    monkeypatch.setattr(snoop, "is_google_hosted", boom)

    spec = _fictional_spec()  # no google_hosted_domains
    spec.google_ready = True
    wire_pipeline(monkeypatch, spec)
    out = tmp_path / "bundle.json"
    rc = snoop.main(["Avery Example", "--allow-google-account", "--out", str(out)])
    assert rc == 0
    bundle = json.loads(out.read_text())
    assert _email_obs(bundle, "avery@acme.example")["data"]["account_exists"] == "unprobed"


def test_bundle_is_byte_stable_across_reruns(tmp_path, monkeypatch):
    """Two runs of the same spec produce identical bundle bytes: elapsed_ms is
    normalized to 0, warnings are canned, and the per-call deep copies keep the
    first run's probe mutations from leaking into the second."""
    wire_pipeline(monkeypatch, _fictional_spec())
    out1, out2 = tmp_path / "b1.json", tmp_path / "b2.json"
    assert snoop.main(["Avery Example", "--out", str(out1)]) == 0
    assert snoop.main(["Avery Example", "--out", str(out2)]) == 0
    assert out1.read_text() == out2.read_text()

    bundle = json.loads(out1.read_text())
    assert bundle["warnings"] == []  # canned capability probe: no machine noise
    sensors = bundle.get("sensors", [])
    assert sensors, "expected per-sensor run records in the bundle"
    assert all(rec.get("elapsed_ms", 0) == 0 for rec in sensors)
