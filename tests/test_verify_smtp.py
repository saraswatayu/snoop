"""Tests for lib/verify_smtp.py.

Mocks the DomainProbe class to avoid real SMTP. Cover the structural
changes from the post-review consensus:
- Personal-provider domains never probed
- Inconclusive verdict carries zero information (handled by scorer; here
  we just verify the verdict + mx_provider get plumbed correctly)
- Per-domain daily budget cap
- One connection per domain (catch-all sentinel runs once)
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from lib.schema import EmailCandidate
from lib.verify_smtp import (
    ProbeBudget,
    SYNTAX_RE,
    detect_provider,
    verify_candidates,
)


class FakeProbe:
    """In-memory DomainProbe replacement. Tracks construction count per
    domain so tests can verify connection reuse."""

    instances_per_domain: dict[str, int] = {}
    # Per-test override: {(domain, address): (verdict, provider, code)}
    responses: dict[tuple[str, str], tuple[str, str | None, int | None]] = {}
    # Per-test override: {domain: error_string} (sets self.error)
    domain_errors: dict[str, str] = {}

    def __init__(self, domain, mail_from, timeout):
        self.domain = domain
        self.mail_from = mail_from
        self.timeout = timeout
        self.error = FakeProbe.domain_errors.get(domain)
        self.provider = "google" if "openai" in domain else "other"
        FakeProbe.instances_per_domain[domain] = (
            FakeProbe.instances_per_domain.get(domain, 0) + 1
        )

    def test(self, email):
        key = (self.domain, email)
        if key in FakeProbe.responses:
            verdict, provider, code = FakeProbe.responses[key]
            if provider is None:
                provider = self.provider
            return verdict, provider, code
        if self.error:
            return "unprobed", self.provider, None
        return "inconclusive", self.provider, None  # default

    def close(self):
        pass

    @classmethod
    def reset(cls):
        cls.instances_per_domain = {}
        cls.responses = {}
        cls.domain_errors = {}


# ---- detect_provider --------------------------------------------------------


def test_detect_provider_recognizes_google():
    assert detect_provider("aspmx.l.google.com") == "google"
    assert detect_provider("googlemail.l.google.com") == "google"


def test_detect_provider_recognizes_microsoft():
    assert detect_provider("openai-com.mail.protection.outlook.com") == "microsoft"


def test_detect_provider_falls_back_to_other():
    assert detect_provider("mail.selfhosted.io") == "other"


def test_detect_provider_handles_empty():
    assert detect_provider("") == "other"


# ---- is_google_hosted -------------------------------------------------------


def test_is_google_hosted_recognizes_native_google():
    from lib.verify_smtp import is_google_hosted
    assert is_google_hosted("google.com") is True


def test_is_google_hosted_recognizes_workspace_mx(monkeypatch):
    from lib import verify_smtp
    monkeypatch.setattr(verify_smtp, "get_mx",
                        lambda d: ("aspmx.l.google.com", None))
    assert verify_smtp.is_google_hosted("formation.bio") is True


def test_is_google_hosted_false_for_microsoft_mx(monkeypatch):
    from lib import verify_smtp
    monkeypatch.setattr(verify_smtp, "get_mx",
                        lambda d: ("openai-com.mail.protection.outlook.com", None))
    assert verify_smtp.is_google_hosted("openai.com") is False


def test_is_google_hosted_false_when_no_mx(monkeypatch):
    from lib import verify_smtp
    monkeypatch.setattr(verify_smtp, "get_mx",
                        lambda d: (None, "no MX records"))
    assert verify_smtp.is_google_hosted("nomx.example") is False


def test_is_google_hosted_false_when_dns_errors(monkeypatch):
    from lib import verify_smtp
    monkeypatch.setattr(verify_smtp, "get_mx",
                        lambda d: (None, "DNS timeout"))
    assert verify_smtp.is_google_hosted("offline.example") is False


# ---- SYNTAX_RE --------------------------------------------------------------


def test_syntax_re_accepts_normal():
    assert SYNTAX_RE.match("pete@openai.com")
    assert SYNTAX_RE.match("pete.steinberger+work@openai.com")


def test_syntax_re_rejects_malformed():
    assert not SYNTAX_RE.match("not-an-email")
    assert not SYNTAX_RE.match("@example.com")
    assert not SYNTAX_RE.match("foo@")


# ---- personal-provider skip -------------------------------------------------


def test_verify_skips_gmail_addresses():
    FakeProbe.reset()
    candidates = [
        EmailCandidate(address="pete@gmail.com"),
        EmailCandidate(address="pete@openai.com"),
    ]
    verify_candidates(candidates, _probe_factory=FakeProbe)  # type: ignore[arg-type]
    # Gmail one: never probed
    assert candidates[0].smtp_verdict == "unprobed"
    assert candidates[0].is_personal_provider is True
    # OpenAI one: probed
    assert candidates[1].smtp_verdict == "inconclusive"
    # FakeProbe was only constructed for openai.com
    assert FakeProbe.instances_per_domain == {"openai.com": 1}


def test_verify_skips_all_major_personal_providers():
    FakeProbe.reset()
    cands = [
        EmailCandidate(address=f"x@{d}")
        for d in ("gmail.com", "yahoo.com", "icloud.com",
                  "outlook.com", "hotmail.com", "protonmail.com",
                  "proton.me", "fastmail.com", "aol.com")
    ]
    verify_candidates(cands, _probe_factory=FakeProbe)  # type: ignore[arg-type]
    for c in cands:
        assert c.smtp_verdict == "unprobed", f"{c.address} should be unprobed"
        assert c.is_personal_provider is True
    assert FakeProbe.instances_per_domain == {}


def test_verify_force_probes_personal_provider_when_flag_off():
    """skip_personal_providers=False forces probing — useful for testing
    self-hosted-like providers."""
    FakeProbe.reset()
    FakeProbe.responses = {("gmail.com", "x@gmail.com"): ("inconclusive", "google", None)}
    cands = [EmailCandidate(address="x@gmail.com")]
    verify_candidates(cands, skip_personal_providers=False, _probe_factory=FakeProbe)  # type: ignore[arg-type]
    assert cands[0].smtp_verdict == "inconclusive"


# ---- connection reuse / catch-all sentinel ---------------------------------


def test_verify_uses_one_probe_per_domain_across_batch():
    """Three candidates on openai.com → ONE DomainProbe construction
    (one catch-all sentinel + connection reuse)."""
    FakeProbe.reset()
    cands = [
        EmailCandidate(address="pete@openai.com"),
        EmailCandidate(address="peter@openai.com"),
        EmailCandidate(address="p.steinberger@openai.com"),
    ]
    verify_candidates(cands, _probe_factory=FakeProbe)  # type: ignore[arg-type]
    assert FakeProbe.instances_per_domain == {"openai.com": 1}


def test_verify_handles_multi_domain_batch():
    """Two domains → two probes, each constructed once."""
    FakeProbe.reset()
    cands = [
        EmailCandidate(address="pete@openai.com"),
        EmailCandidate(address="sam@anthropic.com"),
    ]
    verify_candidates(cands, _probe_factory=FakeProbe)  # type: ignore[arg-type]
    assert FakeProbe.instances_per_domain == {"openai.com": 1, "anthropic.com": 1}


# ---- verdict propagation ----------------------------------------------------


def test_verify_propagates_verdict_to_candidate():
    FakeProbe.reset()
    FakeProbe.responses = {
        ("openai.com", "pete@openai.com"): ("verified", "google", 250),
        ("openai.com", "bogus@openai.com"): ("invalid", "google", 550),
    }
    cands = [
        EmailCandidate(address="pete@openai.com"),
        EmailCandidate(address="bogus@openai.com"),
    ]
    verify_candidates(cands, _probe_factory=FakeProbe)  # type: ignore[arg-type]
    assert cands[0].smtp_verdict == "verified"
    assert cands[0].mx_provider == "google"
    assert cands[1].smtp_verdict == "invalid"


def test_verify_propagates_catch_all_to_unprobed_candidates_on_same_domain():
    """When the catch-all sentinel returns 250 for the first candidate, ALL
    other candidates on the same domain inherit catch_all (with the same
    mx_provider) — they're skipped to save probes, but the verdict
    propagates. Previously these were left "unprobed", which produced
    misleading per-candidate deliverable scores in the rendered output
    (the user's run review caught this)."""
    FakeProbe.reset()
    FakeProbe.responses = {
        ("acme.com", "a@acme.com"): ("catch_all", "google", 250),
    }
    cands = [
        EmailCandidate(address="a@acme.com"),
        EmailCandidate(address="b@acme.com"),
        EmailCandidate(address="c@acme.com"),
    ]
    verify_candidates(cands, _probe_factory=FakeProbe)  # type: ignore[arg-type]
    # All three inherit catch_all + the same mx_provider
    for c in cands:
        assert c.smtp_verdict == "catch_all", (
            f"{c.address} should inherit catch_all from sentinel"
        )
        assert c.mx_provider == "google"
    # Only the first one actually got probed — the others were skipped
    assert FakeProbe.instances_per_domain == {"acme.com": 1}


def test_verify_propagates_unprobed_for_mx_failure():
    """When the SMTP/MX setup fails for a domain, subsequent candidates on
    that domain should be marked `unprobed` (we don't have a verdict to
    propagate — only the knowledge that we couldn't reach the MX)."""
    FakeProbe.reset()
    # First candidate triggers a domain error (no MX, or SMTP connect fail)
    FakeProbe.domain_errors = {"dead-mx.com": "no usable MX"}
    cands = [
        EmailCandidate(address="a@dead-mx.com"),
        EmailCandidate(address="b@dead-mx.com"),
    ]
    verify_candidates(cands, _probe_factory=FakeProbe)  # type: ignore[arg-type]
    # First: probed, MX error → unprobed verdict
    assert cands[0].smtp_verdict == "unprobed"
    # Second: skipped due to dead domain, also unprobed (not catch_all)
    assert cands[1].smtp_verdict == "unprobed"


# ---- malformed addresses ---------------------------------------------------


def test_verify_marks_syntactically_invalid_as_invalid():
    FakeProbe.reset()
    cands = [EmailCandidate(address="not-an-email")]
    verify_candidates(cands, _probe_factory=FakeProbe)  # type: ignore[arg-type]
    assert cands[0].smtp_verdict == "invalid"
    assert FakeProbe.instances_per_domain == {}


def test_verify_marks_empty_address_as_unprobed():
    FakeProbe.reset()
    cands = [EmailCandidate(address="")]
    verify_candidates(cands, _probe_factory=FakeProbe)  # type: ignore[arg-type]
    assert cands[0].smtp_verdict == "unprobed"


# ---- ProbeBudget ------------------------------------------------------------


def test_budget_allows_when_under_limit():
    with tempfile.TemporaryDirectory() as td:
        b = ProbeBudget(per_domain=3, state_path=Path(td) / "budget.json")
        assert b.allow("acme.com")
        b.record("acme.com")
        assert b.allow("acme.com")
        b.record("acme.com")
        b.record("acme.com")
        assert not b.allow("acme.com")
        # Different domain has its own counter
        assert b.allow("other.com")


def test_budget_persists_across_instances():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "budget.json"
        b1 = ProbeBudget(per_domain=2, state_path=path)
        b1.record("acme.com")
        b1.record("acme.com")
        # New ProbeBudget should see the prior counts
        b2 = ProbeBudget(per_domain=2, state_path=path)
        assert not b2.allow("acme.com")


def test_budget_writes_state_with_0600_perms():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "budget.json"
        b = ProbeBudget(per_domain=2, state_path=path)
        b.record("acme.com")
        import os
        mode = os.stat(path).st_mode & 0o777
        assert mode == 0o600


def test_budget_exhausted_domains_left_unprobed():
    FakeProbe.reset()
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "budget.json"
        b = ProbeBudget(per_domain=1, state_path=path)
        cands = [
            EmailCandidate(address="a@acme.com"),
            EmailCandidate(address="b@acme.com"),
        ]
        FakeProbe.responses = {("acme.com", "a@acme.com"): ("verified", "other", 250)}
        verify_candidates(cands, budget=b, _probe_factory=FakeProbe)  # type: ignore[arg-type]
        # First probe succeeds, second hits budget cap
        assert cands[0].smtp_verdict == "verified"
        assert cands[1].smtp_verdict == "unprobed"


# ---- mx_provider exposure for renderer hints --------------------------------


def test_verify_sets_mx_provider_for_renderer():
    """The renderer needs mx_provider to explain inconclusive: 'M365 blocks
    RCPT' is a different message than 'self-hosted RCPT inconclusive.'"""
    FakeProbe.reset()
    FakeProbe.responses = {
        ("openai.com", "pete@openai.com"): ("inconclusive", "microsoft", None),
    }
    cands = [EmailCandidate(address="pete@openai.com")]
    verify_candidates(cands, _probe_factory=FakeProbe)  # type: ignore[arg-type]
    assert cands[0].smtp_verdict == "inconclusive"
    assert cands[0].mx_provider == "microsoft"
