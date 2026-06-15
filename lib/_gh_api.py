"""Shared GitHub API caller used by git_emails, gh_profile, person_resolve.

Three modules previously shipped near-identical _gh_via_cli + _gh_via_http +
_default_gh_caller copies. The copies had already drifted on `_DEFAULT_TIMEOUT_SEC`
(8.0 in git_emails, 6.0 in the other two). Centralizing avoids the next
drift and gives one place to add e.g. retry/backoff.

Auth chain (intentionally the same as before):
  1. `gh` CLI authenticated → 5000 req/hr
  2. Anonymous HTTP → 60 req/hr

Tests inject their own callers at the resolver boundary, so this module
isn't normally on the test path. Each module still exposes a local
`_default_gh_caller` name (a thin wrapper) so existing
`monkeypatch.setattr(module, "_default_gh_caller", ...)` tests keep working.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from shutil import which
from typing import Any, Callable

from .fetch import USER_AGENT

_API_BASE = "https://api.github.com"
DEFAULT_TIMEOUT_SEC = 6.0
# Bound the anonymous-HTTP read so this path keeps the same bounded-body
# discipline lib.fetch enforces everywhere else. api.github.com is trusted, but a
# pathological/compromised response shouldn't read unbounded into memory. Real
# API pages (events/repos at per_page=100) are well under this.
_MAX_RESPONSE_BYTES = 10_000_000

GhCaller = Callable[[str], Any]


def gh_via_cli(path: str, *, timeout: float = DEFAULT_TIMEOUT_SEC) -> Any:
    """Invoke `gh api <path>`. Returns parsed JSON, or {} on empty stdout.

    Raises subprocess.SubprocessError on non-zero exit,
    json.JSONDecodeError on parse failure.
    """
    result = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github+json", path],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if result.returncode != 0:
        raise subprocess.SubprocessError(
            f"gh api {path} exited {result.returncode}: {result.stderr.strip()[:200]}"
        )
    if not result.stdout.strip():
        return {}
    return json.loads(result.stdout)


def gh_via_http(path: str, *, timeout: float = DEFAULT_TIMEOUT_SEC) -> Any:
    """Anonymous HTTP fallback. 60 req/hr ceiling.

    Raises urllib.error.HTTPError on non-2xx (callers decide whether 404
    means 'handle does not exist' or just propagates as error)."""
    url = path if path.startswith("http") else f"{_API_BASE}{path}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise OSError(f"GitHub API response exceeded {_MAX_RESPONSE_BYTES} bytes")
        return json.loads(body)


def default_gh_caller() -> GhCaller:
    """Return gh_via_cli when `gh` is authed, else gh_via_http.

    Never returns None: the anonymous HTTP path is always available as a
    last resort (failures surface inside the caller's own error handling).
    The previous return-type hint of `GhCaller | None` was vestigial — no
    code path actually returned None — but tests monkey-patch the per-module
    `_default_gh_caller` to None to simulate "no auth path", which the
    resolvers still handle as `unavailable`.
    """
    if which("gh") is not None:
        try:
            r = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True, timeout=2, check=False,
            )
            if r.returncode == 0:
                return gh_via_cli
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
    return gh_via_http
