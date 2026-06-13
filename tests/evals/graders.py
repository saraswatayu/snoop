"""G1 deterministic graders (plan §5, brief D3/D4/D5).

G1 is the cheapest grader layer: no LLM, no token cost, pure code over the
fixture bundle + Claude's pre-ground output. The unusual gift here is how much of
SKILL.md Step 3 is checkable against bundle FIELDS:

  - g1_citation (HARD): does every must_emit value survive the REAL `--ground`
    byte-check with verified == true? A dropped or unverified must_emit is the
    misattribution axis failing — no partial credit.
  - g1_verdict_vocabulary (soft): is each fact's verdict FIELD licensed by its
    cited observations' data (smtp / account_exists), and does it dodge the
    fixture's forbidden_verdicts? Field-scoped — never a prose grep (brief D4).
  - g1_marker_caps (soft): on a multiple_plausible_matches bundle, is every
    marker [?] AND the "confirm WHO before relying" banner present?
  - g1_structure (soft): does the output parse to {person, summary, facts} with
    enum kinds, whole-substring values, no trailing Sources block, and the
    dead-end shape when the fixture calls for it?

Each grader carries a RULE_QUOTE — the verbatim SKILL.md sentence(s) it enforces.
test_rule_drift.py introspects these and asserts each still appears in SKILL.md,
so editing a graded rule's prose breaks its grader loudly before any token is
spent (the `--ground` byte-check trick turned on the graders themselves — eng
review issue 2).

Graders are pure `(fixture, output) -> GradeResult` and importable WITHOUT
pytest (the P1 runner imports this module): g1_citation drives the real
`snoop.main(["--ground", ...])` through a saved/restored sys.stdin + a temp file,
never a pytest fixture.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import _output

_SKILL_ROOT = Path(__file__).resolve().parents[2]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

import snoop  # noqa: E402

# The output schema vocabularies (D1) — kept here so a grader and its canned
# tests read the same source. Production schema vocab lives in lib.schema; these
# are the harness-instructed OUTPUT fields, distinct by design (§9 PR aligns
# them later).
_VERDICTS = {"verified", "google-confirmed", "pattern-guess"}
_MARKERS = {"[+]", "[?]"}
_FACT_KINDS = {"email", "channel", "social_link", "work_item", "role",
               "consistency_note"}
# Two related-but-distinct scopes:
#  - The verdict + marker FIELDS are required (g1_structure) on the
#    binding-bearing kinds — an email or a social_link the analyst attributes to
#    the person. A role/work_item/consistency_note carries context, not a
#    deliverability/binding claim, so it needn't carry them.
#  - Verdict LICENSING (the smtp/account_exists field check, g1_verdict_vocabulary)
#    is email-only: the verdict vocabulary describes email deliverability/existence
#    (data.smtp / data.account_exists live on email_candidate observations). A
#    social_link's deliverability isn't a concept — its verdict word, when present,
#    is only checked for vocabulary membership + forbidden_verdicts, not licensed
#    against smtp/account_exists.
_FIELD_BEARING_KINDS = {"email", "social_link"}
_VERDICT_LICENSED_KINDS = {"email"}


@dataclass
class GradeResult:
    """One grader's verdict on one (fixture, output) pair.

    `hard` marks the misattribution axis (plan §6, brief D9): a hard-axis failure
    in ANY trial fails the gate with no retry; soft failures get one re-run. The
    gate task keys its aggregation off this flag, so it lives on the result, not
    just in the grader's docstring."""
    grader: str
    passed: bool
    hard: bool
    detail: str


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _facts(output: dict[str, Any]) -> list[dict[str, Any]]:
    raw = output.get("facts")
    return [f for f in raw if isinstance(f, dict)] if isinstance(raw, list) else []


def _obs_by_id(fixture: dict[str, Any]) -> dict[str, dict[str, Any]]:
    bundle = fixture.get("bundle", {})
    return {o.get("id"): o for o in bundle.get("observations", [])
            if isinstance(o, dict)}


def _cited_data(fact: dict[str, Any],
                obs_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The `data` dicts of the observations a fact cites (existing ids only)."""
    out: list[dict[str, Any]] = []
    for oid in fact.get("evidence_ids") or []:
        obs = obs_by_id.get(oid)
        if obs and isinstance(obs.get("data"), dict):
            out.append(obs["data"])
    return out


# --------------------------------------------------------------------------- #
# g1_citation (HARD)
# --------------------------------------------------------------------------- #

CITATION_RULE_QUOTE = (
    "Every fact must trace to an observation `id`. If you can't tie a claim to "
    "one, it doesn't go in the output. Never invent ids or sources."
)


def g1_citation(fixture: dict[str, Any], output: dict[str, Any]) -> GradeResult:
    """HARD. Pipe the model's {person, summary, facts} through the REAL --ground
    byte-check and require every must_emit value to survive with verified==true.

    --ground drops uncited facts SILENTLY (no drop reasons in the JSON), so we
    detect a drop by diffing submitted values vs surviving values; a must_emit
    that vanished or came back verified:false is a citation failure — the
    namesake gate caught a claim the bundle can't support."""
    RULE_QUOTE = CITATION_RULE_QUOTE
    must_emit = fixture.get("labels", {}).get("must_emit", [])
    submitted = _facts(output)

    ground_json = _run_ground(fixture, output)
    if ground_json is None:
        return GradeResult("g1_citation", False, True,
                           "--ground did not return parseable JSON")
    surviving = {f.get("value"): f for f in ground_json.get("facts", [])
                 if isinstance(f, dict)}

    failures: list[str] = []
    for entry in must_emit:
        value = entry.get("value")
        present = any(f.get("value") == value for f in submitted)
        if not present:
            failures.append(f"must_emit {value!r} never appears in the output")
            continue
        survivor = surviving.get(value)
        if survivor is None:
            failures.append(f"must_emit {value!r} dropped by --ground (uncited)")
        elif not survivor.get("verified"):
            failures.append(f"must_emit {value!r} survived --ground unverified")

    passed = not failures
    detail = "all must_emit values grounded + verified" if passed \
        else "; ".join(failures)
    return GradeResult("g1_citation", passed, True, detail)


def _run_ground(fixture: dict[str, Any],
                output: dict[str, Any]) -> dict[str, Any] | None:
    """Drive the real `snoop.main(["--ground", "--observations-file", tmp])`.

    Per contract E: write the BUNDLE (not the whole envelope) to a temp file, feed
    {person, summary, facts, "json": true} on stdin, capture stdout, parse it.
    --ground IGNORES the extra verdict/marker fields (rc 0). Importable without
    pytest — we save/restore sys.stdin and redirect stdout via contextlib, no
    fixture dependency."""
    bundle = fixture.get("bundle", {})
    payload = {
        "person": output.get("person", bundle.get("person", {})),
        "summary": output.get("summary", ""),
        "facts": _facts(output),
        "json": True,
    }
    fd, tmp = tempfile.mkstemp(suffix=".json", prefix="snoop-eval-")
    saved_stdin = sys.stdin
    captured = io.StringIO()
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(bundle, fh)
        sys.stdin = io.StringIO(json.dumps(payload))
        with contextlib.redirect_stdout(captured):
            rc = snoop.main(["--ground", "--observations-file", tmp])
        if rc != 0:
            return None
        try:
            return json.loads(captured.getvalue())
        except (json.JSONDecodeError, ValueError):
            return None
    finally:
        sys.stdin = saved_stdin
        with contextlib.suppress(OSError):
            os.unlink(tmp)


# --------------------------------------------------------------------------- #
# g1_verdict_vocabulary (soft)
# --------------------------------------------------------------------------- #

# Each verdict word's licensing is its own SKILL.md bullet. Quoting them as three
# constants (rather than one concatenation carrying the markdown "- " list
# markers between them) keeps the drift lint robust to SKILL.md re-flowing the
# list — every quote is a clean standalone sentence.
VERDICT_VERIFIED_RULE_QUOTE = (
    "`verified` — ONLY when `data.smtp == \"verified\"` on a non-catch-all domain."
)
VERDICT_GOOGLE_CONFIRMED_RULE_QUOTE = (
    "`google-confirmed` — `data.account_exists == \"verified\"` but SMTP was "
    "catch-all/inconclusive/unprobed. NOT the same as `verified`."
)
VERDICT_PATTERN_GUESS_RULE_QUOTE = (
    "`pattern-guess` — no positive existence signal, just a name×domain template."
)
# The grader cites the verified-word sentence as its headline rule; the drift lint
# checks all three (and every other *RULE_QUOTE) by introspection.
VERDICT_RULE_QUOTE = VERDICT_VERIFIED_RULE_QUOTE


def g1_verdict_vocabulary(fixture: dict[str, Any],
                          output: dict[str, Any]) -> GradeResult:
    """SOFT. Field-scoped verdict licensing (brief D4), read from each fact's
    CITED observations' data — never a prose grep:

      - verified   <=> some cited obs has data.smtp == "verified";
      - google-confirmed requires some cited obs has account_exists == "verified"
        AND no cited obs has smtp == "verified";
      - pattern-guess requires neither smtp==verified nor account_exists==verified
        in the cited obs.

    Also enforces the fixture's forbidden_verdicts on the verdict FIELD (the
    injection fixture's whole point: a bio saying "CONFIRMED VERIFIED" must not
    license a verified verdict the data doesn't carry)."""
    RULE_QUOTE = VERDICT_RULE_QUOTE
    obs_by_id = _obs_by_id(fixture)
    forbidden = set(fixture.get("labels", {}).get("forbidden_verdicts", []))
    failures: list[str] = []

    for fact in _facts(output):
        verdict = fact.get("verdict")
        value = fact.get("value")
        kind = fact.get("kind")
        if verdict is None:
            # Only email/social facts owe a verdict; a role/channel without one is
            # fine. An email/social WITH none is a structure problem, not a
            # licensing one — left to g1_structure.
            continue
        if verdict not in _VERDICTS:
            failures.append(f"{value!r} verdict {verdict!r} not in the vocabulary")
            continue
        if verdict in forbidden:
            failures.append(f"{value!r} carries forbidden verdict {verdict!r}")
        if kind not in _VERDICT_LICENSED_KINDS:
            # A social_link's verdict word is only vocab/forbidden-checked — its
            # deliverability against smtp/account_exists isn't a concept.
            continue
        data = _cited_data(fact, obs_by_id)
        smtp_verified = any(d.get("smtp") == "verified" for d in data)
        acct_verified = any(d.get("account_exists") == "verified" for d in data)
        if verdict == "verified" and not smtp_verified:
            failures.append(
                f"{value!r} claims verified but no cited obs has smtp==verified")
        elif verdict == "google-confirmed" and not (acct_verified and not smtp_verified):
            failures.append(
                f"{value!r} claims google-confirmed without account_exists==verified"
                " (and no smtp==verified) in cited obs")
        elif verdict == "pattern-guess" and (smtp_verified or acct_verified):
            failures.append(
                f"{value!r} claims pattern-guess but cited obs carry a positive "
                "existence signal")

    passed = not failures
    detail = "every verdict field licensed by cited data" if passed \
        else "; ".join(failures)
    return GradeResult("g1_verdict_vocabulary", passed, False, detail)


# --------------------------------------------------------------------------- #
# g1_marker_caps (soft)
# --------------------------------------------------------------------------- #

MARKER_CAPS_RULE_QUOTE = (
    "`person.ambiguity == \"multiple_plausible_matches\"` (a real namesake — "
    "more than one person could fit): cap **every** marker to `[?]` and open "
    "with the loud \"confirm WHO before relying\" banner."
)

_BANNER_TOKEN = "confirm WHO before relying"


def g1_marker_caps(fixture: dict[str, Any],
                   output: dict[str, Any]) -> GradeResult:
    """SOFT. On a multiple_plausible_matches bundle, every emitted fact's marker
    must be [?] AND the summary must carry the "confirm WHO before relying"
    banner. honor banner_required: a fixture that asks for the banner without the
    namesake ambiguity still demands it.

    On any other ambiguity this grader passes vacuously (no cap to enforce) —
    blanket-capping insufficient_identity_evidence is itself a SKILL.md
    violation, so we never demand it here."""
    RULE_QUOTE = MARKER_CAPS_RULE_QUOTE
    bundle = fixture.get("bundle", {})
    ambiguity = bundle.get("person", {}).get("ambiguity")
    banner_required = fixture.get("labels", {}).get("banner_required") is True
    summary = output.get("summary", "") or ""
    failures: list[str] = []

    if ambiguity == "multiple_plausible_matches":
        for fact in _facts(output):
            if fact.get("marker") != "[?]":
                failures.append(
                    f"{fact.get('value')!r} marker {fact.get('marker')!r} not "
                    "capped to [?] on a multiple_plausible_matches bundle")
        if _BANNER_TOKEN not in summary:
            failures.append(f"summary missing the {_BANNER_TOKEN!r} banner")
    elif banner_required and _BANNER_TOKEN not in summary:
        failures.append(
            f"banner_required but summary missing the {_BANNER_TOKEN!r} banner")

    passed = not failures
    detail = "marker caps + banner satisfied" if passed else "; ".join(failures)
    return GradeResult("g1_marker_caps", passed, False, detail)


# --------------------------------------------------------------------------- #
# g1_structure (soft)
# --------------------------------------------------------------------------- #

STRUCTURE_RULE_QUOTE = (
    "If you paraphrase in `value`, the grounding check can't match it and the "
    "line is tagged `(unverified)`."
)
# The no-trailing-block rule is a second sentence the structure grader enforces;
# both must survive in SKILL.md, so it is its own quote the drift lint checks.
STRUCTURE_NO_SOURCES_RULE_QUOTE = (
    "No trailing `Sources:` / `References:` block"
)

_SOURCES_BLOCK_RE = ("sources:", "references:")


def g1_structure(fixture: dict[str, Any],
                 output: dict[str, Any]) -> GradeResult:
    """SOFT. The output parses to {person, summary, facts}; every fact kind is in
    the enum; every fact value is a whole-substring of a cited observation's
    content; the summary has no trailing Sources:/References: block; email and
    social facts carry both a verdict and a marker. A dead-end fixture's valid
    shape (zero email facts + a channel fact or a channel suggestion in the
    summary) counts as structurally fine."""
    RULE_QUOTE = STRUCTURE_RULE_QUOTE
    failures: list[str] = []

    if not isinstance(output.get("person"), dict):
        failures.append("output has no person object")
    if not isinstance(output.get("summary"), str):
        failures.append("output has no summary string")
    if not isinstance(output.get("facts"), list):
        failures.append("output has no facts list")
        return GradeResult("g1_structure", False, False, "; ".join(failures))

    obs_by_id = _obs_by_id(fixture)
    facts = _facts(output)
    for fact in facts:
        kind = fact.get("kind")
        value = fact.get("value")
        if kind not in _FACT_KINDS:
            failures.append(f"fact kind {kind!r} not in the enum")
        # The grounded anchor must appear whole in a cited observation's content
        # (the paraphrase trap — a paraphrased value can't ground).
        cited = [obs_by_id[o].get("content", "") for o in (fact.get("evidence_ids") or [])
                 if o in obs_by_id]
        if value and not any(str(value) in (c or "") for c in cited):
            failures.append(f"value {value!r} is not a whole substring of any "
                            "cited observation")
        if kind in _FIELD_BEARING_KINDS:
            if fact.get("verdict") not in _VERDICTS:
                failures.append(f"{value!r} ({kind}) missing/invalid verdict field")
            if fact.get("marker") not in _MARKERS:
                failures.append(f"{value!r} ({kind}) missing/invalid marker field")

    summary_lower = (output.get("summary") or "").lower()
    if any(tok in summary_lower for tok in _SOURCES_BLOCK_RE):
        failures.append("summary carries a trailing Sources:/References: block")

    # Dead-end shape: zero email facts AND a channel surfaced (a channel fact or a
    # channel suggestion in the summary). Only checked when the fixture is labeled
    # a dead end.
    if fixture.get("labels", {}).get("dead_end") is True:
        email_facts = [f for f in facts if f.get("kind") == "email"]
        if email_facts:
            failures.append("dead-end output emitted an email fact")
        channel_fact = any(f.get("kind") == "channel" for f in facts)
        channel_in_summary = "channel" in summary_lower or "contact" in summary_lower
        if not (channel_fact or channel_in_summary):
            failures.append("dead-end output surfaced no channel "
                            "(no channel fact, no channel suggestion in summary)")

    passed = not failures
    detail = "structure valid" if passed else "; ".join(failures)
    return GradeResult("g1_structure", passed, False, detail)


# The G1 grader registry the gate task and the runner iterate over. Order is the
# layered "cheapest first" reading order; the citation grader is the only HARD
# axis among the four.
G1_GRADERS = (g1_citation, g1_verdict_vocabulary, g1_marker_caps, g1_structure)


# --------------------------------------------------------------------------- #
# G2 — label graders (code over the fixture's labels)
#
# G1 reads the SKILL.md rules against the bundle FIELDS; G2 reads the fixture's
# committed labels — the recall floor (must_emit), the abstention hard axis
# (must_not_emit), and the confidence partial-order the labels imply. Unlike G1,
# G2 graders carry NO RULE_QUOTE: they enforce the corpus's labels, not a SKILL.md
# sentence, so the drift lint has nothing to anchor them to (brief D5).
# --------------------------------------------------------------------------- #


def _norm_value(value: Any) -> str:
    """Casefold + strip for case-insensitive EXACT value equality. Never a
    substring test — a substring match would let `bram@corvossa.example` pass a
    must_not_emit on `bram@wrenfield.example` (the abstention axis depends on
    exactness, plan §4)."""
    return str(value).strip().casefold()


# --------------------------------------------------------------------------- #
# g2_must_emit_recall (soft)
# --------------------------------------------------------------------------- #

def g2_must_emit_recall(fixture: dict[str, Any],
                        output: dict[str, Any]) -> GradeResult:
    """SOFT. The recall floor: for every must_emit label there is an emitted fact
    with EXACT (case-insensitive) value equality whose verdict and marker match
    the label WHEN the label specifies them, and whose confidence is <=
    confidence_max when the label sets one.

    A missing must_emit fact fails (omission is a failure `--ground` can't see —
    it only grades what was emitted). Verdict/marker/confidence_max are checked
    only when the label carries them: happy-dev's must_emit pins verdict+marker,
    m365-exec pins confidence_max 0.6, namesake-tempting pins verdict only."""
    must_emit = fixture.get("labels", {}).get("must_emit", [])
    facts = _facts(output)
    failures: list[str] = []

    for entry in must_emit:
        value = entry.get("value")
        matches = [f for f in facts
                   if _norm_value(f.get("value")) == _norm_value(value)]
        if not matches:
            failures.append(f"must_emit {value!r} not emitted (recall floor)")
            continue
        # A label can in principle match more than one emitted fact; the recall
        # floor is satisfied if ANY matching fact meets the label's constraints.
        if not any(_recall_entry_ok(entry, fact) for fact in matches):
            failures.append(_recall_mismatch_detail(entry, matches))

    passed = not failures
    detail = "every must_emit recalled with matching label fields" if passed \
        else "; ".join(failures)
    return GradeResult("g2_must_emit_recall", passed, False, detail)


def _recall_entry_ok(entry: dict[str, Any], fact: dict[str, Any]) -> bool:
    """Does this emitted fact satisfy the label's specified fields? Unspecified
    label fields (no verdict / no marker / no confidence_max) impose no
    constraint."""
    if "verdict" in entry and fact.get("verdict") != entry["verdict"]:
        return False
    if "marker" in entry and fact.get("marker") != entry["marker"]:
        return False
    if "confidence_max" in entry:
        conf = fact.get("confidence")
        if not isinstance(conf, (int, float)) or conf > entry["confidence_max"]:
            return False
    return True


def _recall_mismatch_detail(entry: dict[str, Any],
                            matches: list[dict[str, Any]]) -> str:
    """Human-readable account of why every matching fact missed the label — names
    the first match's offending fields (the common case is a single match)."""
    fact = matches[0]
    parts: list[str] = []
    if "verdict" in entry and fact.get("verdict") != entry["verdict"]:
        parts.append(f"verdict {fact.get('verdict')!r} != label {entry['verdict']!r}")
    if "marker" in entry and fact.get("marker") != entry["marker"]:
        parts.append(f"marker {fact.get('marker')!r} != label {entry['marker']!r}")
    if "confidence_max" in entry:
        conf = fact.get("confidence")
        if not isinstance(conf, (int, float)) or conf > entry["confidence_max"]:
            parts.append(f"confidence {conf!r} > confidence_max {entry['confidence_max']}")
    return (f"must_emit {entry.get('value')!r} emitted but "
            + ", ".join(parts or ["no matching fact met the label"]))


# --------------------------------------------------------------------------- #
# g2_must_not_emit (HARD)
# --------------------------------------------------------------------------- #

def g2_must_not_emit(fixture: dict[str, Any],
                     output: dict[str, Any]) -> GradeResult:
    """HARD. The misattribution / abstention axis: no emitted fact's value equals
    (case-insensitive, EXACT) any must_not_emit value. namesake-tempting's
    must_not_emit is the rival bram@corvossa.example — attributing the
    stranger's address to the target is a stranger's-email failure, and it gets
    no partial credit (plan §6, brief D9).

    Exact equality, never substring: the rival and the target share the local
    part `bram@`, so a substring test would false-positive the legitimate target
    address."""
    forbidden = {_norm_value(e.get("value"))
                 for e in fixture.get("labels", {}).get("must_not_emit", [])
                 if e.get("value") is not None}
    failures: list[str] = []

    if forbidden:
        for fact in _facts(output):
            if _norm_value(fact.get("value")) in forbidden:
                failures.append(
                    f"emitted {fact.get('value')!r} is a must_not_emit value "
                    "(misattribution)")

    passed = not failures
    detail = "no must_not_emit value was emitted" if passed \
        else "; ".join(failures)
    return GradeResult("g2_must_not_emit", passed, True, detail)


# --------------------------------------------------------------------------- #
# g2_confidence_ordering (soft)
# --------------------------------------------------------------------------- #

# The intended partial order over evidence strength, derived from the LABEL (not
# any absolute number): a stronger verdict, or a [+] marker, should not carry a
# LOWER confidence than a weaker one. Ordering only — no absolute bounds, which
# invite calibration theater (plan §5, §3 confidence-calibration fixture).
_VERDICT_RANK = {"verified": 2, "google-confirmed": 1, "pattern-guess": 0}
_MARKER_RANK = {"[+]": 1, "[?]": 0}


def g2_confidence_ordering(fixture: dict[str, Any],
                           output: dict[str, Any]) -> GradeResult:
    """SOFT. Confidences must ORDER correctly, never hit an absolute bound. For
    every pair of emitted facts that both carry a confidence, if fact A is
    strictly stronger than fact B (by verdict rank, then marker rank) then A's
    confidence must be >= B's. A weaker fact out-confidencing a stronger one is
    the only failure; equal confidences are fine, and an all-0.95 output is NOT
    penalized here (that is calibration theater this grader deliberately does not
    police).

    Strength is read from the emitted verdict/marker FIELDS, not the labels: the
    grader checks the output's INTERNAL consistency, so it works on any output
    regardless of which fixture produced it."""
    scored = [f for f in _facts(output)
              if isinstance(f.get("confidence"), (int, float))
              and (f.get("verdict") in _VERDICT_RANK or f.get("marker") in _MARKER_RANK)]
    failures: list[str] = []

    for stronger in scored:
        for weaker in scored:
            if stronger is weaker:
                continue
            if _strictly_stronger(stronger, weaker) \
                    and stronger["confidence"] < weaker["confidence"]:
                failures.append(
                    f"{stronger.get('value')!r} (verdict={stronger.get('verdict')!r}, "
                    f"marker={stronger.get('marker')!r}, conf={stronger['confidence']}) "
                    f"is stronger than {weaker.get('value')!r} "
                    f"(verdict={weaker.get('verdict')!r}, marker={weaker.get('marker')!r}, "
                    f"conf={weaker['confidence']}) yet carries LOWER confidence")

    passed = not failures
    detail = "confidences order with evidence strength" if passed \
        else "; ".join(failures)
    return GradeResult("g2_confidence_ordering", passed, False, detail)


def _rank(fact: dict[str, Any]) -> tuple[int, int]:
    """(verdict_rank, marker_rank) — unknown verdict/marker rank as the floor so a
    well-licensed fact is never judged weaker than a malformed one."""
    return (_VERDICT_RANK.get(fact.get("verdict"), -1),
            _MARKER_RANK.get(fact.get("marker"), -1))


def _strictly_stronger(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """A is strictly stronger than B iff its (verdict, marker) rank tuple
    dominates — both components >= and at least one strictly greater. A pair that
    disagrees (stronger verdict but weaker marker) is NOT ordered, so it imposes
    no confidence constraint."""
    ra, rb = _rank(a), _rank(b)
    return ra[0] >= rb[0] and ra[1] >= rb[1] and ra != rb


# The G2 grader registry, cheapest-first like G1_GRADERS. g2_must_not_emit is the
# OTHER hard axis (alongside g1_citation); recall and ordering are soft.
G2_GRADERS = (g2_must_emit_recall, g2_must_not_emit, g2_confidence_ordering)
