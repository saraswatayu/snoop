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
import re
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
from lib.schema import BELONGS_MARKERS, EMAIL_VERDICTS, FACT_KINDS  # noqa: E402

# The output-contract vocabularies live in lib.schema (the single source the
# graders, the renderer, and the standing instruction all read), so editing the
# vocabulary in one place can't silently drift from another.
_VERDICTS = EMAIL_VERDICTS
_MARKERS = BELONGS_MARKERS
_FACT_KINDS = FACT_KINDS
# Two per-kind capability scopes — one source of truth each (no back-compat
# aliases or parallel copies: a duplicated kind set silently drifts when only one
# copy is edited):
#  - MARKER-bearing kinds carry the [+]/[?] belongs-to-person marker
#    (SKILL.md:209-214) — an email or a social_link the analyst attributes to the
#    person. A role/work_item/channel/consistency_note carries context, not a
#    binding claim, so it needn't carry a marker.
#  - VERDICT-bearing kinds carry the verified|google-confirmed|pattern-guess
#    deliverability word, which SKILL.md scopes (lines 226-231) entirely to an
#    email's data.smtp / data.account_exists. A non-email fact has no
#    deliverability concept, so it carries — and is licensed for — no verdict; a
#    stray verdict on one is flagged (g1_verdict_vocabulary). The same set governs
#    both "must carry a verdict" (g1_structure) and "verdict is field-licensed"
#    (g1_verdict_vocabulary): they are the same concept, so they are one constant.
_MARKER_BEARING_KINDS = {"email", "social_link"}
_VERDICT_BEARING_KINDS = {"email"}


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
    """Map observation id -> observation. Id-less observations are SKIPPED (so a
    fact citing null can't resolve to one), and a duplicate id keeps the FIRST
    occurrence (no silent last-wins shadowing). A clean fixture has unique string
    ids; this just fails closed on a malformed one."""
    bundle = fixture.get("bundle", {})
    out: dict[str, dict[str, Any]] = {}
    for o in bundle.get("observations", []):
        if not isinstance(o, dict):
            continue
        oid = o.get("id")
        if not isinstance(oid, str) or not oid:
            continue
        out.setdefault(oid, o)
    return out


def _value_grounded_substring(value: Any, contents: list[str]) -> bool:
    """Case-insensitive WHOLE-substring check: the value (stripped, lowercased)
    appears verbatim in some cited observation's content. Deliberately STRICTER
    than --ground's lib.ground._value_appears, which falls back to a single
    longest-token match — that fallback is right for the production verifier but
    would let g1_structure pass a paraphrase that merely shares one token
    ("Brightforge Industries Inc" against cited "Brightforge Labs"). g1_structure
    IS the paraphrase trap, so it stays whole-substring; case-insensitivity still
    tolerates a benign re-casing (a lowercased email), and the HARD g1_citation
    grader still drives the real --ground for the must_emit values."""
    v = str(value).strip().lower()
    if not v:
        return False
    return any(v in (c or "").lower() for c in contents)


def _cited_data(fact: dict[str, Any],
                obs_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The `data` dicts of the observations a fact cites (existing ids only)."""
    out: list[dict[str, Any]] = []
    for oid in fact.get("evidence_ids") or []:
        obs = obs_by_id.get(oid)
        if obs and isinstance(obs.get("data"), dict):
            out.append(obs["data"])
    return out


def _flag_true(value: Any) -> bool:
    """Interpret a bundle flag (e.g. name_match) as a boolean, failing CLOSED on
    a stringy value: bool('no') is truthy, so a plain bool() would let a
    name-mismatch rival ('name_match': 'no') license google-confirmed. Only a
    real True, a nonzero number, or an affirmative string counts as true; 'no' /
    'false' / '0' / '' / None are false."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y", "t", "1"}
    return False


def _obs_licenses_google_confirmed(d: dict[str, Any]) -> bool:
    """Whether ONE cited observation's own data licenses `google-confirmed`
    (SKILL.md:228-231 + :350/:378): account_exists=="verified" AND a text
    name_match, with the SMTP state in {catch_all, inconclusive, unprobed,
    unstated}. A locked-tenant 'exists_unverifiable' account can't bind identity
    (no name_match), a name_match=no candidate is a DIFFERENT person, a 5xx
    'invalid' mailbox is a bounce, and smtp=="verified" would be plain `verified`
    — none license google-confirmed. Read per-obs so the existence and SMTP
    signals must come from the SAME observation, never a mix (finding #6)."""
    return (d.get("account_exists") == "verified"
            and _flag_true(d.get("name_match"))
            and d.get("smtp") not in {"verified", "invalid"})


# A URL or @-handle a model could echo into the summary to point at a REAL
# reachability channel (finding [9]). The STRUCTURED source_url is the primary,
# clean signal; this regex is a fallback for a channel mentioned only in prose,
# hardened against two extraction bugs (finding #13):
#  - URLs: trailing sentence punctuation is stripped off the match (so a
#    period-terminated "...contact." still equals the clean URL in the summary).
#  - Handles: a negative lookbehind keeps an email local part
#    ("contact@graymoor.example") from yielding a bogus "@graymoor" handle.
_CHANNEL_TOKEN_RE = re.compile(
    r"https?://[^\s)]+|(?<![\w.@])@[A-Za-z0-9_]{2,}", re.IGNORECASE)
_URL_TRAILING_PUNCT = ".,;:!?)]}>\"'"


def _channel_hint_candidates(fixture: dict[str, Any]) -> list[str]:
    """The real channel strings a dead-end output could surface, drawn from the
    bundle's STRUCTURED hints first — every channel_hint observation's source_url
    and any top-level bundle channel_hints value — plus, as a fallback, any
    URL/handle embedded in a hint's content prose (trailing punctuation stripped
    off URLs, email locals excluded from handles). Used to verify a dead-end
    summary suggests a REAL channel, not the literal words 'channel'/'contact'."""
    bundle = fixture.get("bundle", {})
    out: list[str] = []
    for obs in bundle.get("observations", []):
        if not isinstance(obs, dict) or obs.get("type") != "channel_hint":
            continue
        src = obs.get("source_url")
        if isinstance(src, str) and src.strip():
            out.append(src.strip())
        for tok in _CHANNEL_TOKEN_RE.findall(str(obs.get("content", ""))):
            out.append(tok if tok.startswith("@")
                       else tok.rstrip(_URL_TRAILING_PUNCT))
    hints = bundle.get("channel_hints")
    if isinstance(hints, dict):
        out.extend(str(v) for v in hints.values() if v)
    elif isinstance(hints, list):
        out.extend(str(v) for v in hints if v)
    return [h for h in out if h]


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
    must_emit = fixture.get("labels", {}).get("must_emit", [])
    submitted = _facts(output)

    ground_json = _run_ground(fixture, output)
    if ground_json is None:
        return GradeResult("g1_citation", False, True,
                           "--ground did not return parseable JSON")
    # finding [4]: keep ALL surviving facts (a list), not a value-keyed dict —
    # a dict is last-write-wins, so a correctly-grounded copy of a duplicated
    # value would be shadowed by a later wrongly-cited copy, hard-failing an
    # output that DID emit a verified copy.
    surviving = [f for f in ground_json.get("facts", []) if isinstance(f, dict)]

    failures: list[str] = []
    for entry in must_emit:
        value = entry.get("value")
        # finding [3]: normalize with _norm_value (casefold+strip) for BOTH the
        # presence check and the surviving lookup, matching every other grader
        # and --ground's own case-insensitive _value_appears. A case/whitespace
        # variant of a correct, --ground-verified address must not HARD-fail.
        nv = _norm_value(value)
        present = any(_norm_value(f.get("value")) == nv for f in submitted)
        if not present:
            failures.append(f"must_emit {value!r} never appears in the output")
            continue
        # finding [4]: pass if ANY surviving fact with this (normalized) value
        # verified; order-independent and tolerant of a duplicate uncited copy.
        matches = [s for s in surviving if _norm_value(s.get("value")) == nv]
        if not matches:
            failures.append(f"must_emit {value!r} dropped by --ground (uncited)")
        elif not any(s.get("verified") for s in matches):
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

      - verified         <=> some cited obs has data.smtp == "verified";
      - google-confirmed <=> some SINGLE cited obs has account_exists=="verified"
        AND a text name_match (SKILL.md:378) AND an SMTP state in
        {catch_all, inconclusive, unprobed, unstated}. A locked-tenant
        'exists_unverifiable' account (no name bind — finding #8), a name_match=no
        rival (finding #9), a 5xx 'invalid' mailbox (finding #5), or
        smtp==verified do NOT license it. Read per-obs, never a field-mix across
        observations (finding #6);
      - pattern-guess requires no STRONG positive existence signal (neither
        smtp==verified nor account_exists==verified) in the cited obs — an
        exists_unverifiable account, carrying neither, may be pattern-guess.

    A verdict on a NON-email kind is flagged: SKILL.md gives social_links etc. no
    verdict word (finding [1]). Also enforces the fixture's forbidden_verdicts on
    the verdict FIELD (the injection fixture's whole point: a bio saying
    "CONFIRMED VERIFIED" must not license a verified verdict the data doesn't
    carry)."""
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
        if kind not in _VERDICT_BEARING_KINDS:
            # SKILL.md gives a non-email kind (e.g. social_link) NO verdict word —
            # its deliverability against smtp/account_exists isn't a concept. A
            # stray verdict here is unlicensed by construction, so flag it as
            # defense-in-depth (finding [1]) rather than silently waving it
            # through; the vocab + forbidden checks above still ran.
            failures.append(
                f"{value!r} ({kind}) carries a verdict {verdict!r} but SKILL.md "
                "gives non-email facts no verdict word")
            continue
        data = _cited_data(fact, obs_by_id)
        # Strong positive existence signals are read across the cited obs only to
        # PROHIBIT a too-weak verdict (pattern-guess can't sit on a verified
        # account); the POSITIVE licensing of verified/google-confirmed is per-obs
        # (finding #6) so existence + SMTP must come from the same observation.
        smtp_verified = any(d.get("smtp") == "verified" for d in data)
        acct_verified = any(d.get("account_exists") == "verified" for d in data)
        if verdict == "verified" and not smtp_verified:
            failures.append(
                f"{value!r} claims verified but no cited obs has smtp==verified")
        elif verdict == "google-confirmed" and not any(
                _obs_licenses_google_confirmed(d) for d in data):
            failures.append(
                f"{value!r} claims google-confirmed but no single cited obs has "
                "account_exists==verified + a text name_match + an SMTP state in "
                "{catch_all, inconclusive, unprobed} (SKILL.md:228/378): a locked "
                "'exists_unverifiable' account, a name_match=no rival, a 5xx "
                "'invalid' mailbox, and smtp==verified do not license it")
        elif verdict == "pattern-guess" and (smtp_verified or acct_verified):
            failures.append(
                f"{value!r} claims pattern-guess but cited obs carry a strong "
                "positive existence signal (smtp==verified or "
                "account_exists==verified)")

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


def _banner_present(summary: str) -> bool:
    """Case-insensitive containment (finding #14): a conformant loud banner the
    model may render as 'Confirm WHO before relying', title case, or ALL CAPS must
    not false-fail. The grader checks PRESENCE of the banner phrase; SKILL.md's
    WHO emphasis is a style cue, not something the grader polices by casing."""
    return _BANNER_TOKEN.lower() in (summary or "").lower()


def g1_marker_caps(fixture: dict[str, Any],
                   output: dict[str, Any]) -> GradeResult:
    """SOFT. On a multiple_plausible_matches bundle, every emitted fact's marker
    must be [?] AND the summary must carry the "confirm WHO before relying"
    banner. honor banner_required: a fixture that asks for the banner without the
    namesake ambiguity still demands it.

    On any other ambiguity this grader passes vacuously (no cap to enforce) —
    blanket-capping insufficient_identity_evidence is itself a SKILL.md
    violation, so we never demand it here."""
    bundle = fixture.get("bundle", {})
    ambiguity = bundle.get("person", {}).get("ambiguity")
    banner_required = fixture.get("labels", {}).get("banner_required") is True
    summary = output.get("summary", "") or ""
    failures: list[str] = []

    if ambiguity == "multiple_plausible_matches":
        for fact in _facts(output):
            # SKILL.md:372 caps EVERY marker to [?]. A context fact that
            # legitimately carries NO marker (role/channel/work_item/
            # consistency_note — the namesake identity-check line) is fine
            # (finding [6]); but a fact that DOES carry a marker must be [?],
            # whatever its kind — finding [6]'s kind-based skip over-corrected and
            # let a stray [+] binding overclaim on a context fact escape the cap
            # (finding #7).
            marker = fact.get("marker")
            if marker is None:
                continue
            if marker != "[?]":
                failures.append(
                    f"{fact.get('value')!r} marker {marker!r} not "
                    "capped to [?] on a multiple_plausible_matches bundle")
        if not _banner_present(summary):
            failures.append(f"summary missing the {_BANNER_TOKEN!r} banner")
    elif banner_required and not _banner_present(summary):
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

# SKILL.md forbids a TRAILING `Sources:`/`References:` block, not the words
# appearing inline in prose. The discriminator (finding [8] re-fix) is
# BLOCK-INITIAL: a real block's label starts its own line AND is preceded by
# nothing, a blank line, or a line that ENDS A SENTENCE. A soft-wrapped
# "...drawn from several\nsources:" continues an unterminated clause (the prior
# line has no terminal punctuation), so it is NOT a block. The label class allows
# markdown emphasis, list bullets, and numbered-list prefixes ("1. Sources:").
_SOURCES_LABEL_RE = re.compile(
    r"^\s*[>*_#\-]*\s*\d*[.)]?\s*(sources|references)\s*:", re.IGNORECASE)


def _has_trailing_sources_block(summary: str) -> bool:
    """True iff a `Sources:`/`References:` label begins a BLOCK — its own line,
    preceded by nothing / a blank line / a sentence-ending line — anywhere in the
    summary. Catches a real trailing block (preceded by a finished sentence or a
    blank line, incl. numbered "1. Sources:" and blocks not in the final
    paragraph) while NOT flagging a soft-wrapped inline "...several\\nsources:"
    mention (preceded by an unterminated clause). Both failure modes the old
    last-paragraph heuristic got wrong."""
    if not summary:
        return False
    lines = summary.splitlines()
    for i, line in enumerate(lines):
        if not _SOURCES_LABEL_RE.match(line):
            continue
        prev = next((lines[j].rstrip() for j in range(i - 1, -1, -1)
                     if lines[j].strip()), None)
        blank_before = i > 0 and not lines[i - 1].strip()
        if prev is None or blank_before or prev.endswith((".", "!", "?")):
            return True
    return False


def g1_structure(fixture: dict[str, Any],
                 output: dict[str, Any]) -> GradeResult:
    """SOFT. The output parses to {person, summary, facts}; every fact kind is in
    the enum; every fact value is a whole-substring of a cited observation's
    content; the summary has no trailing Sources:/References: block; email and
    social facts carry both a verdict and a marker. A dead-end fixture's valid
    shape (zero email facts + a channel fact or a channel suggestion in the
    summary) counts as structurally fine."""
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
        # finding [11]: an empty/None value is a malformed grounded anchor on ANY
        # kind — flag it BEFORE the substring check, which short-circuits on a
        # falsy value (and `"" in content` is vacuously True, so the substring
        # check alone can never catch it). --ground would tag it (unverified).
        if value is None or str(value).strip() == "":
            failures.append(f"{kind!r} fact has an empty value (no grounded anchor)")
        # The grounded anchor must appear WHOLE in a cited observation's content
        # (the paraphrase trap — a paraphrased value can't ground). Case-insensitive
        # whole-substring, deliberately stricter than --ground's longest-token
        # fallback (finding [7] re-fix — see _value_grounded_substring).
        elif value:
            cited = [obs_by_id[o].get("content", "")
                     for o in (fact.get("evidence_ids") or []) if o in obs_by_id]
            if not _value_grounded_substring(value, cited):
                failures.append(f"value {value!r} is not a whole substring of any "
                                "cited observation")
        # finding [1]: the VERDICT field is email-only (SKILL.md gives social_links
        # no verdict word); the MARKER field stays required on email + social_link
        # (the [+]/[?] belongs-to-person axis, SKILL.md:209-214).
        if kind in _VERDICT_BEARING_KINDS and fact.get("verdict") not in _VERDICTS:
            failures.append(f"{value!r} ({kind}) missing/invalid verdict field")
        if kind in _MARKER_BEARING_KINDS and fact.get("marker") not in _MARKERS:
            failures.append(f"{value!r} ({kind}) missing/invalid marker field")

    summary_lower = (output.get("summary") or "").lower()
    if _has_trailing_sources_block(output.get("summary") or ""):
        failures.append("summary carries a trailing Sources:/References: block")

    # Dead-end shape: zero email facts AND a channel surfaced (a channel fact or a
    # channel suggestion in the summary). Only checked when the fixture is labeled
    # a dead end.
    if fixture.get("labels", {}).get("dead_end") is True:
        email_facts = [f for f in facts if f.get("kind") == "email"]
        if email_facts:
            failures.append("dead-end output emitted an email fact")
        channel_fact = any(f.get("kind") == "channel" for f in facts)
        # finding [9]: a channel SUGGESTION in the summary means a REAL channel
        # from the bundle's hints surfaced — not the literal words
        # "channel"/"contact" (which false-fail "try his website form at ..." and
        # spuriously pass a URL that merely ends in /contact). Gather candidate
        # channel strings from channel_hint observations (source_url + any
        # URL/handle embedded in content) and from a top-level channel_hints field.
        channel_in_summary = any(
            hint and hint.lower() in summary_lower
            for hint in _channel_hint_candidates(fixture))
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
    m365-exec pins confidence_max 0.6, namesake-tempting pins verdict only.

    finding #3: a kind=="channel" must_emit is ALSO satisfied by the channel value
    appearing in the summary (a channel SUGGESTION), matching the dead-end output
    contract (_output.py) and g1_structure's dead-end shape — so a contract-faithful
    summary-only dead end is not failed by the recall floor. The and/or-summary
    allowance is channel-only; an email recall floor still needs an emitted fact."""
    must_emit = fixture.get("labels", {}).get("must_emit", [])
    facts = _facts(output)
    summary_cf = (output.get("summary") or "").casefold()
    failures: list[str] = []

    for entry in must_emit:
        value = entry.get("value")
        matches = [f for f in facts
                   if _norm_value(f.get("value")) == _norm_value(value)]
        if not matches:
            if (entry.get("kind") == "channel" and value
                    and str(value).strip().casefold() in summary_cf):
                continue  # a channel suggested in the summary satisfies recall
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
        # The summary LEADS with the email answer (SKILL.md standing instruction),
        # so a forbidden address presented there misattributes a stranger's email
        # to the target just as a fact would — even when no FACT carries it. Match
        # the FULL forbidden address as a substring of the summary: that is safe
        # because the rival and target share only the local part (`bram@`), not the
        # whole address, so the legitimate target never false-trips.
        summary_l = _norm_value(output.get("summary"))
        for bad in forbidden:
            if bad and bad in summary_l:
                failures.append(
                    f"must_not_emit value {bad!r} appears in the summary "
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
    strictly stronger than fact B (dominating on the strength axes BOTH define)
    then A's confidence must be >= B's. A weaker fact out-confidencing a stronger
    one is
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


def _strictly_stronger(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """A is strictly stronger than B over the axes BOTH facts define. An axis
    (verdict, marker) counts only when both facts carry a RANKABLE value for it —
    so a verdict-less social_link and an email compare on the marker axis alone,
    never with the social_link's absent verdict floored below pattern-guess
    (finding #1, which made any email out-rank a validated social_link of the same
    marker). A dominates iff, over the shared axes, it is >= on every one and >
    on at least one. With no shared axis — or a disagreement across axes — the two
    are incomparable and impose no confidence constraint."""
    shared = False
    stronger_somewhere = False
    if a.get("verdict") in _VERDICT_RANK and b.get("verdict") in _VERDICT_RANK:
        shared = True
        ra, rb = _VERDICT_RANK[a["verdict"]], _VERDICT_RANK[b["verdict"]]
        if ra < rb:
            return False
        stronger_somewhere = stronger_somewhere or ra > rb
    if a.get("marker") in _MARKER_RANK and b.get("marker") in _MARKER_RANK:
        shared = True
        ma, mb = _MARKER_RANK[a["marker"]], _MARKER_RANK[b["marker"]]
        if ma < mb:
            return False
        stronger_somewhere = stronger_somewhere or ma > mb
    return shared and stronger_somewhere


# The G2 grader registry, cheapest-first like G1_GRADERS. g2_must_not_emit is the
# OTHER hard axis (alongside g1_citation); recall and ordering are soft.
G2_GRADERS = (g2_must_emit_recall, g2_must_not_emit, g2_confidence_ordering)
