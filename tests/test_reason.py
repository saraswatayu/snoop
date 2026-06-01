"""Tests for lib/reason.py — the LLM-native reasoning step.

No network: a fake Anthropic client returns a canned structured payload. The
tests pin the two guarantees that make this safe — the call is TOOL-LESS, and
every returned fact is grounded against a real observation (lib.ground) — plus
the SDK call shape (model, adaptive thinking, cached instructions, structured
output).
"""

from __future__ import annotations

import json

import pytest

from lib import reason
from lib.schema import EmailCandidate, Employer, GitHubRepo, Person, Source
from datetime import datetime, timezone


NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _person():
    return Person(
        name="Alice Smith",
        handles={"github": "alice"},
        gh_name="Alice Smith",
        gh_company="Corp",
        employer=Employer(name="Corp", domains=["corp.com"]),
        gh_recent_repos=[GitHubRepo(
            name="alice/widget", description="a widget",
            html_url="https://github.com/alice/widget", pushed_at="2026-05-01T00:00:00Z",
        )],
        bound_anchors=[("github_name_match", "Alice Smith"),
                       ("github_employer_match", "Corp")],
        ambiguity="single_plausible_match",
    )


def _candidate():
    return EmailCandidate(
        address="alice@corp.com", belongs_to_person=0.8, smtp_verdict="verified",
        sources=[Source(type="gh_profile", url="https://github.com/alice",
                        observed_at=NOW, detail="profile email")],
    )


class _Usage:
    input_tokens = 100
    output_tokens = 50
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 80


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.usage = _Usage()


class _Messages:
    def __init__(self, outer):
        self._outer = outer

    def create(self, **kwargs):
        self._outer.calls.append(kwargs)
        return _Resp(json.dumps(self._outer.payload))


class FakeClient:
    """Stand-in for anthropic.Anthropic(); records create() kwargs."""
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.messages = _Messages(self)


# --- build_evidence ----------------------------------------------------------


def test_build_evidence_flattens_core_signals():
    obs = reason.build_evidence(_person(), [_candidate()])
    types = {o.type for o in obs}
    assert {"github_handle", "gh_profile", "github_repo", "email_candidate",
            "anchor", "employer"} <= types
    # ids are unique and contiguous
    assert [o.id for o in obs] == [f"o{i}" for i in range(1, len(obs) + 1)]
    blob = "\n".join(o.content for o in obs)
    assert "alice@corp.com" in blob and "alice/widget" in blob


def test_build_evidence_empty_candidates_still_describes_identity():
    obs = reason.build_evidence(_person(), [])
    assert any(o.type == "gh_profile" for o in obs)
    assert not any(o.type == "email_candidate" for o in obs)


# --- reason_profile ----------------------------------------------------------


def _payload(facts, summary="Alice Smith, engineer at Corp."):
    return {"summary": summary, "identity_confidence": 0.9, "facts": facts}


def test_reason_profile_grounds_and_maps():
    # one fact cites a real candidate obs; one cites a bogus id and must drop.
    person, cand = _person(), _candidate()
    obs = reason.build_evidence(person, [cand])
    email_id = next(o.id for o in obs if o.type == "email_candidate")
    payload = _payload([
        {"kind": "email", "label": "", "value": "alice@corp.com", "detail": "",
         "confidence": 0.9, "evidence_ids": [email_id], "reasoning": "profile email"},
        {"kind": "work_item", "label": "", "value": "ghost", "detail": "",
         "confidence": 0.8, "evidence_ids": ["o999"], "reasoning": "hallucinated"},
    ])
    client = FakeClient(payload)

    profile = reason.reason_profile(person, [cand], client=client)

    assert profile.summary.startswith("Alice Smith")
    assert profile.identity_confidence == 0.9
    kinds = [f.kind for f in profile.facts]
    assert kinds == ["email"]                       # the o999 fact was dropped
    assert profile.facts[0].verified is True        # value appears in the cited obs
    assert profile.usage["input_tokens"] == 100


def test_reason_profile_call_is_tool_less():
    client = FakeClient(_payload([]))
    reason.reason_profile(_person(), [_candidate()], client=client)
    kwargs = client.calls[0]
    assert "tools" not in kwargs            # the exfiltration control
    assert "tool_choice" not in kwargs


def test_reason_profile_uses_opus_with_cached_instructions():
    client = FakeClient(_payload([]))
    reason.reason_profile(_person(), [], client=client)
    kwargs = client.calls[0]
    assert kwargs["model"] == "claude-opus-4-8"
    assert kwargs["thinking"] == {"type": "adaptive"}
    # structured output + effort both live under output_config
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    assert kwargs["output_config"]["effort"] == "high"
    # frozen instruction block is cached (prompt-caching prefix)
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "snoop" in kwargs["system"][0]["text"]


def test_reason_profile_appends_extra_observations_with_unique_ids():
    person, cand = _person(), _candidate()
    extra = [reason.Observation(id="x", type="web_search",
                                content="talk on widgets", source_url="https://conf/x")]
    client = FakeClient(_payload([]))
    profile = reason.reason_profile(person, [cand], client=client, extra_observations=extra)
    ids = [o.id for o in profile.observations]
    assert len(ids) == len(set(ids))                 # all unique
    assert any(o.type == "web_search" for o in profile.observations)
    # the rendered evidence text in the call includes the appended obs
    assert "talk on widgets" in client.calls[0]["messages"][0]["content"]


def test_reasoning_unavailable_propagates(monkeypatch):
    def _boom():
        raise reason.ReasoningUnavailable("no creds")
    monkeypatch.setattr(reason, "_default_client", _boom)
    with pytest.raises(reason.ReasoningUnavailable):
        reason.reason_profile(_person(), [], client=None)
