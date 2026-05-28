"""lib/gh_profile.py — GitHub profile email, bio, and profile-README scan.

Distinct from `git_emails`. That resolver mines what the user *used to
commit*; this one reads what they *publish as their identity*.

Two surfaces:

  1. GET /users/{handle}
       The `email` field, when non-null, is high-trust: the user opted
       in to publishing it. ~20-30% of accounts populate it. Bio also
       sometimes contains an email or external profile URL worth a
       cross-link signal.

  2. The "profile README" at github.com/{handle}/{handle}/blob/main/README.md
       Many devs use the profile-README pattern with a contact line:
       "📫 reach me at: x@y.com". Parse mailto: anchors first
       (highest precision), then fall back to email regex on visible
       text. Skip image-embedded emails (don't OCR).

Both paths return Sources tagged distinctly so the renderer can attribute
correctly: `Source(type="gh_profile", ...)` vs `Source(type="gh_readme", ...)`.

The README fetch goes via raw.githubusercontent.com (not the gh API), so
this module accepts a separate http_get callable for testability.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable

from . import _gh_api
from ._gh_api import GhCaller
from .normalize import normalize_email
from .schema import EmailCandidate, ResolverResult, Source

_DEFAULT_TIMEOUT_SEC = 6.0

# Used both for README extraction and bio regex. Conservative: requires a
# dot in the TLD, no whitespace, minimum reasonable length.
_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)
_MAILTO_RE = re.compile(
    r"mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
    re.IGNORECASE,
)

# Don't extract from these — pre-filter at parse time before scoring sees them.
_BAD_EMAIL_DOMAINS = (
    "example.com", "example.org", "example.net",
    "localhost", "test", "invalid",
)
_BAD_LOCALPARTS = ("noreply", "no-reply", "do-not-reply")

# Caller signatures match git_emails.py for consistency.
HttpGet = Callable[[str], str | None]   # url -> body or None on 404


def _is_extractable(email: str) -> bool:
    """Same logic as git_emails._is_noise_email but tuned for profile content.
    Less aggressive about bots (people don't list bots as their contact)."""
    if "@" not in email:
        return False
    local, _, domain = email.lower().partition("@")
    if not local or not domain:
        return False
    if any(domain == d or domain.endswith("." + d) for d in _BAD_EMAIL_DOMAINS):
        return False
    if any(local.startswith(lp) for lp in _BAD_LOCALPARTS):
        return False
    return True


# Local re-export so existing tests that monkey-patch
# `gh_profile._default_gh_caller` keep working.
def _default_gh_caller() -> GhCaller | None:
    return _gh_api.default_gh_caller()


def _default_http_get(url: str, *, timeout: float = _DEFAULT_TIMEOUT_SEC) -> str | None:
    """Fetch URL body as text. Returns None on 404; raises on other errors."""
    req = urllib.request.Request(url, headers={"User-Agent": "snoop-skill"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _extract_emails_from_text(text: str) -> list[tuple[str, str]]:
    """Return a list of (email, extraction_method) tuples from a block of text.

    `extraction_method` is "mailto" or "regex". Mailto-tagged hits get a
    higher trust weight at the scorer; raw-regex hits sometimes are
    obfuscated or false-positives.

    Deduplication is by normalized address (not by method), keeping the
    first occurrence's method (mailto wins if present).
    """
    if not text:
        return []
    found: dict[str, str] = {}
    for match in _MAILTO_RE.finditer(text):
        email = normalize_email(match.group(1))
        if _is_extractable(email) and email not in found:
            found[email] = "mailto"
    for match in _EMAIL_RE.finditer(text):
        email = normalize_email(match.group(0))
        if _is_extractable(email) and email not in found:
            found[email] = "regex"
    return [(email, method) for email, method in found.items()]


def fetch_gh_profile(
    handle: str,
    *,
    gh_caller: GhCaller | None = None,
    http_get: HttpGet | None = None,
    now: datetime | None = None,
) -> ResolverResult:
    """Resolve a target's GitHub profile and profile-README for declared emails.

    Args:
        handle: GitHub username.
        gh_caller: Optional injected API caller; defaults to gh-cli-then-HTTP.
        http_get: Optional injected raw-content fetcher; defaults to urllib.
        now: For deterministic tests.

    Returns:
        ResolverResult with EmailCandidate(s):
          - One per email found, each with a Source(type="gh_profile") or
            Source(type="gh_readme") (or both, if the same address appears
            in both surfaces).
          - status="ok" if any candidates found
          - status="empty" if profile loaded but no extractable email
          - status="error" / "timeout" / "unavailable" otherwise
    """
    start = datetime.now(timezone.utc) if now is None else now
    caller = gh_caller if gh_caller is not None else _default_gh_caller()
    if caller is None:
        return ResolverResult(
            resolver="gh_profile", candidates=[], status="unavailable",
            error_detail="no GitHub API access path available",
        )
    if not handle or not handle.strip():
        return ResolverResult(
            resolver="gh_profile", candidates=[], status="empty",
            error_detail="no handle provided",
        )

    by_addr: dict[str, list[Source]] = {}

    # --- Surface 1: GET /users/{handle} ---
    try:
        profile = caller(f"/users/{handle}")
    except subprocess.TimeoutExpired:
        return ResolverResult(
            resolver="gh_profile", candidates=[], status="timeout",
            error_detail="gh api /users/ timed out",
        )
    except (subprocess.SubprocessError, urllib.error.URLError,
            json.JSONDecodeError, OSError) as e:
        return ResolverResult(
            resolver="gh_profile", candidates=[], status="error",
            error_detail=f"profile fetch: {type(e).__name__}: {e}",
        )

    if isinstance(profile, dict):
        # Direct profile email field
        email_field = profile.get("email")
        if isinstance(email_field, str) and email_field.strip():
            email = normalize_email(email_field)
            if _is_extractable(email):
                src = Source(
                    type="gh_profile",
                    url=profile.get("html_url") or f"https://github.com/{handle}",
                    observed_at=start,
                    detail="public profile email",
                )
                by_addr.setdefault(email, []).append(src)

        # Bio can also contain a contact email occasionally
        bio = profile.get("bio")
        if isinstance(bio, str) and bio:
            for email, method in _extract_emails_from_text(bio):
                src = Source(
                    type="gh_profile",
                    url=profile.get("html_url") or f"https://github.com/{handle}",
                    observed_at=start,
                    detail=f"profile bio ({method})",
                )
                by_addr.setdefault(email, []).append(src)

    # --- Surface 2: profile README ---
    http = http_get if http_get is not None else _default_http_get
    readme_text: str | None = None
    readme_url = None
    for branch in ("main", "master"):
        candidate_url = (
            f"https://raw.githubusercontent.com/{handle}/{handle}/{branch}/README.md"
        )
        try:
            body = http(candidate_url)
        except (urllib.error.URLError, OSError):
            continue
        if body is not None:
            readme_text = body
            readme_url = f"https://github.com/{handle}/{handle}/blob/{branch}/README.md"
            break

    if readme_text:
        for email, method in _extract_emails_from_text(readme_text):
            src = Source(
                type="gh_readme",
                url=readme_url,
                observed_at=start,
                detail=f"profile README ({method})",
            )
            by_addr.setdefault(email, []).append(src)

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
        resolver="gh_profile",
        candidates=candidates,
        status=status,
        elapsed_ms=elapsed,
        error_detail=(
            "profile loaded but no extractable email found"
            if not candidates
            else None
        ),
    )
