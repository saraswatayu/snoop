"""Grader↔SKILL.md drift lint (plan §5, brief D5).

Every G1 grader carries a RULE_QUOTE — the verbatim SKILL.md sentence(s) it
enforces. This default-suite lint introspects graders.py for EVERY `*RULE_QUOTE`
module constant (never a hand-maintained list — a new grader's quote is picked up
automatically) and asserts each still appears in SKILL.md. Editing a graded
rule's prose then loudly breaks its grader's drift test before any eval token is
spent — the `--ground` byte-check trick turned on the graders themselves (eng
review issue 2; the same drift class test_acceptance.py kills for commands).

Both sides are whitespace-normalized with `" ".join(text.split())` (the house
pattern, test_calibration.py) so a quote that wraps across lines with markdown
emphasis still matches the single-line normalization of SKILL.md.
"""

from __future__ import annotations

from pathlib import Path

from . import graders

_SKILL_MD = Path(__file__).resolve().parents[2] / "SKILL.md"


def _normalize(text: str) -> str:
    """Collapse all runs of whitespace to single spaces (house pattern)."""
    return " ".join(text.split())


def _rule_quotes() -> dict[str, str]:
    """Every module-level constant in graders.py whose name ends in RULE_QUOTE,
    keyed by name. Introspected, so adding a grader+quote needs no edit here."""
    return {
        name: value
        for name, value in vars(graders).items()
        if name.endswith("RULE_QUOTE") and isinstance(value, str)
    }


def test_grader_module_exposes_rule_quotes():
    """Guard against an empty introspection silently passing: graders.py exposes
    at least the four G1 quotes (citation, verdict, marker-caps, structure)."""
    quotes = _rule_quotes()
    assert len(quotes) >= 4, f"expected >=4 RULE_QUOTE constants, got {sorted(quotes)}"


def test_every_rule_quote_still_appears_in_skill_md():
    """Each grader's RULE_QUOTE is a verbatim (whitespace-normalized) substring of
    the current SKILL.md. A prose edit that drops or rewords a graded sentence
    fails HERE, naming the stale constant — the eval doing its own §8 triage
    bucket 1 ('does the drift lint flag a stale RULE_QUOTE?')."""
    skill = _normalize(_SKILL_MD.read_text())
    stale: list[str] = []
    for name, quote in _rule_quotes().items():
        if _normalize(quote) not in skill:
            stale.append(name)
    assert not stale, (
        "RULE_QUOTE constants no longer found in SKILL.md (prose drifted from a "
        f"graded rule): {stale}")
