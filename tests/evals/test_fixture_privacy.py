"""CI privacy gate for committed eval fixtures (plan §2, brief D7).

The closed-world insight (plan §1): the analyst layer's whole input is the
observation bundle, so synthetic fixtures about fictional people carry zero
privacy cost — PROVIDED nothing real ever sneaks into a committed bundle. This
gate is what enforces "nothing real" mechanically, not by hand: it walks every
committed `tests/evals/fixtures/*.json`, recursively extracts every @-address,
domain, URL, handle-like string, and name-bearing field, and asserts each is a
member of the fictional roster or the reserved/allowlisted domain sets.

The checker is factored as `scan_fixtures(dir_path, roster)` so the NEGATIVE
SELF-TEST can point it at a tmp_path holding a deliberately-leaking fixture and
prove the gate actually catches a leak (a gate never seen catching a leak is
trusted on faith — eng review issue 8).

Superset of the T5 calibration gate (test_calibration.py), which only checks
ground_truth @-addresses and names; this checks every slot of every fixture.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[2]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
PERSONAS_MD = Path(__file__).resolve().parent / "personas.md"

# RFC 2606 reserved domains: example.{com,org,net} exactly, plus any subdomain
# of the reserved TLDs .example and .test. keys.openpgp.example (a fixture PGP
# key server) is reserved by the .example suffix rule.
_RESERVED_EXACT = {"example.com", "example.org", "example.net"}
_RESERVED_SUFFIXES = (".example", ".test")

# Explicit fictional-domain allowlist (house style: a set literal in the gate,
# NOT seeded from SKILL.md's worked-example domains — openai.com / linear.app /
# steipete.com / replicate.com are REAL). Empty today: every committed fixture
# domain is RFC 2606 reserved. New fictional domains that can't be reserved go
# here, never in the bundle by accident.
_FICTIONAL_DOMAIN_ALLOWLIST: set[str] = set()

# The only non-reserved URL host a fixture may reference: github.com, and only
# on a /<roster-handle> path (every roster handle is snoop-fixture-*).
_GITHUB_HOST = "github.com"

# Every roster GitHub/HN handle is namespaced so it can be registered as a
# maintainer-owned account; the gate hard-asserts the prefix on each handle.
_HANDLE_PREFIX = "snoop-fixture-"

# A handle-like token: github.com/<handle> path segments and "service:handle"
# pairs. We treat any alnum/dash run that looks like an account name and assert
# roster membership; addresses and reserved domains are checked separately.
_GITHUB_PATH_RE = re.compile(r"github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)")
_ADDRESS_RE = re.compile(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
_NAME_KEYS = {"name", "google_display_name", "gh_name"}

# Names also hide in observation CONTENT prose — not just the structured
# _NAME_KEYS fields — in the two forms reason.py templates them into: the
# `name_match = <Name>` identity anchor and a `google_display_name="<Name>"`
# mirror (finding #10). The content string is exactly what the analyst reads, so
# a real name would leak there; extract those and roster-check them too. The
# name_match form requires >=2 capitalized tokens so the boolean `name_match=yes`
# / `no` and lowercase `snoop-fixture-*` handles are never mistaken for names.
_CONTENT_NAME_RES = (
    re.compile(r'google_display_name\s*=\s*"([^"]+)"'),
    re.compile(r"name_match\s*=\s*([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*)+)"),
)

# A bare-domain scan over arbitrary prose would false-positive on dotted code
# identifiers ("data.smtp", "first.last") whose final label isn't a real TLD.
# We only treat a dotted token as a domain-to-validate when its last label is a
# real, network-resolvable TLD shape — the reserved TLDs we want plus the common
# real TLDs a leak would plausibly use. A leak on any of these MUST be reserved
# or roster; "data.smtp" / "first.last" simply aren't domains and are ignored.
_LEAK_PRONE_TLDS = (
    "example", "test",  # RFC 2606 reserved (must still resolve to reserved set)
    "com", "org", "net", "io", "dev", "app", "ai", "co", "me", "info", "biz",
    "edu", "gov", "uk", "us", "ca", "de", "fr", "tech", "xyz", "cloud", "page",
)
_DOMAIN_RE = re.compile(
    r"\b([a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)*\.(?:"
    + "|".join(_LEAK_PRONE_TLDS) + r"))\b")


@dataclass(frozen=True)
class Roster:
    """The fictional persona roster, parsed mechanically from personas.md."""
    names: frozenset[str]
    handles: frozenset[str]       # github + other-handle account names
    domains: frozenset[str]       # roster-declared email/site domains


def parse_roster(path: Path = PERSONAS_MD) -> Roster:
    """Parse personas.md's single Markdown table per its stated parse contract.

    Columns in order: name | github | other handles | domains | employer |
    fixtures | registration. A persona row is any `|`-led line after the header
    and the `|---` separator; `—` (em dash) means an empty cell. Multi-valued
    cells (github, other handles, domains) are comma-separated. Other-handle
    cells are `service:handle` pairs — we take the handle after the colon.
    """
    names: set[str] = set()
    handles: set[str] = set()
    domains: set[str] = set()
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        # Skip the header row and the |---|---| separator row.
        if cells[0].lower() == "name" or set(cells[0]) <= {"-", ":"}:
            continue
        if len(cells) < 4:
            continue
        name, github, other, doms = cells[0], cells[1], cells[2], cells[3]
        if name and name != "—":
            names.add(name)
        for h in _split_cell(github):
            handles.add(h)
        for pair in _split_cell(other):
            handles.add(pair.split(":", 1)[1] if ":" in pair else pair)
        for d in _split_cell(doms):
            domains.add(d.lower())
    return Roster(names=frozenset(names), handles=frozenset(handles),
                  domains=frozenset(domains))


def _split_cell(cell: str) -> list[str]:
    """Comma-split a table cell; `—` (or empty) means none."""
    if not cell or cell == "—":
        return []
    return [part.strip() for part in cell.split(",") if part.strip()]


def _domain_ok(domain: str, roster: Roster) -> bool:
    """A domain passes if it's RFC 2606 reserved, roster-declared, or
    explicitly allowlisted."""
    domain = domain.lower()
    if domain in _RESERVED_EXACT or domain in _FICTIONAL_DOMAIN_ALLOWLIST:
        return True
    if any(domain == suf[1:] or domain.endswith(suf) for suf in _RESERVED_SUFFIXES):
        return True
    if domain in roster.domains:
        return True
    # github.com is a URL host, handled by the URL check — never a free domain.
    return False


@dataclass
class _Violation:
    fixture_id: str
    slot: str
    value: str

    def __str__(self) -> str:
        return f"[{self.fixture_id}] {self.slot}: {self.value!r}"


def _strings(node, path: str = ""):
    """Yield (json-path, key, string-value) for every string in the tree, so a
    failure message can name the slot that leaked."""
    if isinstance(node, dict):
        for key, val in node.items():
            sub = f"{path}.{key}" if path else key
            if isinstance(val, str):
                yield sub, key, val
            else:
                yield from _strings(val, sub)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _strings(item, f"{path}[{i}]")


def _check_one(fixture_id: str, envelope: dict, roster: Roster) -> list[_Violation]:
    """Extract every identity-bearing string from one fixture envelope and
    return the violations (empty list = clean)."""
    out: list[_Violation] = []
    for jpath, key, val in _strings(envelope):
        # Name-bearing fields must be roster members verbatim.
        if key in _NAME_KEYS and val and not val.startswith("http"):
            if val not in roster.names:
                out.append(_Violation(fixture_id, f"name@{jpath}", val))
        # Names embedded in CONTENT prose (the templated name_match / display-name
        # forms), not just the structured _NAME_KEYS fields (finding #10).
        for rx in _CONTENT_NAME_RES:
            for nm in rx.findall(val):
                if nm not in roster.names:
                    out.append(_Violation(fixture_id, f"content-name@{jpath}", nm))
        # @-addresses: the domain part must pass the domain rule.
        for addr in _ADDRESS_RE.findall(val):
            dom = addr.split("@", 1)[1]
            if not _domain_ok(dom, roster):
                out.append(_Violation(fixture_id, f"address@{jpath}", addr))
        # URLs: github.com/<roster-handle> or a passing-domain host.
        for url in _URL_RE.findall(val):
            host = re.sub(r"^https?://", "", url).split("/", 1)[0].split(":", 1)[0]
            if host == _GITHUB_HOST:
                m = _GITHUB_PATH_RE.search(url)
                handle = m.group(1) if m else None
                if handle not in roster.handles:
                    out.append(_Violation(fixture_id, f"url-handle@{jpath}", url))
            elif not _domain_ok(host, roster):
                out.append(_Violation(fixture_id, f"url-host@{jpath}", url))
        # Bare domains (no scheme, not part of an address): must pass too. We
        # skip github.com here — its handle is validated by the URL check above.
        for dom in _DOMAIN_RE.findall(val):
            if dom == _GITHUB_HOST or "@" + dom in val:
                continue
            if not _domain_ok(dom, roster):
                out.append(_Violation(fixture_id, f"domain@{jpath}", dom))
        # github.com/<handle> references anywhere (content prose, not just URLs).
        for handle in _GITHUB_PATH_RE.findall(val):
            if handle not in roster.handles:
                out.append(_Violation(fixture_id, f"handle@{jpath}", handle))
    return out


def scan_fixtures(dir_path: Path, roster: Roster) -> list[_Violation]:
    """The reusable checker: load every *.json in dir_path and collect privacy
    violations across all of them. Pointed at the committed fixtures by the gate
    and at tmp_path by the negative self-test."""
    violations: list[_Violation] = []
    for path in sorted(dir_path.glob("*.json")):
        envelope = json.loads(path.read_text())
        fixture_id = envelope.get("id", path.stem)
        violations.extend(_check_one(fixture_id, envelope, roster))
    return violations


def test_committed_fixtures_leak_no_real_identities():
    """The gate: every committed fixture's names, addresses, domains, URLs, and
    handles are fictional-roster / RFC-2606 / allowlisted. Any other value
    fails, naming the fixture id + offending value + slot."""
    roster = parse_roster()
    violations = scan_fixtures(FIXTURES_DIR, roster)
    assert not violations, "privacy leak in committed fixtures:\n" + \
        "\n".join(str(v) for v in violations)


def test_every_roster_handle_is_snoop_fixture_namespaced():
    """Defense in depth: every handle the roster vouches for is snoop-fixture-
    prefixed, so a typo in personas.md can't quietly admit a real account."""
    roster = parse_roster()
    bad = [h for h in roster.handles if not h.startswith(_HANDLE_PREFIX)]
    assert not bad, f"roster handles missing {_HANDLE_PREFIX!r} prefix: {bad}"


def test_every_roster_domain_passes_the_domain_rule():
    """Every roster-declared domain is itself RFC 2606 reserved (or allowlisted)
    — the roster can't license a real domain into a fixture by listing it."""
    roster = parse_roster()
    bad = [d for d in roster.domains
           if not (d in _RESERVED_EXACT or d in _FICTIONAL_DOMAIN_ALLOWLIST
                   or any(d.endswith(suf) for suf in _RESERVED_SUFFIXES))]
    assert not bad, f"roster domains not RFC 2606 reserved/allowlisted: {bad}"


def test_negative_self_test_a_leaking_fixture_fails_the_gate(tmp_path):
    """A gate never seen catching a leak is trusted on faith (eng review issue
    8). Seed a deliberately-leaking fixture — a real-looking address, domain,
    name, and unrostered handle — into tmp_path and assert the checker FAILS,
    naming each leaked slot."""
    leaking = {
        "id": "leak-probe",
        "suite": "regression",
        "bundle": {
            "schema": 2,
            "warnings": [],
            "person": {"name": "Peter Steinberger",  # real-looking, unrostered
                       "ambiguity": "single_plausible_match"},
            "observations": [
                {"id": "o1", "type": "github_handle",
                 "content": "github handle: realuser (github.com/realuser)",
                 "source_url": "https://github.com/realuser"},
                {"id": "o2", "type": "email_candidate",
                 "content": "candidate email: ceo@acme-corp.com",
                 "source_url": "https://acme-corp.com/contact",
                 "data": {"address": "ceo@acme-corp.com"}},
            ],
            "sensors": [],
        },
        "labels": {"must_emit": [], "must_not_emit": [], "banner_required": False,
                   "forbidden_verdicts": [], "rubric_notes": "leak probe"},
    }
    (tmp_path / "leak-probe.json").write_text(json.dumps(leaking))
    roster = parse_roster()
    violations = scan_fixtures(tmp_path, roster)
    slots = {v.slot.split("@", 1)[0] for v in violations}
    # The leak must trip the name, address, domain, URL-host, and handle checks.
    assert "name" in slots, f"name leak missed; caught: {violations}"
    assert "address" in slots, f"address leak missed; caught: {violations}"
    assert any(s in slots for s in ("domain", "url-host")), \
        f"domain leak missed; caught: {violations}"
    assert any(s in slots for s in ("handle", "url-handle")), \
        f"handle leak missed; caught: {violations}"


def test_negative_self_test_a_real_name_in_content_prose_fails(tmp_path):
    """finding #10: names hide in observation content prose (the templated
    'name_match = <Name>' anchor and 'google_display_name="<Name>"' mirror), not
    just the _NAME_KEYS fields. A real, unrostered name there must trip the gate —
    the old checker only validated the three structured name-key fields, so a name
    living only in content leaked silently. person.name stays rostered (clean) so
    the content names are the sole signal."""
    leaking = {
        "id": "content-name-leak", "suite": "regression",
        "bundle": {
            "schema": 2, "warnings": [],
            "person": {"name": "Zinnia Voss-Calloway",  # rostered: name-key clean
                       "ambiguity": "single_plausible_match"},
            "observations": [
                {"id": "o1", "type": "anchor", "source_url": None,
                 "content": ("identity anchor validated: "
                             "github_name_match = Peter Steinberger")},
                {"id": "o2", "type": "email_candidate", "source_url": None,
                 "content": ('candidate email: x@brightforge.example '
                             '(google_display_name="Linus Torvalds", name_match=yes)'),
                 "data": {"address": "x@brightforge.example"}},
            ],
            "sensors": [],
        },
        "labels": {"must_emit": [], "must_not_emit": [], "banner_required": False,
                   "forbidden_verdicts": [], "rubric_notes": "content name leak"},
    }
    (tmp_path / "content-name-leak.json").write_text(json.dumps(leaking))
    roster = parse_roster()
    leaked = {v.value for v in scan_fixtures(tmp_path, roster)}
    assert "Peter Steinberger" in leaked, f"anchor-name leak missed: {leaked}"
    assert "Linus Torvalds" in leaked, f"display-name leak missed: {leaked}"


def test_negative_self_test_a_clean_fixture_passes(tmp_path):
    """Symmetry: a tmp_path fixture using only roster identities passes — the
    gate isn't failing everything indiscriminately (which would also be a gate
    you can't trust)."""
    clean = {
        "id": "clean-probe",
        "suite": "regression",
        "bundle": {
            "schema": 2, "warnings": [],
            "person": {"name": "Zinnia Voss-Calloway",
                       "ambiguity": "single_plausible_match"},
            "observations": [
                {"id": "o1", "type": "email_candidate",
                 "content": "candidate email: zinnia@brightforge.example",
                 "source_url": "https://github.com/snoop-fixture-zinnia",
                 "data": {"address": "zinnia@brightforge.example"}},
            ],
            "sensors": [],
        },
        "labels": {"must_emit": [], "must_not_emit": [], "banner_required": False,
                   "forbidden_verdicts": [], "rubric_notes": "clean probe"},
    }
    (tmp_path / "clean-probe.json").write_text(json.dumps(clean))
    roster = parse_roster()
    assert not scan_fixtures(tmp_path, roster)
