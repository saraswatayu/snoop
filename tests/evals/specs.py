"""Eval fixture spec registry — the source of truth for every committed fixture.

A committed fixture JSON is an ARTIFACT; the spec here (plus the shared sensor
wiring in _pipeline_mock.py) is its source. Never hand-author bundle JSON —
make_fixture.py drives the real `snoop.main()` against the spec so committed
bundles stay schema-true by construction and break loudly when the bundle
format evolves.

Authoring a spec:

- `pipeline` is a `_pipeline_mock.PipelineSpec` — the canned sensor world.
  Identities come from the fictional roster (tests/evals/personas.md): roster
  names, `snoop-fixture-*` handles, RFC 2606 domains ONLY (the privacy gate
  enforces this on the committed JSON).
- `argv` is the full `snoop.main()` argument list minus `--out` (name first,
  then whatever flags the scenario needs — `--person-plan`, `--no-pgp`,
  `--allow-google-account`, ...). The builder appends `--out` itself.
- `labels` follow the fixture envelope's label schema: `must_emit` entries
  carry kind/value/verdict/marker (+ optional `confidence_max` and an
  `evidence` list of observation ids), plus `must_not_emit`,
  `banner_required`, `forbidden_verdicts`, `rubric_notes`. Observation ids are
  assigned by the pipeline — build once, read the bundle, then pin the ids in
  `evidence`. Pick `must_emit` values that appear whole in a cited
  observation's `content` (the --ground byte-check matches the whole value
  first; token fallback is a trap).
- `variants` (optional) name deterministic perturbations for capability-suite
  robustness: each variant shuffles observation order (seeded by
  "<id>:<variant>", ids renumbered o1..oN, label `evidence` remapped to
  follow) and applies its rephrase map to observation `content` prose.
  Rephrase maps must never touch grounding anchors (addresses, URLs, handles)
  or `data` fields — only connective prose.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SKILL_ROOT = Path(__file__).resolve().parents[2]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from lib.schema import EmailCandidate, Employer, Person, ResolverResult, Source  # noqa: E402
from tests.evals._pipeline_mock import NOW, PipelineSpec, ProbeOutcome  # noqa: E402


@dataclass
class FixtureSpec:
    """Everything make_fixture.py needs to build one committed fixture."""
    id: str
    suite: str  # "regression" | "capability"
    pipeline: PipelineSpec
    argv: list[str]  # snoop.main argv minus --out (person name first)
    labels: dict[str, Any]
    # variant name -> rephrase map (old substring -> new substring) applied to
    # observation content prose; every variant also shuffles + renumbers.
    variants: dict[str, dict[str, str]] = field(default_factory=dict)


def _smoke_spec() -> FixtureSpec:
    """The trivial happy-path persona the infra tests exercise: Perrin
    Saltmarsh (roster), github-bound, git email on the employer domain (binds
    under ENG-8), SMTP 250. The simplest possible fixture — its job is proving
    the builder, not probing Claude. Task-3 specs add the real corpus."""
    return FixtureSpec(
        id="smoke",
        suite="regression",
        pipeline=PipelineSpec(
            person=Person(
                name="Perrin Saltmarsh",
                handles={"github": "snoop-fixture-perrin"},
                employer=Employer(name="Saltworks Instruments",
                                  domains=["saltworks.example"]),
                ambiguity="single_plausible_match",
                bound_anchors=[("github_name_match", "Perrin Saltmarsh"),
                               ("github_handle_exists", "snoop-fixture-perrin")],
            ),
            git_emails=ResolverResult(
                resolver="git_emails",
                candidates=[EmailCandidate(
                    address="perrin@saltworks.example", employer_match=True,
                    sources=[Source(type="git_commit",
                                    url="https://github.com/snoop-fixture-perrin",
                                    observed_at=NOW,
                                    detail="commit author email")])],
                status="ok"),
            probes={"perrin@saltworks.example": ProbeOutcome(
                smtp_verdict="verified", mx_provider="other")},
        ),
        argv=["Perrin Saltmarsh"],
        labels={
            "must_emit": [
                {"kind": "email", "value": "perrin@saltworks.example",
                 "verdict": "verified", "marker": "[+]",
                 "evidence": ["o5"]},  # the email_candidate observation
            ],
            "must_not_emit": [],
            "banner_required": False,
            "forbidden_verdicts": [],
            "rubric_notes": "Smoke persona: github-bound dev, git email on the "
                            "employer domain, SMTP 250. Lead with the email, "
                            "verified/[+], citing the email_candidate observation.",
        },
        variants={
            # Connective prose only — the address/handle anchors stay verbatim.
            "reworded": {"identity anchor validated": "validated identity anchor",
                         "declared current employer": "current employer (declared)"},
        },
    )


# The registry make_fixture.py builds from, keyed by fixture id.
SPECS: dict[str, FixtureSpec] = {s.id: s for s in [
    _smoke_spec(),
]}
