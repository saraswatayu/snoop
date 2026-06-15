"""Invariants for the discovery-sensor registry (lib/sensors.py).

The registry is the single source of truth for which discovery sensors run and
why one is skipped. These tests are the safety net that keeps it honest — most
importantly, that every spec's source_type is a real schema.SourceType, so the
registry can't drift from the bundle's type contract.
"""

from __future__ import annotations

from typing import get_args

from lib.schema import Person, SourceType
from lib.sensors import DISCOVERY_SENSORS, SensorContext

_SOURCE_TYPES = set(get_args(SourceType))


def test_every_spec_source_type_is_a_real_source_type():
    for spec in DISCOVERY_SENSORS:
        assert spec.source_type in _SOURCE_TYPES, spec.name


def test_spec_names_are_unique():
    names = [s.name for s in DISCOVERY_SENSORS]
    assert len(names) == len(set(names))


def test_pattern_gen_is_the_unconditional_fallback():
    [pg] = [s for s in DISCOVERY_SENSORS if s.name == "pattern_gen"]
    empty = SensorContext(person=Person(name="X"), packages=[], gh_handle=None)
    assert pg.gate(empty) is True
    assert pg.skip_reason == ""


def test_empty_context_gates_in_only_pattern_gen():
    ctx = SensorContext(person=Person(name="X"), packages=[], gh_handle=None)
    running = {s.name for s in DISCOVERY_SENSORS if s.gate(ctx)}
    assert running == {"pattern_gen"}


def test_full_context_gates_in_every_sensor():
    p = Person(name="X", handles={"github": "h", "hn": "h"},
               personal_domains=["x.com"])
    ctx = SensorContext(person=p, packages=[{"registry": "pypi", "name": "f"}],
                        gh_handle="h")
    running = {s.name for s in DISCOVERY_SENSORS if s.gate(ctx)}
    assert running == {s.name for s in DISCOVERY_SENSORS}
