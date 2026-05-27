"""Tests for lib/git_emails.py.

Deterministic — all GitHub API responses come through an injected
gh_caller stub. Real network tests would live under `@pytest.mark.network`
but aren't included here (we'd want a fixed account with known commits).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lib.git_emails import (
    _is_noise_email,
    fetch_git_emails,
)


# --- Fixed reference time so cutoff math is deterministic. ---
NOW = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
RECENT = (NOW - timedelta(days=10)).isoformat().replace("+00:00", "Z")
OLD = (NOW - timedelta(days=120)).isoformat().replace("+00:00", "Z")
JUST_INSIDE = (NOW - timedelta(days=89)).isoformat().replace("+00:00", "Z")
JUST_OUTSIDE = (NOW - timedelta(days=91)).isoformat().replace("+00:00", "Z")


def make_caller(routes):
    """Build a deterministic gh_caller from a {path_prefix: response} dict.

    Path matching is by startswith() against the keys; longest matching key
    wins. This lets a test specify '/users/X/events' and '/users/X/repos'
    independently without dealing with query string ordering.
    """
    def caller(path: str):
        best_key = ""
        for key in routes:
            if path.startswith(key) and len(key) > len(best_key):
                best_key = key
        if not best_key:
            raise AssertionError(f"unexpected gh_caller path in test: {path}")
        resp = routes[best_key]
        if isinstance(resp, Exception):
            raise resp
        return resp
    return caller


# ---- _is_noise_email --------------------------------------------------------


def test_noise_email_drops_github_noreply():
    assert _is_noise_email("12345+steipete@users.noreply.github.com")
    assert _is_noise_email("nobody@users.noreply.github.com")


def test_noise_email_drops_example_domains():
    assert _is_noise_email("test@example.com")
    assert _is_noise_email("a@example.org")
    assert _is_noise_email("b@localhost")
    assert _is_noise_email("c@.local")
    assert _is_noise_email("d@something.localhost")


def test_noise_email_drops_noreply_localparts():
    assert _is_noise_email("noreply@github.com")
    assert _is_noise_email("no-reply@example.com")


def test_noise_email_drops_bot_markers():
    assert _is_noise_email("49699333+dependabot[bot]@users.noreply.github.com")
    assert _is_noise_email("github-actions[bot]@github.com")
    assert _is_noise_email("renovate@renovate.com")
    assert _is_noise_email("snyk-bot@snyk.io")


def test_noise_email_accepts_real_addresses():
    assert not _is_noise_email("pete@openai.com")
    assert not _is_noise_email("steipete@gmail.com")
    assert not _is_noise_email("etienne@dupont.fr")


def test_noise_email_drops_malformed():
    assert _is_noise_email("")
    assert _is_noise_email("not-an-email")
    assert _is_noise_email("@example.com")
    assert _is_noise_email("user@")


# ---- fetch_git_emails: happy path -------------------------------------------


def test_fetch_returns_emails_from_recent_push_events():
    routes = {
        "/users/steipete/events": [
            {
                "type": "PushEvent",
                "created_at": RECENT,
                "repo": {"name": "steipete/lobster-os"},
                "payload": {
                    "commits": [
                        {
                            "sha": "abc123",
                            "author": {"email": "pete@openai.com", "name": "Peter Steinberger"},
                        }
                    ]
                },
            }
        ],
        "/users/steipete/repos": [],
    }
    result = fetch_git_emails("steipete", gh_caller=make_caller(routes), now=NOW)
    assert result.status == "ok"
    assert len(result.candidates) == 1
    assert result.candidates[0].address == "pete@openai.com"
    assert len(result.candidates[0].sources) == 1
    src = result.candidates[0].sources[0]
    assert src.type == "git_commit"
    assert src.url == "https://github.com/steipete/lobster-os/commit/abc123"
    assert "steipete/lobster-os" in src.detail


def test_fetch_dedupes_across_multiple_commits():
    routes = {
        "/users/steipete/events": [
            {
                "type": "PushEvent",
                "created_at": RECENT,
                "repo": {"name": "steipete/repo-one"},
                "payload": {
                    "commits": [
                        {"sha": "aaa", "author": {"email": "pete@openai.com"}},
                        {"sha": "bbb", "author": {"email": "pete@openai.com"}},
                    ]
                },
            },
            {
                "type": "PushEvent",
                "created_at": (NOW - timedelta(days=20)).isoformat().replace("+00:00", "Z"),
                "repo": {"name": "steipete/repo-two"},
                "payload": {
                    "commits": [
                        {"sha": "ccc", "author": {"email": "pete@openai.com"}},
                    ]
                },
            },
        ],
        "/users/steipete/repos": [],
    }
    result = fetch_git_emails("steipete", gh_caller=make_caller(routes), now=NOW)
    assert len(result.candidates) == 1
    assert len(result.candidates[0].sources) == 3
    # Newest first
    assert result.candidates[0].sources[0].url.endswith("/aaa") or \
           result.candidates[0].sources[0].url.endswith("/bbb")


def test_fetch_preserves_work_and_personal_split():
    """Multi-account devs commit as work-email-A AND personal-email-B.
    Don't merge them — surface both."""
    routes = {
        "/users/steipete/events": [
            {
                "type": "PushEvent",
                "created_at": RECENT,
                "repo": {"name": "steipete/oss-thing"},
                "payload": {
                    "commits": [
                        {"sha": "aaa", "author": {"email": "steipete@gmail.com"}},
                    ]
                },
            },
            {
                "type": "PushEvent",
                "created_at": (NOW - timedelta(days=12)).isoformat().replace("+00:00", "Z"),
                "repo": {"name": "steipete/lobster-os"},
                "payload": {
                    "commits": [
                        {"sha": "bbb", "author": {"email": "pete@openai.com"}},
                    ]
                },
            },
        ],
        "/users/steipete/repos": [],
    }
    result = fetch_git_emails("steipete", gh_caller=make_caller(routes), now=NOW)
    assert {c.address for c in result.candidates} == {"steipete@gmail.com", "pete@openai.com"}


# ---- fetch_git_emails: filtering --------------------------------------------


def test_fetch_drops_noreply_emails():
    routes = {
        "/users/steipete/events": [
            {
                "type": "PushEvent",
                "created_at": RECENT,
                "repo": {"name": "steipete/x"},
                "payload": {
                    "commits": [
                        {"sha": "a", "author": {"email": "12345+steipete@users.noreply.github.com"}},
                        {"sha": "b", "author": {"email": "pete@openai.com"}},
                    ]
                },
            }
        ],
        "/users/steipete/repos": [],
    }
    result = fetch_git_emails("steipete", gh_caller=make_caller(routes), now=NOW)
    assert [c.address for c in result.candidates] == ["pete@openai.com"]


def test_fetch_drops_bot_commits():
    routes = {
        "/users/steipete/events": [
            {
                "type": "PushEvent",
                "created_at": RECENT,
                "repo": {"name": "steipete/x"},
                "payload": {
                    "commits": [
                        {"sha": "a", "author": {"email": "dependabot[bot]@users.noreply.github.com"}},
                        {"sha": "b", "author": {"email": "renovate@whitesource.com"}},
                        {"sha": "c", "author": {"email": "pete@openai.com"}},
                    ]
                },
            }
        ],
        "/users/steipete/repos": [],
    }
    result = fetch_git_emails("steipete", gh_caller=make_caller(routes), now=NOW)
    assert [c.address for c in result.candidates] == ["pete@openai.com"]


def test_fetch_drops_events_outside_lookback_window():
    routes = {
        "/users/steipete/events": [
            {
                "type": "PushEvent",
                "created_at": JUST_OUTSIDE,
                "repo": {"name": "steipete/x"},
                "payload": {
                    "commits": [{"sha": "a", "author": {"email": "old@openai.com"}}]
                },
            },
            {
                "type": "PushEvent",
                "created_at": JUST_INSIDE,
                "repo": {"name": "steipete/x"},
                "payload": {
                    "commits": [{"sha": "b", "author": {"email": "current@openai.com"}}]
                },
            },
        ],
        "/users/steipete/repos": [],
    }
    result = fetch_git_emails("steipete", gh_caller=make_caller(routes), now=NOW)
    assert [c.address for c in result.candidates] == ["current@openai.com"]


def test_fetch_ignores_non_push_event_types():
    routes = {
        "/users/steipete/events": [
            {"type": "PullRequestEvent", "created_at": RECENT, "payload": {}},
            {"type": "IssueCommentEvent", "created_at": RECENT, "payload": {}},
            {
                "type": "PushEvent",
                "created_at": RECENT,
                "repo": {"name": "steipete/x"},
                "payload": {
                    "commits": [{"sha": "a", "author": {"email": "pete@openai.com"}}]
                },
            },
        ],
        "/users/steipete/repos": [],
    }
    result = fetch_git_emails("steipete", gh_caller=make_caller(routes), now=NOW)
    assert [c.address for c in result.candidates] == ["pete@openai.com"]


# ---- fetch_git_emails: pass 2 (repos) ---------------------------------------


def test_fetch_falls_back_to_repos_pass_for_infrequent_pushers():
    """If /events is empty but the user has a repo with a recent commit,
    pass 2 should find it."""
    routes = {
        "/users/jane/events": [],
        "/users/jane/repos": [
            {
                "fork": False,
                "owner": {"login": "jane"},
                "name": "old-project",
                "pushed_at": RECENT,
            }
        ],
        "/repos/jane/old-project/commits": [
            {
                "sha": "deadbeef",
                "commit": {
                    "author": {
                        "email": "jane@acme.com",
                        "name": "Jane Doe",
                        "date": (NOW - timedelta(days=40)).isoformat().replace("+00:00", "Z"),
                    },
                },
            }
        ],
    }
    result = fetch_git_emails("jane", gh_caller=make_caller(routes), now=NOW)
    assert result.status == "ok"
    assert [c.address for c in result.candidates] == ["jane@acme.com"]


def test_fetch_skips_fork_repos_in_pass_2():
    """Fork commits are typically the upstream author, not the handle owner.
    Skip them to avoid laundering noise."""
    routes = {
        "/users/jane/events": [],
        "/users/jane/repos": [
            {
                "fork": True,
                "owner": {"login": "jane"},
                "name": "their-project",
                "pushed_at": RECENT,
            }
        ],
        # Even if commits exist, the fork should be skipped before they're queried.
        "/repos/jane/their-project/commits": [
            {
                "sha": "x",
                "commit": {"author": {"email": "upstream@elsewhere.com", "date": RECENT}},
            }
        ],
    }
    result = fetch_git_emails("jane", gh_caller=make_caller(routes), now=NOW)
    assert result.status == "empty"
    assert result.candidates == []


# ---- fetch_git_emails: failure modes ----------------------------------------


def test_fetch_handles_empty_response():
    routes = {
        "/users/ghost/events": [],
        "/users/ghost/repos": [],
    }
    result = fetch_git_emails("ghost", gh_caller=make_caller(routes), now=NOW)
    assert result.status == "empty"
    assert result.candidates == []
    assert "noreply" in (result.error_detail or "") or "activity" in (result.error_detail or "")


def test_fetch_returns_unavailable_when_no_caller_discoverable(monkeypatch):
    """If `_default_gh_caller` returns None (no gh, no anon HTTP), surface the
    gap so the renderer can flag a degraded run. The pipeline keeps going."""
    import lib.git_emails as ge
    monkeypatch.setattr(ge, "_default_gh_caller", lambda: None)
    result = ge.fetch_git_emails("steipete", now=NOW)
    assert result.status == "unavailable"
    assert result.candidates == []
    assert "gh CLI" in (result.error_detail or "")


def test_fetch_returns_empty_when_handle_blank():
    result = fetch_git_emails("", gh_caller=make_caller({}), now=NOW)
    assert result.status == "empty"
    assert result.candidates == []


def test_fetch_returns_error_on_json_parse_failure():
    import json
    def bad_caller(path):
        raise json.JSONDecodeError("bad", "doc", 0)
    result = fetch_git_emails("x", gh_caller=bad_caller, now=NOW)
    assert result.status == "error"
    assert "JSONDecodeError" in (result.error_detail or "")


def test_fetch_returns_timeout_on_subprocess_timeout():
    import subprocess
    def slow_caller(path):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=8)
    result = fetch_git_emails("x", gh_caller=slow_caller, now=NOW)
    assert result.status == "timeout"
    assert "timeout" in (result.error_detail or "").lower()


def test_fetch_returns_error_on_http_failure():
    import urllib.error
    def failing_caller(path):
        raise urllib.error.URLError("network down")
    result = fetch_git_emails("x", gh_caller=failing_caller, now=NOW)
    assert result.status == "error"


def test_fetch_continues_when_per_repo_call_fails():
    """A single bad /commits call should not kill the whole resolver — the
    other repos should still be tried, and any events-pass candidates kept."""
    import subprocess
    call_count = {"n": 0}

    def caller(path):
        call_count["n"] += 1
        if path.startswith("/users/jane/events"):
            return [
                {
                    "type": "PushEvent",
                    "created_at": RECENT,
                    "repo": {"name": "jane/main-repo"},
                    "payload": {
                        "commits": [{"sha": "x", "author": {"email": "jane@acme.com"}}]
                    },
                }
            ]
        if path.startswith("/users/jane/repos"):
            return [
                {"fork": False, "owner": {"login": "jane"}, "name": "ok-repo", "pushed_at": RECENT},
                {"fork": False, "owner": {"login": "jane"}, "name": "bad-repo", "pushed_at": RECENT},
            ]
        if "bad-repo" in path:
            raise subprocess.SubprocessError("simulated")
        if "ok-repo" in path:
            return [
                {
                    "sha": "abc",
                    "commit": {
                        "author": {
                            "email": "jane@othercorp.com",
                            "date": (NOW - timedelta(days=30)).isoformat().replace("+00:00", "Z"),
                        }
                    },
                }
            ]
        return []

    result = fetch_git_emails("jane", gh_caller=caller, now=NOW)
    assert result.status == "ok"
    addresses = {c.address for c in result.candidates}
    # Events pass found jane@acme.com; repo pass found jane@othercorp.com from ok-repo
    # and gracefully skipped bad-repo.
    assert "jane@acme.com" in addresses
    assert "jane@othercorp.com" in addresses


# ---- sources are sorted newest first ----------------------------------------


def test_sources_per_candidate_are_sorted_newest_first():
    older = (NOW - timedelta(days=60)).isoformat().replace("+00:00", "Z")
    newer = (NOW - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    routes = {
        "/users/jane/events": [
            {
                "type": "PushEvent",
                "created_at": older,
                "repo": {"name": "jane/x"},
                "payload": {"commits": [{"sha": "old", "author": {"email": "jane@acme.com"}}]},
            },
            {
                "type": "PushEvent",
                "created_at": newer,
                "repo": {"name": "jane/x"},
                "payload": {"commits": [{"sha": "new", "author": {"email": "jane@acme.com"}}]},
            },
        ],
        "/users/jane/repos": [],
    }
    result = fetch_git_emails("jane", gh_caller=make_caller(routes), now=NOW)
    sources = result.candidates[0].sources
    assert sources[0].observed_at > sources[1].observed_at
