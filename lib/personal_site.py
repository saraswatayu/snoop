"""lib/personal_site.py — mailto: anchors from a target's declared personal domains.

Subagent finding F7: HTML scraping fragility is well-documented (Cloudflare
email obfuscation, JS-rendered SPAs, image-embedded emails). The plan's
response was to scope v1 hard: mailto: anchors only. No raw-text email
regex, no Cloudflare deobfuscation, no JS rendering. Defer those to v2.

A mailto: anchor is the highest-precision signal we can extract from an
arbitrary HTML page. The author EXPLICITLY put a clickable email link
there. Even when the visible text is obfuscated, the anchor's href is
not — unless Cloudflare's email-protection script has rewritten it,
which it sometimes does (the rewritten href is `/cdn-cgi/l/email-protection`
and we skip those).

For each declared `personal_domain`, this resolver tries up to N common
paths: `/`, `/about`, `/contact`. Each fetch is per-call timed; the outer
resolver fan-out enforces a wall-clock cap via `future.result(timeout=5)`.

Generic-inbox addresses (`info@`, `hello@`, `contact@`) are KEPT, not
dropped — many one-person consultancies legitimately publish
`hello@theirname.com` as their personal address. The scorer will
downrank these via a generic-localpart check. System-only addresses
(`noreply@`, `webmaster@`, `postmaster@`, `abuse@`) ARE dropped here.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable

from .normalize import domain_is_noise, normalize_email
from .schema import EmailCandidate, ResolverResult, Source

_DEFAULT_TIMEOUT_SEC = 4.0
_PATHS_TO_TRY = ("/", "/about", "/contact")

# Drop these system-only addresses outright (EXACT localpart match —
# deliberately stricter than the prefix policy in the profile sensors).
# Generic-but-legitimate localparts (info@, hello@, contact@) are kept; the
# scorer downranks them. The reserved-domain set is shared (lib.normalize).
_SYSTEM_LOCALPARTS = (
    "noreply", "no-reply", "do-not-reply",
    "postmaster", "abuse", "webmaster",
    "mailer-daemon",
)

# Match `href="mailto:foo@bar.com"` or `href='mailto:foo@bar.com?subject=...'`.
# Stop on query string, fragment, header separator, whitespace, or closing
# quote. `&` is excluded because raw HTML often contains `&amp;` for header
# separators — without the exclusion, the encoded `&amp;subject=…` leaks into
# the captured address. `#` is excluded for the same reason against fragments.
_MAILTO_HREF_RE = re.compile(
    r"""href\s*=\s*['"]\s*mailto:\s*([^'"?#&\s]+)""",
    re.IGNORECASE,
)

# Cloudflare email protection rewrites real mailto: links into
# /cdn-cgi/l/email-protection#<encoded>. Detect and skip — we don't
# deobfuscate in v1.
_CLOUDFLARE_PROTECTED_RE = re.compile(
    r"/cdn-cgi/l/email-protection", re.IGNORECASE,
)


HttpGet = Callable[[str], str | None]  # url -> body or None on 404


def _is_extractable(email: str) -> bool:
    """Drop only system-only mailto addresses. Generic-but-legitimate
    locals (hello@, info@) pass through; scorer handles them."""
    if not email or "@" not in email:
        return False
    local, _, domain = email.lower().partition("@")
    if not local or not domain:
        return False
    if local in _SYSTEM_LOCALPARTS:
        return False
    if domain_is_noise(domain):
        return False
    return True


def _extract_mailto_addresses(html: str) -> list[str]:
    """Return normalized mailto addresses from HTML, in document order.

    Deduplicates while preserving first-occurrence order. Skips Cloudflare-
    protected stub anchors entirely.
    """
    if not html:
        return []
    seen: dict[str, None] = {}  # ordered set via dict
    for match in _MAILTO_HREF_RE.finditer(html):
        raw = match.group(1).strip()
        # Skip Cloudflare's protected stub href values — these don't contain
        # the real address.
        if _CLOUDFLARE_PROTECTED_RE.search(raw):
            continue
        # urldecode common cases (e.g. %40 → @)
        raw = urllib.parse.unquote(raw)
        email = normalize_email(raw)
        if _is_extractable(email) and email not in seen:
            seen[email] = None
    return list(seen.keys())


def _default_http_get(url: str, *, timeout: float = _DEFAULT_TIMEOUT_SEC) -> str | None:
    """Fetch URL body as text. Returns None on 404; raises on connection error."""
    req = urllib.request.Request(url, headers={"User-Agent": "snoop-skill"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Cap response size to avoid pathological downloads.
            body = resp.read(2_000_000)  # 2MB cap
            # Best-effort charset detection. Most HTML is utf-8 or ISO-8859-1.
            charset = resp.headers.get_content_charset() or "utf-8"
            try:
                return body.decode(charset, errors="replace")
            except (LookupError, TypeError):
                return body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return None
        raise


def fetch_personal_site(
    personal_domains: list[str],
    *,
    http_get: HttpGet | None = None,
    now: datetime | None = None,
    paths_to_try: tuple[str, ...] = _PATHS_TO_TRY,
) -> ResolverResult:
    """Pull mailto: anchor addresses from a target's personal websites.

    Args:
        personal_domains: List of domains to scrape (e.g. ["steipete.com"]).
            Already-normalized (lowercased, IDNA-encoded) is preferred.
        http_get: Optional injected fetcher (url -> body|None).
        now: For deterministic tests.
        paths_to_try: Per-domain paths. Default: "/", "/about", "/contact".

    Returns:
        ResolverResult. Status:
          - "ok": ≥1 mailto address extracted
          - "empty": fetched at least one page successfully but no mailto found
          - "error": all fetch attempts failed
          - "unavailable": no domains given (resolver had nothing to do)
    """
    start = datetime.now(timezone.utc) if now is None else now
    if not personal_domains:
        return ResolverResult(
            resolver="personal_site", candidates=[], status="unavailable",
            error_detail="no personal_domains provided",
        )
    http = http_get if http_get is not None else _default_http_get

    by_addr: dict[str, list[Source]] = {}
    any_fetch_succeeded = False
    fetch_errors: list[str] = []

    for domain in personal_domains:
        if not domain or not domain.strip():
            continue
        domain = domain.strip().lower()
        for path in paths_to_try:
            url = f"https://{domain}{path}"
            try:
                body = http(url)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                fetch_errors.append(f"{url}: {type(e).__name__}")
                continue
            if body is None:
                # 404/410 — try the next path
                continue
            any_fetch_succeeded = True
            for email in _extract_mailto_addresses(body):
                src = Source(
                    type="personal_site",
                    url=url,
                    observed_at=start,
                    detail=f"mailto: anchor on {url}",
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

    if candidates:
        status = "ok"
        error_detail = None
    elif any_fetch_succeeded:
        status = "empty"
        error_detail = (
            "personal sites loaded but no mailto: anchors found "
            "(v1 mailto-only; raw email regex deferred to v2)"
        )
    else:
        status = "error"
        error_detail = (
            f"all fetches failed for domains {personal_domains}: "
            + "; ".join(fetch_errors[:3])
        )

    return ResolverResult(
        resolver="personal_site",
        candidates=candidates,
        status=status,
        elapsed_ms=elapsed,
        error_detail=error_detail,
    )
