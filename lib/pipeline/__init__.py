"""The snoop pipeline brain — the pure decision logic that sits between the
sensors (I/O) and the analyst (the host model).

Extracted from snoop.py so the highest-stakes, most snoop-specific logic — the
stranger-proofing — lives in named, directly unit-testable modules instead of as
private helpers in the CLI entry point:

  - `binding`     — does the right *person*, and the right *address*, bind?
  - `candidates`  — clustering, ranking, and name×domain template algebra.
  - `probes`      — which candidates are eligible for which Phase-2 probe.

These modules are PURE: no network, no stdin/stdout, no module-level sensor
calls. The fan-out and probe *harness* (run_pipeline, _probe_candidates, the
slow-probe threads) stays in snoop.py — those call the sensor functions through
the snoop module namespace, which the test suite stubs via
`monkeypatch.setattr(snoop, "fetch_*", ...)`. Moving them would silently break
that injection surface. See ARCHITECTURE.md ("Why the harness stays in snoop.py").
"""
