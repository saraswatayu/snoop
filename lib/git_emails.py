"""lib/git_emails.py — author emails from public GitHub commits.

The plan calls this "the killer signal." Two-pass design covers both
frequent and infrequent pushers:

  Pass 1: GET /users/{h}/events
    PushEvents over the last N days. Captures users who push regularly.

  Pass 2: GET /users/{h}/repos + per-repo GET /commits?author={h}&per_page=1
    Latest commit per owned repo. Captures infrequent pushers whose
    activity falls outside the events window. Owned repos only — commits
    to other orgs aren't visible here.

Noise filtering is critical. Per the post-review plan:
  - Drop `*@users.noreply.github.com` (user opted out — ~25-30% of accounts)
  - Drop `*@example.com`, `localhost`, `local` (obvious placeholders)
  - Drop bot markers: `[bot]`, `dependabot`, `github-actions`, `renovate`
  - Drop `noreply@*` localparts

Auth chain (Subagent F1 mitigation):
  1. gh CLI authenticated → 5000 req/hr (good for any normal lookup)
  2. Anonymous HTTP → 60 req/hr (enough for one lookup, not for retries)
  3. Neither → ResolverResult(status="unavailable") so the renderer can
     surface the gap; the rest of the pipeline keeps running.

Per-call timeout: each `gh_caller` invocation has its own timeout
budget. The outer ThreadPoolExecutor in the pipeline enforces a
hard wall-clock cap via future.result(timeout=5).
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

from . import _gh_api
from ._gh_api import GhCaller
from .normalize import domain_is_noise, normalize_email
from .schema import EmailCandidate, ResolverResult, Source

_DEFAULT_LOOKBACK_DAYS = 90
_DEFAULT_REPO_LIMIT = 30  # per-repo pass cost; cap to keep latency bounded

# GitHub's privacy-noreply domain is git-specific; the reserved/placeholder set
# is shared (lib.normalize.RESERVED_NOISE_DOMAINS via domain_is_noise).
_EXTRA_NOISE_DOMAINS = ("users.noreply.github.com",)
_NOISE_LOCALPART_PREFIXES = ("noreply", "no-reply")
_BOT_MARKERS = ("[bot]", "dependabot", "github-actions", "renovate", "snyk-bot")


# Local re-export so existing tests that monkey-patch
# `git_emails._default_gh_caller` keep working.
def _default_gh_caller() -> GhCaller | None:
    return _gh_api.default_gh_caller()


def _is_noise_email(email: str) -> bool:
    """True if this email should be discarded as a non-personal address."""
    if not email or "@" not in email:
        return True
    local, _, domain = email.lower().partition("@")
    if not local or not domain:
        return True
    if domain_is_noise(domain, extra=_EXTRA_NOISE_DOMAINS):
        return True
    if any(local.startswith(lp) for lp in _NOISE_LOCALPART_PREFIXES):
        return True
    if any(m in email.lower() for m in _BOT_MARKERS):
        return True
    return False


def _parse_dt(s: str | None) -> datetime | None:
    """Parse a GitHub ISO timestamp like '2026-05-14T12:00:00Z'."""
    if not s:
        return None
    try:
        # GitHub returns 'Z' suffix; fromisoformat in 3.11+ handles it.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _harvest_from_events(
    events: Any,
    cutoff: datetime,
    out: dict[str, list[Source]],
) -> None:
    """Walk the /events response, adding (email → Source) entries to `out`.

    Only PushEvents are considered. Other event types (PullRequestEvent,
    IssueCommentEvent, etc.) don't expose author email.
    """
    if not isinstance(events, list):
        return
    for evt in events:
        if not isinstance(evt, dict) or evt.get("type") != "PushEvent":
            continue
        evt_time = _parse_dt(evt.get("created_at"))
        if evt_time is None or evt_time < cutoff:
            continue
        repo_name = (evt.get("repo") or {}).get("name", "")
        commits = (evt.get("payload") or {}).get("commits") or []
        for commit in commits:
            if not isinstance(commit, dict):
                continue
            author = commit.get("author") or {}
            email_raw = author.get("email", "")
            if not isinstance(email_raw, str):
                continue
            if _is_noise_email(email_raw):
                continue
            email = normalize_email(email_raw)
            sha = commit.get("sha", "")
            url = (
                f"https://github.com/{repo_name}/commit/{sha}"
                if repo_name and sha
                else None
            )
            src = Source(
                type="git_commit",
                url=url,
                observed_at=evt_time,
                detail=f"commit author in {repo_name}" if repo_name else "commit author",
            )
            out.setdefault(email, []).append(src)


def _harvest_from_repos(
    repos: Any,
    cutoff: datetime,
    handle: str,
    caller: GhCaller,
    out: dict[str, list[Source]],
    repo_limit: int = _DEFAULT_REPO_LIMIT,
) -> None:
    """Walk the /repos response, fetching one commit per repo for `handle`.

    Catches infrequent pushers whose activity is outside the /events
    window. Limited to repo_limit repos to keep the second-pass cost
    bounded (each repo = one extra API call).
    """
    if not isinstance(repos, list):
        return
    # Sort by pushed_at descending (most recently active first) so the
    # repo_limit cap captures relevant repos.
    def _pushed_key(r: Any) -> str:
        if isinstance(r, dict):
            return r.get("pushed_at") or ""
        return ""
    sorted_repos = sorted(repos, key=_pushed_key, reverse=True)[:repo_limit]

    for repo in sorted_repos:
        if not isinstance(repo, dict):
            continue
        if repo.get("fork"):
            # Fork commits will mostly be the upstream author, not the
            # handle owner. Skip to avoid laundering noise.
            continue
        owner = (repo.get("owner") or {}).get("login")
        name = repo.get("name")
        if not owner or not name:
            continue
        try:
            commits = caller(
                f"/repos/{owner}/{name}/commits?author={urllib.parse.quote(handle, safe='')}&per_page=1"
            )
        except (subprocess.SubprocessError, urllib.error.URLError,
                json.JSONDecodeError, OSError):
            # Per-repo failures are not fatal; keep walking.
            continue
        if not isinstance(commits, list):
            continue
        for commit in commits:
            if not isinstance(commit, dict):
                continue
            sha = commit.get("sha", "")
            commit_data = commit.get("commit") or {}
            author = commit_data.get("author") or {}
            email_raw = author.get("email", "")
            ts_raw = author.get("date")
            if not isinstance(email_raw, str) or _is_noise_email(email_raw):
                continue
            ts = _parse_dt(ts_raw)
            if ts is None or ts < cutoff:
                continue
            email = normalize_email(email_raw)
            url = f"https://github.com/{owner}/{name}/commit/{sha}" if sha else None
            src = Source(
                type="git_commit",
                url=url,
                observed_at=ts,
                detail=f"commit author in {owner}/{name}",
            )
            out.setdefault(email, []).append(src)


def fetch_git_emails(
    handle: str,
    days: int = _DEFAULT_LOOKBACK_DAYS,
    *,
    gh_caller: GhCaller | None = None,
    now: datetime | None = None,
    repo_limit: int = _DEFAULT_REPO_LIMIT,
) -> ResolverResult:
    """Pull author emails from a GitHub user's recent public commits.

    Args:
        handle: GitHub username.
        days: Lookback window. Default 90.
        gh_caller: Injectable API caller; if None, uses gh CLI or anon HTTP.
        now: For deterministic tests; defaults to datetime.utcnow().
        repo_limit: Max repos to inspect in pass 2. Default 30.

    Returns:
        ResolverResult with deduped EmailCandidates. Sources per candidate
        are sorted newest-first.
    """
    start = datetime.now(timezone.utc) if now is None else now
    caller = gh_caller if gh_caller is not None else _default_gh_caller()
    if caller is None:
        return ResolverResult(
            resolver="git_emails",
            candidates=[],
            status="unavailable",
            error_detail="neither gh CLI (auth'd) nor anonymous HTTP available",
        )

    if not handle or not handle.strip():
        return ResolverResult(
            resolver="git_emails",
            candidates=[],
            status="empty",
            error_detail="no handle provided",
        )

    cutoff = start - timedelta(days=days)
    by_addr: dict[str, list[Source]] = {}

    try:
        events = caller(f"/users/{urllib.parse.quote(handle, safe='')}/events?per_page=100")
        _harvest_from_events(events, cutoff, by_addr)

        repos = caller(f"/users/{urllib.parse.quote(handle, safe='')}/repos?per_page=100&type=owner")
        _harvest_from_repos(repos, cutoff, handle, caller, by_addr, repo_limit)
    except subprocess.TimeoutExpired as e:
        elapsed = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
        return ResolverResult(
            resolver="git_emails",
            candidates=[],
            status="timeout",
            elapsed_ms=elapsed,
            error_detail=f"gh api timeout after {e.timeout}s",
        )
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        elapsed = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
        return ResolverResult(
            resolver="git_emails",
            candidates=[],
            status="error",
            elapsed_ms=elapsed,
            error_detail=f"HTTP error: {e}",
        )
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as e:
        elapsed = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
        return ResolverResult(
            resolver="git_emails",
            candidates=[],
            status="error",
            elapsed_ms=elapsed,
            error_detail=f"{type(e).__name__}: {e}",
        )

    candidates = [
        EmailCandidate(
            address=addr,
            sources=sorted(srcs, key=lambda s: -s.observed_at.timestamp()),
        )
        for addr, srcs in by_addr.items()
    ]
    elapsed = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    status = "ok" if candidates else "empty"
    return ResolverResult(
        resolver="git_emails",
        candidates=candidates,
        status=status,
        elapsed_ms=elapsed,
        error_detail=(
            "no commit emails (user may use GitHub privacy noreply, or no recent activity)"
            if not candidates
            else None
        ),
    )
