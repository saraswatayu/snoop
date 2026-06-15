"""The discovery-sensor registry — one table, not four hand-synced surfaces.

Adding a Phase-1 discovery sensor used to mean editing snoop.py in two places
that encode the SAME gate twice — once positively in run_pipeline ("if gh_handle:
run git_emails") and once negatively in _sensor_skips ("if not gh_handle: skip
git_emails") — plus the SourceType literal in schema.py. The gate drifting between
the run-list and the skip-list is the bug this prevents: declare it ONCE here.

What stays in snoop.py: the task *builder* that actually calls the sensor
function (e.g. `lambda: fetch_git_emails(gh_handle)`), keyed by name. Those calls
resolve the fetch_* names from the snoop module namespace, which the test suite
stubs via monkeypatch.setattr(snoop, "fetch_*", ...) — so the invocation has to
live there. The registry owns everything that isn't the call itself.

The Phase-2 enrichers (verify_smtp, google_account) and slow probes (rel_me, pgp)
are NOT here: they mutate prior candidates rather than returning a ResolverResult,
run under their own deadline harnesses, and are gated on different inputs. See
snoop._sensor_skips for their (bespoke, genuinely different) skip gating.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from lib.schema import Person, SourceType


@dataclass
class SensorContext:
    """The inputs every discovery-sensor gate reads. Built once per run from the
    resolved Person + plan, so a gate is a pure predicate over this struct."""
    person: Person
    packages: list
    gh_handle: str | None   # the BOUND github handle (None if it didn't bind)


@dataclass(frozen=True)
class SensorSpec:
    """One discovery sensor's registry entry. `gate` decides whether it runs this
    invocation; when it doesn't, `skip_reason` is the typed-degradation reason."""
    name: str
    source_type: SourceType            # validated ⊆ schema.SourceType by a test
    gate: Callable[[SensorContext], bool]
    skip_reason: str


# Order is the bundle/skip-record order. pattern_gen always runs — it's the
# explicit name×domain fallback when nothing else is gated in.
DISCOVERY_SENSORS: list[SensorSpec] = [
    SensorSpec("git_emails", "git_commit",
               gate=lambda c: bool(c.gh_handle),
               skip_reason="no bound github handle"),
    SensorSpec("gh_profile", "gh_profile",
               gate=lambda c: bool(c.gh_handle),
               skip_reason="no bound github handle"),
    SensorSpec("hn_profile", "hn_profile",
               gate=lambda c: bool(c.person.handles.get("hn")),
               skip_reason="no hn handle in plan"),
    SensorSpec("personal_site", "personal_site",
               gate=lambda c: bool(c.person.personal_domains),
               skip_reason="no personal_domains in plan"),
    SensorSpec("package_registry", "package_registry",
               gate=lambda c: bool(c.packages),
               skip_reason="no packages in plan"),
    SensorSpec("pattern_gen", "pattern",
               gate=lambda c: True,
               skip_reason=""),
]
