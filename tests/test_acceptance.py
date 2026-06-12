"""ENG-7 end-to-end acceptance test.

Drives the EXACT documented SKILL.md Tier-1 worked example through the real CLI
with the network monkeypatched — Step 2 (`--observations` via `--out`) then Step 4
(`--ground --observations-file`) — and asserts the card a user copies actually
runs green. This is the one test that pins the documented flow itself, not a
synthetic smoke target; if SKILL.md's worked example drifts from the code, this
fails.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

import snoop  # noqa: E402
from lib.schema import EmailCandidate, Employer, Person, ResolverResult, Source  # noqa: E402

NOW = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)

# The documented Step-2 plan, verbatim from SKILL.md.
PLAN = json.dumps({
    "name": "Peter Steinberger",
    "handles": {"github": "steipete"},
    "personal_domains": ["steipete.com"],
    "employer": {"name": "OpenAI", "domains": ["openai.com"]},
})


def _wire_documented_pipeline(monkeypatch):
    """Monkeypatch every sensor to the canned readings of the documented bundle:
    Peter Steinberger, github-bound, pete@openai.com observed in git commits on
    the employer domain (so it BINDS under ENG-8) and SMTP-verified."""
    monkeypatch.setattr(snoop, "resolve_person", lambda name, **kw: Person(
        name="Peter Steinberger", handles={"github": "steipete"},
        personal_domains=["steipete.com"],
        employer=Employer(name="OpenAI", domains=["openai.com"]),
        ambiguity="single_plausible_match",
        bound_anchors=[("github_name_match", "Peter Steinberger"),
                       ("github_handle_exists", "steipete")],
    ))

    def git_emails(*a, **kw):
        return ResolverResult(
            resolver="git_emails",
            candidates=[EmailCandidate(
                address="pete@openai.com", employer_match=True,
                sources=[Source(type="git_commit", url="https://github.com/steipete",
                                observed_at=NOW, detail="commit author email")])],
            status="ok")

    empty = ResolverResult(resolver="x", candidates=[], status="empty")
    monkeypatch.setattr(snoop, "fetch_git_emails", git_emails)
    monkeypatch.setattr(snoop, "fetch_gh_profile", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_personal_site", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_pattern_candidates", lambda *a, **kw: empty)
    monkeypatch.setattr(snoop, "fetch_recent_repos", lambda *a, **kw: [])

    def verify(cands, **kw):
        for c in cands:
            c.smtp_verdict = "verified"
            c.mx_provider = "other"
    monkeypatch.setattr(snoop, "verify_candidates", verify)

    # Stub the Google existence check to a no-op: this machine may have a live
    # Chrome/Google session, and the real probe would make a nondeterministic
    # network call to Google's People API for pete@openai.com (a Workspace domain).
    # A not_found there would mark the candidate dead and skip SMTP — the documented
    # flow is the SMTP-verified path, so we keep Google out of it here.
    monkeypatch.setattr(snoop, "fetch_google_account",
                        lambda cands, **kw: ResolverResult(
                            resolver="google_account", candidates=[], status="empty"))


def test_documented_tier1_worked_example_runs_green(monkeypatch, capsys, tmp_path):
    _wire_documented_pipeline(monkeypatch)
    bundle_path = tmp_path / "snoop-obs.json"

    # Step 2 — sense (the documented command shape, with --out).
    rc = snoop.main(["Peter Steinberger", "--person-plan", PLAN,
                     "--allow-google-account", "--no-pgp", "--out", str(bundle_path)])
    assert rc == 0
    out = capsys.readouterr().out
    # --out prints the ready-to-run Step-4 command pointer.
    assert "--ground --observations-file" in out

    bundle = json.loads(bundle_path.read_text())
    email_obs = next(o for o in bundle["observations"]
                     if o["type"] == "email_candidate" and "pete@openai.com" in o["content"])
    gh_obs = next(o for o in bundle["observations"] if o["type"] == "github_handle")
    # The bound, SMTP-verified candidate is in the bundle as verified.
    assert email_obs["data"]["smtp"] == "verified"

    # Step 3 — the host reasons (here, the test) and cites the real observation ids.
    facts = {
        "person": bundle["person"],
        "summary": "Peter Steinberger — best reached at his OpenAI work address.",
        "facts": [
            {"kind": "email", "label": "", "value": "pete@openai.com",
             "detail": "smtp verified", "confidence": 0.95,
             "evidence_ids": [email_obs["id"]], "reasoning": "git commit, SMTP 250"},
            {"kind": "social_link", "label": "github", "value": "github.com/steipete",
             "detail": "", "confidence": 0.95, "evidence_ids": [gh_obs["id"]],
             "reasoning": "validated handle"},
        ],
    }

    # Step 4 — ground (the documented --observations-file command).
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(facts)))
    rc = snoop.main(["--ground", "--observations-file", str(bundle_path)])
    assert rc == 0
    card = capsys.readouterr().out
    assert "pete@openai.com" in card
    assert "github.com/steipete" in card
    assert "(unverified)" not in card        # every documented fact grounds


def test_documented_flow_drops_a_hallucinated_citation(monkeypatch, capsys, tmp_path):
    """Negative acceptance: a fact citing an observation id the bundle never
    emitted is dropped by the real CLI's --ground (the namesake gate end-to-end)."""
    _wire_documented_pipeline(monkeypatch)
    bundle_path = tmp_path / "snoop-obs.json"
    snoop.main(["Peter Steinberger", "--person-plan", PLAN, "--no-pgp",
                "--out", str(bundle_path)])
    capsys.readouterr()

    facts = {
        "person": json.loads(bundle_path.read_text())["person"],
        "summary": "Peter Steinberger.",
        "facts": [{
            "kind": "email", "label": "", "value": "ghost@evil.com", "detail": "",
            "confidence": 0.9, "evidence_ids": ["o999"], "reasoning": "hallucinated",
        }],
    }
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(facts)))
    rc = snoop.main(["--ground", "--observations-file", str(bundle_path)])
    assert rc == 0
    assert "ghost@evil.com" not in capsys.readouterr().out
