"""lib/hn_profile.py — public email from a Hacker News user profile.

Hacker News (news.ycombinator.com) renders a per-user profile page at
`/user?id=<handle>`. When the user opts in, the profile table includes an
`email:` row; the freeform `about:` box can also carry a contact email.
Both surfaces are public and self-declared, so an address found there is a
reasonable contact signal — the same flavor of "what they publish as their
identity" that `gh_profile` reads off GitHub.

Scope (mirrors personal_site / gh_profile v1):

  - Extract real text/`mailto:` emails from the email row AND the about text.
  - HTML-unescape entities first (HN encodes `@` as `&#x40;`/`&#64;` and
    `.` as `&#x2e;` in some renders), so entity-obfuscated addresses decode.
  - No "name at domain dot com" word-deobfuscation, no JS rendering — out of
    scope, matching the other HTTP sensors.

Noise filtering replicates the small local filter used by gh_profile /
git_emails (placeholders, no-reply, system addresses). We deliberately do
NOT import those private helpers; the filter is replicated locally so this
module stays self-contained.

Returns Sources tagged `Source(type="hn_profile", ...)` (already in
schema.SourceType) so the renderer attributes correctly.
"""

from __future__ import annotations

import html
import urllib.parse
from datetime import datetime, timezone
from typing import Callable

from .fetch import fetch
from .normalize import EMAIL_RE, MAILTO_RE, domain_is_noise, normalize_email
from .schema import EmailCandidate, ResolverResult, Source

_DEFAULT_TIMEOUT_SEC = 6.0

_PROFILE_URL = "https://news.ycombinator.com/user?id={handle}"

# Address shape + reserved-domain set are shared (lib.normalize); the localpart
# policy is local.
_BAD_LOCALPARTS = ("noreply", "no-reply", "do-not-reply")


HttpGet = Callable[[str], str | None]  # url -> body or None on 404


def _is_extractable(email: str) -> bool:
    """True if `email` is a plausible personal contact address.

    Replicated locally (not imported from gh_profile/git_emails) so this
    module is self-contained: rejects placeholders, no-reply localparts,
    and malformed addresses.
    """
    if not email or "@" not in email:
        return False
    local, _, domain = email.lower().partition("@")
    if not local or not domain:
        return False
    if domain_is_noise(domain):
        return False
    if any(local.startswith(lp) for lp in _BAD_LOCALPARTS):
        return False
    return True


def _extract_emails_from_text(text: str) -> list[str]:
    """Return normalized, deduped, extractable emails from a block of text.

    HTML entities are unescaped first so entity-obfuscated addresses (e.g.
    `name&#x40;domain.com`) decode before matching. Order preserved by first
    occurrence; mailto: hrefs are scanned before raw-text matches.
    """
    if not text:
        return []
    decoded = html.unescape(text)
    seen: dict[str, None] = {}  # ordered set via dict
    for match in MAILTO_RE.finditer(decoded):
        email = normalize_email(match.group(1))
        if _is_extractable(email) and email not in seen:
            seen[email] = None
    for match in EMAIL_RE.finditer(decoded):
        email = normalize_email(match.group(0))
        if _is_extractable(email) and email not in seen:
            seen[email] = None
    return list(seen.keys())


def _default_http_get(url: str, *, timeout: float = _DEFAULT_TIMEOUT_SEC) -> str | None:
    """Fetch URL body as text through the SSRF-hardened lib.fetch (https-only,
    public-IP-pinned, redirect-revalidated, body-capped). Returns None on
    404/410; lets FetchBlocked / network (OSError) propagate so the caller marks
    the sensor degraded."""
    res = fetch(url, timeout=timeout)
    if res.status in (404, 410):
        return None
    return res.text


def fetch_hn_profile(handle: str, *, http_get: HttpGet | None = None) -> ResolverResult:
    """Resolve a target's Hacker News profile for a declared email.

    Fetches `https://news.ycombinator.com/user?id=<handle>` and extracts any
    email from both the `email:` row and the `about:` text.

    Args:
        handle: HN username (from the person plan's `handles.hn`).
        http_get: Optional injected fetcher (url -> body|None) for testing;
            defaults to a urllib-backed fetch.

    Returns:
        ResolverResult, one EmailCandidate per distinct address. Status:
          - "ok": ≥1 extractable email found
          - "empty": profile loaded but no extractable email
          - "unavailable": no handle provided (resolver had nothing to do)
          - "error": fetch/parse raised, or the profile page 404'd
    """
    start = datetime.now(timezone.utc)

    if not handle or not handle.strip():
        return ResolverResult(
            resolver="hn_profile", candidates=[], status="unavailable",
            error_detail="no handle provided",
        )

    handle = handle.strip()
    url = _PROFILE_URL.format(handle=urllib.parse.quote(handle, safe=""))
    http = http_get if http_get is not None else _default_http_get

    try:
        body = http(url)
    except Exception as e:  # noqa: BLE001 — never raise out of the public fn
        return ResolverResult(
            resolver="hn_profile", candidates=[], status="error",
            error_detail=f"{type(e).__name__}: {e}",
        )

    if body is None:
        # 404/410 — no such public profile.
        return ResolverResult(
            resolver="hn_profile", candidates=[], status="error",
            error_detail=f"HN profile not found: {url}",
        )

    emails = _extract_emails_from_text(body)

    candidates = [
        EmailCandidate(
            address=addr,
            sources=[Source(
                type="hn_profile",
                url=url,
                observed_at=start,
                detail="public email on Hacker News profile",
            )],
        )
        for addr in emails
    ]
    elapsed = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    status = "ok" if candidates else "empty"
    return ResolverResult(
        resolver="hn_profile",
        candidates=candidates,
        status=status,
        elapsed_ms=elapsed,
        error_detail=(
            "HN profile loaded but no extractable email found"
            if not candidates
            else None
        ),
    )
