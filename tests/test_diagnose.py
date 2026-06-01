"""Tests for lib/diagnose.py.

Tests use monkeypatch to simulate missing / present / failing
capabilities so we don't depend on what's actually installed on the
test machine.
"""

from __future__ import annotations

import lib.diagnose as diag
from lib.diagnose import (
    Capability,
    diagnose,
    format_report,
)


# ---- _probe_gh_cli ----------------------------------------------------------


def test_gh_cli_missing(monkeypatch):
    monkeypatch.setattr(diag.shutil, "which", lambda x: None if x == "gh" else "/usr/bin/x")
    cap = diag._probe_gh_cli()
    assert cap.status == "missing"
    assert "gh" in cap.detail.lower()
    assert "anonymous" in cap.impact.lower()


def test_gh_cli_present_and_authed(monkeypatch):
    import subprocess
    monkeypatch.setattr(diag.shutil, "which", lambda x: "/usr/local/bin/gh")
    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""
    monkeypatch.setattr(diag.subprocess, "run", lambda *a, **kw: FakeResult())
    cap = diag._probe_gh_cli()
    assert cap.status == "ok"
    assert "5000" in cap.detail


def test_gh_cli_present_but_not_authed(monkeypatch):
    monkeypatch.setattr(diag.shutil, "which", lambda x: "/usr/local/bin/gh")
    class FakeResult:
        returncode = 1
        stdout = ""
        stderr = "not logged in"
    monkeypatch.setattr(diag.subprocess, "run", lambda *a, **kw: FakeResult())
    cap = diag._probe_gh_cli()
    assert cap.status == "degraded"
    assert "auth login" in cap.detail.lower()


def test_gh_cli_subprocess_error_degrades_gracefully(monkeypatch):
    import subprocess
    monkeypatch.setattr(diag.shutil, "which", lambda x: "/usr/local/bin/gh")

    def bombs(*a, **kw):
        raise subprocess.SubprocessError("simulated")
    monkeypatch.setattr(diag.subprocess, "run", bombs)
    cap = diag._probe_gh_cli()
    assert cap.status == "degraded"


# ---- _probe_anon_github -----------------------------------------------------


def test_anon_github_reachable(monkeypatch):
    import urllib.request

    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b"zen"

    def fake_urlopen(req, timeout=None):
        return FakeResp()
    monkeypatch.setattr(diag.urllib.request, "urlopen", fake_urlopen)
    cap = diag._probe_anon_github()
    assert cap.status == "ok"


def test_anon_github_unreachable(monkeypatch):
    import urllib.error
    def bombs(*a, **kw):
        raise urllib.error.URLError("network down")
    monkeypatch.setattr(diag.urllib.request, "urlopen", bombs)
    cap = diag._probe_anon_github()
    assert cap.status == "missing"
    assert "unreachable" in cap.detail.lower()


# ---- _probe_dnspython -------------------------------------------------------


def test_dnspython_present():
    # We're in a venv that has dnspython per the existing requirements.txt.
    cap = diag._probe_dnspython()
    assert cap.status == "ok"


def test_dnspython_missing(monkeypatch):
    import builtins
    orig = builtins.__import__
    def bomb_import(name, *args, **kwargs):
        if name.startswith("dns"):
            raise ImportError("simulated")
        return orig(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", bomb_import)
    cap = diag._probe_dnspython()
    assert cap.status == "missing"
    assert "verify_smtp" in cap.impact.lower()


# ---- _probe_idna ------------------------------------------------------------


def test_idna_missing_is_degraded_not_missing(monkeypatch):
    """idna lib being absent is non-fatal — stdlib has a 2003 fallback."""
    import builtins
    orig = builtins.__import__
    def bomb_import(name, *args, **kwargs):
        if name == "idna":
            raise ImportError("simulated")
        return orig(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", bomb_import)
    cap = diag._probe_idna()
    # Degraded, not missing — stdlib fallback still works
    assert cap.status == "degraded"


# ---- _probe_whois_binary ----------------------------------------------------


def test_whois_binary_missing(monkeypatch):
    monkeypatch.setattr(diag.shutil, "which", lambda x: None if x == "whois" else "/usr/bin/x")
    cap = diag._probe_whois_binary()
    assert cap.status == "missing"


def test_whois_binary_present(monkeypatch):
    monkeypatch.setattr(diag.shutil, "which", lambda x: "/usr/bin/whois")
    cap = diag._probe_whois_binary()
    assert cap.status == "ok"


# ---- _probe_snoop_state_dir -------------------------------------------------


def test_snoop_state_dir_writable(tmp_path, monkeypatch):
    """Use a tmp home so we don't litter the real ~/.snoop."""
    monkeypatch.setattr(diag.Path, "home", classmethod(lambda cls: tmp_path))
    cap = diag._probe_snoop_state_dir()
    assert cap.status == "ok"
    assert (tmp_path / ".snoop").exists()


def test_snoop_state_dir_unwritable(tmp_path, monkeypatch):
    """If mkdir bombs, we get a missing status."""
    import os as _os
    monkeypatch.setattr(diag.Path, "home", classmethod(lambda cls: tmp_path))

    orig_mkdir = diag.Path.mkdir
    def bombs(self, *a, **kw):
        raise OSError("simulated")
    monkeypatch.setattr(diag.Path, "mkdir", bombs)
    cap = diag._probe_snoop_state_dir()
    assert cap.status == "missing"


# ---- diagnose() -------------------------------------------------------------


def test_diagnose_returns_all_probes():
    caps = diagnose()
    names = {c.name for c in caps}
    expected = {
        "gh_cli", "anon_github", "dnspython", "idna", "whois",
        "google_account", "profile_search", "snoop_state_dir",
    }
    assert names == expected


def test_profile_search_reports_host_model_source():
    from lib.diagnose import _probe_profile_search
    cap = _probe_profile_search()
    assert cap.name == "profile_search"
    assert cap.status == "degraded"
    assert "work_search_results" in cap.detail


# ---- _probe_google_account --------------------------------------------------


def test_google_account_missing_when_no_cookies(monkeypatch):
    import lib.chrome_cookies as cc
    monkeypatch.setattr(cc, "list_supported_browsers", lambda: ["chrome"])
    monkeypatch.setattr(cc, "get_google_cookies", lambda: {})
    cap = diag._probe_google_account()
    assert cap.status == "missing"
    assert "no Google cookies" in cap.detail


def test_google_account_missing_on_unsupported_platform(monkeypatch):
    import lib.chrome_cookies as cc
    monkeypatch.setattr(cc, "list_supported_browsers", lambda: [])
    cap = diag._probe_google_account()
    assert cap.status == "missing"
    assert "not supported" in cap.detail.lower()


def test_google_account_degraded_when_cookies_present_but_no_sapisid(monkeypatch):
    import lib.chrome_cookies as cc
    monkeypatch.setattr(cc, "list_supported_browsers", lambda: ["chrome"])
    monkeypatch.setattr(cc, "get_google_cookies",
                        lambda: {"SID": "s", "SSID": "ss"})  # no SAPISID-family
    cap = diag._probe_google_account()
    assert cap.status == "degraded"
    assert "SAPISID" in cap.detail


def test_google_account_ok_when_sapisid_present(monkeypatch):
    import lib.chrome_cookies as cc
    monkeypatch.setattr(cc, "list_supported_browsers", lambda: ["chrome"])
    monkeypatch.setattr(
        cc, "get_google_cookies",
        lambda: {"SID": "s", "SAPISID": "real-sapisid", "HSID": "h"},
    )
    cap = diag._probe_google_account()
    assert cap.status == "ok"
    assert "ready" in cap.detail.lower()


def test_google_account_ok_when_modern_secure_sapisid_present(monkeypatch):
    """__Secure-1PAPISID counts as a SAPISID-family cookie."""
    import lib.chrome_cookies as cc
    monkeypatch.setattr(cc, "list_supported_browsers", lambda: ["chrome"])
    monkeypatch.setattr(
        cc, "get_google_cookies",
        lambda: {"__Secure-1PAPISID": "modern-only"},
    )
    cap = diag._probe_google_account()
    assert cap.status == "ok"


def test_google_account_degraded_when_cookie_read_errors(monkeypatch):
    import lib.chrome_cookies as cc
    monkeypatch.setattr(cc, "list_supported_browsers", lambda: ["chrome"])
    def bombs():
        raise OSError("simulated")
    monkeypatch.setattr(cc, "get_google_cookies", bombs)
    cap = diag._probe_google_account()
    assert cap.status == "degraded"
    assert "errored" in cap.detail.lower()


def test_diagnose_returns_list_of_capability():
    caps = diagnose()
    for c in caps:
        assert isinstance(c, Capability)
        assert c.status in ("ok", "degraded", "missing")
        assert isinstance(c.detail, str) and c.detail
        assert isinstance(c.impact, str) and c.impact


# ---- format_report ----------------------------------------------------------


def test_format_report_handles_empty():
    out = format_report([])
    assert "no capabilities probed" in out


def test_format_report_shows_all_caps():
    caps = [
        Capability(name="x", status="ok", detail="works", impact="nothing"),
        Capability(name="y", status="missing", detail="absent", impact="something"),
    ]
    out = format_report(caps)
    assert "[OK]" in out and "[XX " in out
    assert "x:" in out and "y:" in out
    assert "works" in out and "absent" in out


def test_format_report_glyph_for_degraded():
    caps = [Capability(name="z", status="degraded", detail="meh", impact="meh")]
    out = format_report(caps)
    assert "[!!" in out
