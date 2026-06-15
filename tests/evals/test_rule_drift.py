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

_SKILL_ROOT = Path(__file__).resolve().parents[2]
_SKILL_MD = _SKILL_ROOT / "SKILL.md"


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


def test_fact_kinds_match_production_render_enum():
    """The fact-kind vocabulary now has ONE home — lib.schema.FACT_KINDS — that
    the graders import and the renderer's section map must match. Pin the
    renderer's kinds to the schema source so adding a kind in one place without
    the other fails HERE, loudly, instead of g1_structure silently flagging the
    new kind as 'not in the enum'."""
    import sys
    if str(_SKILL_ROOT) not in sys.path:
        sys.path.insert(0, str(_SKILL_ROOT))
    from lib import render  # noqa: E402
    from lib.schema import FACT_KINDS  # noqa: E402

    assert graders._FACT_KINDS is FACT_KINDS  # graders read the schema source
    assert FACT_KINDS == set(render._KIND_SECTION), (
        "lib.render._KIND_SECTION drifted from lib.schema.FACT_KINDS "
        f"(schema-only: {FACT_KINDS - set(render._KIND_SECTION)}; "
        f"render-only: {set(render._KIND_SECTION) - FACT_KINDS})")


def test_rank_tables_match_schema_vocabulary():
    """g2_confidence_ordering ranks facts via hand-written dicts (_VERDICT_RANK /
    _MARKER_RANK) keyed by the verdict/marker vocabulary. Unlike _FACT_KINDS they
    do NOT read the schema source, so a renamed/added verdict or marker in
    lib.schema would leave a stale key here and the grader would mis-rank silently
    (`.get()` returns None → treated as the weakest). Pin the rank-table keys to
    the schema vocabulary so a vocabulary change fails HERE, loudly, instead."""
    import sys
    if str(_SKILL_ROOT) not in sys.path:
        sys.path.insert(0, str(_SKILL_ROOT))
    from lib.schema import BELONGS_MARKERS, EMAIL_VERDICTS  # noqa: E402

    assert set(graders._VERDICT_RANK) == EMAIL_VERDICTS, (
        "graders._VERDICT_RANK drifted from lib.schema.EMAIL_VERDICTS "
        f"(rank-only: {set(graders._VERDICT_RANK) - EMAIL_VERDICTS}; "
        f"schema-only: {EMAIL_VERDICTS - set(graders._VERDICT_RANK)})")
    assert set(graders._MARKER_RANK) == BELONGS_MARKERS, (
        "graders._MARKER_RANK drifted from lib.schema.BELONGS_MARKERS "
        f"(rank-only: {set(graders._MARKER_RANK) - BELONGS_MARKERS}; "
        f"schema-only: {BELONGS_MARKERS - set(graders._MARKER_RANK)})")
