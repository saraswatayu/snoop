"""Tests for lib/gh_profile.py.

Deterministic — both gh_caller (for /users/) and http_get (for raw README)
are injected so no network is required.
"""

from __future__ import annotations

from datetime import datetime, timezone

from lib.gh_profile import (
    _extract_emails_from_text,
    _is_extractable,
    fetch_gh_profile,
)


NOW = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)


def make_gh(routes):
    def caller(path):
        for key, resp in routes.items():
            if path.startswith(key):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected gh path: {path}")
    return caller


def make_http(routes):
    """A fake http_get that returns the body for matching URLs or None for 404."""
    def get(url):
        for key, resp in routes.items():
            if url.startswith(key):
                if isinstance(resp, Exception):
                    raise resp
                return resp  # may be None to simulate 404
        return None
    return get


# ---- _is_extractable --------------------------------------------------------


def test_is_extractable_accepts_real_addresses():
    assert _is_extractable("pete@openai.com")
    assert _is_extractable("hello@steipete.com")


def test_is_extractable_rejects_placeholders():
    assert not _is_extractable("test@example.com")
    assert not _is_extractable("a@localhost")
    assert not _is_extractable("b@test.invalid")


def test_is_extractable_rejects_noreply():
    assert not _is_extractable("noreply@github.com")
    assert not _is_extractable("do-not-reply@anywhere.com")


def test_is_extractable_rejects_malformed():
    assert not _is_extractable("not-an-email")
    assert not _is_extractable("@example.com")
    assert not _is_extractable("foo@")


# ---- _extract_emails_from_text ----------------------------------------------


def test_extract_prefers_mailto_when_present():
    """mailto: anchors are higher-precision than raw regex matches.
    When both exist for the same address, the mailto extraction wins."""
    text = '''
        Visit my <a href="mailto:pete@openai.com">site</a>.
        Or write to pete@openai.com directly.
    '''
    results = _extract_emails_from_text(text)
    assert results == [("pete@openai.com", "mailto")]


def test_extract_falls_back_to_regex_when_no_mailto():
    text = "Reach me at pete@openai.com any time."
    results = _extract_emails_from_text(text)
    assert results == [("pete@openai.com", "regex")]


def test_extract_dedups_multiple_occurrences():
    text = "pete@openai.com pete@openai.com pete@openai.com"
    results = _extract_emails_from_text(text)
    assert results == [("pete@openai.com", "regex")]


def test_extract_returns_multiple_distinct_addresses():
    text = "work pete@openai.com personal pete@gmail.com"
    results = _extract_emails_from_text(text)
    emails = {e for e, _ in results}
    assert emails == {"pete@openai.com", "pete@gmail.com"}


def test_extract_drops_noise_addresses():
    text = "Contact: hello@example.com (real one: pete@acme.com)"
    results = _extract_emails_from_text(text)
    assert results == [("pete@acme.com", "regex")]


def test_extract_handles_empty_or_none():
    assert _extract_emails_from_text("") == []
    assert _extract_emails_from_text("   ") == []


# ---- fetch_gh_profile: profile email surface --------------------------------


def test_fetch_picks_up_public_profile_email():
    gh = make_gh({
        "/users/steipete": {
            "email": "pete@openai.com",
            "bio": None,
            "html_url": "https://github.com/steipete",
        }
    })
    http = make_http({})  # No README
    result = fetch_gh_profile("steipete", gh_caller=gh, http_get=http, now=NOW)
    assert result.status == "ok"
    assert [c.address for c in result.candidates] == ["pete@openai.com"]
    src = result.candidates[0].sources[0]
    assert src.type == "gh_profile"
    assert src.detail == "public profile email"
    assert src.url == "https://github.com/steipete"


def test_fetch_skips_null_profile_email():
    gh = make_gh({
        "/users/steipete": {
            "email": None,
            "bio": None,
            "html_url": "https://github.com/steipete",
        }
    })
    http = make_http({})
    result = fetch_gh_profile("steipete", gh_caller=gh, http_get=http, now=NOW)
    assert result.status == "empty"
    assert result.candidates == []


def test_fetch_extracts_email_from_bio():
    gh = make_gh({
        "/users/jane": {
            "email": None,
            "bio": "Backend eng. Reach me at jane@acme.com.",
            "html_url": "https://github.com/jane",
        }
    })
    http = make_http({})
    result = fetch_gh_profile("jane", gh_caller=gh, http_get=http, now=NOW)
    assert [c.address for c in result.candidates] == ["jane@acme.com"]
    assert "bio" in result.candidates[0].sources[0].detail


def test_fetch_combines_profile_email_and_bio():
    """If the profile field AND the bio both contain emails, surface both."""
    gh = make_gh({
        "/users/jane": {
            "email": "jane@acme.com",
            "bio": "Personal jane@gmail.com",
            "html_url": "https://github.com/jane",
        }
    })
    http = make_http({})
    result = fetch_gh_profile("jane", gh_caller=gh, http_get=http, now=NOW)
    addresses = {c.address for c in result.candidates}
    assert addresses == {"jane@acme.com", "jane@gmail.com"}


# ---- fetch_gh_profile: README surface ---------------------------------------


def test_fetch_picks_up_email_from_profile_readme_main_branch():
    gh = make_gh({
        "/users/steipete": {"email": None, "bio": None, "html_url": "https://github.com/steipete"},
    })
    http = make_http({
        "https://raw.githubusercontent.com/steipete/steipete/main/README.md":
            "# Hi I'm Peter\n\n📫 reach me: <a href='mailto:pete@openai.com'>email</a>",
    })
    result = fetch_gh_profile("steipete", gh_caller=gh, http_get=http, now=NOW)
    assert [c.address for c in result.candidates] == ["pete@openai.com"]
    src = result.candidates[0].sources[0]
    assert src.type == "gh_readme"
    assert "main/README.md" in (src.url or "")
    assert "(mailto)" in src.detail


def test_fetch_falls_back_to_master_branch_for_old_repos():
    """Some legacy profile READMEs sit on `master`, not `main`."""
    gh = make_gh({
        "/users/oldcoder": {"email": None, "bio": None, "html_url": "https://github.com/oldcoder"},
    })
    http = make_http({
        # main 404s; master has the content
        "https://raw.githubusercontent.com/oldcoder/oldcoder/main/README.md": None,
        "https://raw.githubusercontent.com/oldcoder/oldcoder/master/README.md":
            "Contact: hello@oldcoder.dev",
    })
    result = fetch_gh_profile("oldcoder", gh_caller=gh, http_get=http, now=NOW)
    assert [c.address for c in result.candidates] == ["hello@oldcoder.dev"]
    assert "master/README.md" in (result.candidates[0].sources[0].url or "")


def test_fetch_merges_profile_and_readme_signals_for_same_address():
    """If pete@openai.com appears in both the profile field AND the README,
    surface ONE candidate with TWO sources (one per surface)."""
    gh = make_gh({
        "/users/steipete": {
            "email": "pete@openai.com",
            "bio": None,
            "html_url": "https://github.com/steipete",
        },
    })
    http = make_http({
        "https://raw.githubusercontent.com/steipete/steipete/main/README.md":
            "Reach me at pete@openai.com",
    })
    result = fetch_gh_profile("steipete", gh_caller=gh, http_get=http, now=NOW)
    assert len(result.candidates) == 1
    source_types = {s.type for s in result.candidates[0].sources}
    assert source_types == {"gh_profile", "gh_readme"}


def test_fetch_handles_no_readme_at_all():
    gh = make_gh({
        "/users/x": {"email": "x@y.com", "bio": None, "html_url": "https://github.com/x"},
    })
    http = make_http({})  # everything 404s
    result = fetch_gh_profile("x", gh_caller=gh, http_get=http, now=NOW)
    assert result.status == "ok"
    assert [c.address for c in result.candidates] == ["x@y.com"]


# ---- fetch_gh_profile: failure modes ----------------------------------------


def test_fetch_returns_unavailable_when_no_caller(monkeypatch):
    import lib.gh_profile as gp
    monkeypatch.setattr(gp, "_default_gh_caller", lambda: None)
    result = gp.fetch_gh_profile("x", now=NOW)
    assert result.status == "unavailable"


def test_fetch_returns_empty_for_blank_handle():
    result = fetch_gh_profile("", gh_caller=make_gh({}), http_get=make_http({}), now=NOW)
    assert result.status == "empty"
    assert result.candidates == []


def test_fetch_returns_error_on_subprocess_error():
    import subprocess
    def bombs(path):
        raise subprocess.SubprocessError("simulated gh failure")
    result = fetch_gh_profile("x", gh_caller=bombs, http_get=make_http({}), now=NOW)
    assert result.status == "error"
    assert "SubprocessError" in (result.error_detail or "")


def test_fetch_returns_timeout_on_subprocess_timeout():
    import subprocess
    def slow(path):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=6)
    result = fetch_gh_profile("x", gh_caller=slow, http_get=make_http({}), now=NOW)
    assert result.status == "timeout"


def test_fetch_continues_when_readme_fetch_raises():
    """If the README fetch throws on main, we should silently try master,
    and if BOTH throw, we still keep any profile-email signal."""
    import urllib.error

    def http_that_errors(url):
        raise urllib.error.URLError("dns failed")

    gh = make_gh({
        "/users/steipete": {
            "email": "pete@openai.com",
            "bio": None,
            "html_url": "https://github.com/steipete",
        },
    })
    result = fetch_gh_profile("steipete", gh_caller=gh, http_get=http_that_errors, now=NOW)
    # README fetch fails entirely; profile email survives.
    assert result.status == "ok"
    assert [c.address for c in result.candidates] == ["pete@openai.com"]
    assert result.candidates[0].sources[0].type == "gh_profile"
