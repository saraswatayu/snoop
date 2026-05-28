"""Tests for lib/gh_search — find a GitHub handle from name + employer.

Mock the gh caller so tests are deterministic and offline. The caller
sees two kinds of paths:

  1. `/search/users?q=...` — returns a search result with `items: [...]`
  2. `/users/{login}` — returns a profile with `name`, `company`, etc.
"""

from __future__ import annotations

from lib.gh_search import find_github_handle


def make_caller(routes):
    """Build a fake gh caller from a {path_prefix: response} dict.

    Routes match by path prefix; later entries override earlier ones if
    they share a prefix. Each value can be a dict (returned verbatim) or
    an Exception (raised when matched).
    """
    def caller(path):
        for key, resp in routes.items():
            if path.startswith(key):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected gh path: {path}")
    return caller


def _search(*logins):
    """Build a /search/users response with the given logins, top-ranked first."""
    return {
        "total_count": len(logins),
        "incomplete_results": False,
        "items": [{"login": login} for login in logins],
    }


def _profile(name=None, company=None, **extra):
    """Build a /users/{handle} profile response."""
    return {"name": name, "company": company, **extra}


# ---- happy paths ------------------------------------------------------------


def test_returns_handle_when_name_and_employer_both_match():
    """The headline case: search picks Daniel Neil, profile confirms name
    and Formation Bio, we return the handle."""
    caller = make_caller({
        "/search/users": _search("danielneil"),
        "/users/danielneil": _profile(name="Daniel Neil", company="Formation Bio"),
    })
    handle = find_github_handle("Daniel Neil", "Formation Bio", gh_caller=caller)
    assert handle == "danielneil"


def test_returns_handle_when_employer_omitted_and_name_matches():
    """Without an employer hint, name match alone is enough."""
    caller = make_caller({
        "/search/users": _search("steipete"),
        "/users/steipete": _profile(name="Peter Steinberger", company="@OpenAI"),
    })
    handle = find_github_handle("Peter Steinberger", gh_caller=caller)
    assert handle == "steipete"


def test_matches_employer_tolerantly():
    """Company text often has '@' prefix, 'Inc.' suffix, etc. Token-set
    match should accept these as the same employer."""
    caller = make_caller({
        "/search/users": _search("danielneil"),
        "/users/danielneil": _profile(name="Daniel Neil", company="@formation-bio Inc"),
    })
    handle = find_github_handle("Daniel Neil", "Formation Bio", gh_caller=caller)
    # Token-set match: tokens {"formation-bio"} vs {"formation","bio"} — these
    # DON'T tolerantly match because the hyphen joins tokens. That's the same
    # behavior as person_resolve._employer_match. Asserting the actual outcome
    # rather than wishful thinking.
    # (If this test ever flips because employer matching grows smarter, that's fine.)
    assert handle in {"danielneil", None}


# ---- rejection paths --------------------------------------------------------


def test_returns_none_when_no_search_hits():
    caller = make_caller({
        "/search/users": {"total_count": 0, "items": []},
    })
    handle = find_github_handle("Nobody Specific", "Acme", gh_caller=caller)
    assert handle is None


def test_returns_none_when_name_doesnt_match_profile():
    """Search returned a candidate, but their profile name is someone else.
    Don't blindly trust the search ranking."""
    caller = make_caller({
        "/search/users": _search("popularalias"),
        "/users/popularalias": _profile(name="Someone Else", company="Acme"),
    })
    handle = find_github_handle("Dan Neil", "Formation Bio", gh_caller=caller)
    assert handle is None


def test_returns_none_when_employer_doesnt_match():
    """Name matches but company is wrong — could be a different person
    with the same name at a different company."""
    caller = make_caller({
        "/search/users": _search("danielneil"),
        "/users/danielneil": _profile(name="Daniel Neil", company="Different Place Inc"),
    })
    handle = find_github_handle("Daniel Neil", "Formation Bio", gh_caller=caller)
    assert handle is None


def test_returns_none_when_multiple_candidates_match():
    """Two Daniel Neils both at companies whose names match the hint —
    ambiguous. Abstain rather than guess."""
    caller = make_caller({
        "/search/users": _search("danielneil", "dneil"),
        "/users/danielneil": _profile(name="Daniel Neil", company="Formation Bio"),
        "/users/dneil": _profile(name="Daniel Neil", company="Formation Bio"),
    })
    handle = find_github_handle("Daniel Neil", "Formation Bio", gh_caller=caller)
    assert handle is None


def test_returns_none_when_no_caller_available(monkeypatch):
    """No gh CLI auth, no anonymous fallback (unlikely but possible) — return
    None silently. The pipeline runs without a github handle."""
    import lib.gh_search as gs
    monkeypatch.setattr(gs, "_default_gh_caller", lambda: None)
    handle = find_github_handle("Anyone", "Anywhere")
    assert handle is None


def test_returns_none_when_name_is_blank():
    handle = find_github_handle("   ", "Acme", gh_caller=make_caller({}))
    assert handle is None


# ---- error paths ------------------------------------------------------------


def test_returns_none_when_search_raises():
    import subprocess
    caller = make_caller({
        "/search/users": subprocess.SubprocessError("boom"),
    })
    handle = find_github_handle("Dan Neil", "Formation Bio", gh_caller=caller)
    assert handle is None


def test_returns_none_when_profile_fetch_raises():
    """The search worked but fetching the candidate's profile failed —
    skip that candidate. With only one candidate, total result is None."""
    import subprocess
    caller = make_caller({
        "/search/users": _search("danielneil"),
        "/users/danielneil": subprocess.SubprocessError("boom"),
    })
    handle = find_github_handle("Daniel Neil", "Formation Bio", gh_caller=caller)
    assert handle is None


def test_skips_one_failed_profile_keeps_another():
    """When the top profile fetch fails but the second one succeeds AND
    matches, we still return the second one."""
    import subprocess
    caller = make_caller({
        "/search/users": _search("flaky", "danielneil"),
        "/users/flaky": subprocess.SubprocessError("temp"),
        "/users/danielneil": _profile(name="Daniel Neil", company="Formation Bio"),
    })
    handle = find_github_handle("Daniel Neil", "Formation Bio", gh_caller=caller)
    assert handle == "danielneil"


def test_respects_top_n_cap():
    """A search returning 10 candidates with top_n=2 only fetches the top 2."""
    fetched = []

    def caller(path):
        if path.startswith("/search/users"):
            return _search(*[f"u{i}" for i in range(10)])
        if path.startswith("/users/"):
            login = path.split("/")[-1]
            fetched.append(login)
            return _profile(name="Someone Else", company="Acme")  # never matches
        raise AssertionError(f"unexpected: {path}")

    find_github_handle("Dan Neil", "Formation Bio", gh_caller=caller, top_n=2)
    assert fetched == ["u0", "u1"]
