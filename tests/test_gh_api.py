"""Tests for the shared lib/_gh_api module.

The resolver tests inject `gh_caller=` at the public API and don't exercise
the shared module directly. These tests cover the shared layer so a future
break (e.g. accidentally changing the API base URL or the auth-check
command) is caught without needing live network.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
from unittest.mock import patch

import pytest

from lib import _gh_api


def test_default_timeout_sec_is_a_number():
    assert isinstance(_gh_api.DEFAULT_TIMEOUT_SEC, (int, float))
    assert _gh_api.DEFAULT_TIMEOUT_SEC > 0


def test_gh_via_cli_returns_parsed_json_on_zero_exit():
    fake_completed = subprocess.CompletedProcess(
        args=["gh"], returncode=0,
        stdout=json.dumps({"login": "steipete"}), stderr="",
    )
    with patch("lib._gh_api.subprocess.run", return_value=fake_completed):
        result = _gh_api.gh_via_cli("/users/steipete")
    assert result == {"login": "steipete"}


def test_gh_via_cli_returns_empty_dict_when_stdout_empty():
    fake_completed = subprocess.CompletedProcess(
        args=["gh"], returncode=0, stdout="", stderr="",
    )
    with patch("lib._gh_api.subprocess.run", return_value=fake_completed):
        result = _gh_api.gh_via_cli("/users/steipete")
    assert result == {}


def test_gh_via_cli_raises_on_nonzero_exit():
    fake_completed = subprocess.CompletedProcess(
        args=["gh"], returncode=1, stdout="", stderr="not authed",
    )
    with patch("lib._gh_api.subprocess.run", return_value=fake_completed):
        with pytest.raises(subprocess.SubprocessError) as ei:
            _gh_api.gh_via_cli("/users/steipete")
        assert "not authed" in str(ei.value)


def test_gh_via_http_propagates_http_errors():
    """The shared module does NOT swallow 404 — that's a person_resolve-
    specific contract handled by its own wrapper. Other resolvers expect
    HTTPError to propagate so their wrap-with-status="error" path works."""
    err = urllib.error.HTTPError(
        url="https://api.github.com/users/x",
        code=404, msg="Not Found", hdrs=None, fp=None,  # type: ignore[arg-type]
    )
    with patch("lib._gh_api.urllib.request.urlopen", side_effect=err):
        with pytest.raises(urllib.error.HTTPError):
            _gh_api.gh_via_http("/users/x")


def test_default_gh_caller_prefers_cli_when_authed():
    """gh installed + `gh auth status` exit 0 → use gh_via_cli."""
    auth_ok = subprocess.CompletedProcess(
        args=["gh", "auth", "status"], returncode=0, stdout=b"", stderr=b"",
    )
    with patch("lib._gh_api.which", return_value="/usr/local/bin/gh"), \
         patch("lib._gh_api.subprocess.run", return_value=auth_ok):
        caller = _gh_api.default_gh_caller()
    assert caller is _gh_api.gh_via_cli


def test_default_gh_caller_falls_back_to_http_when_gh_missing():
    with patch("lib._gh_api.which", return_value=None):
        caller = _gh_api.default_gh_caller()
    assert caller is _gh_api.gh_via_http


def test_default_gh_caller_falls_back_to_http_when_gh_not_authed():
    auth_failed = subprocess.CompletedProcess(
        args=["gh", "auth", "status"], returncode=1, stdout=b"", stderr=b"",
    )
    with patch("lib._gh_api.which", return_value="/usr/local/bin/gh"), \
         patch("lib._gh_api.subprocess.run", return_value=auth_failed):
        caller = _gh_api.default_gh_caller()
    assert caller is _gh_api.gh_via_http
