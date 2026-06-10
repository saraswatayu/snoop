"""lib/package_registry.py — publisher emails from npm + PyPI metadata.

When the host model knows the target published a package (it supplies
`plan["packages"]` as a list of `{"registry", "name"}` dicts), this
resolver reads the registry metadata and extracts whatever publisher
emails are still exposed.

Two registries, two metadata shapes:

  npm: GET https://registry.npmjs.org/{name}
    - `maintainers[].email` — the current maintainer set
    - `author.email` — the top-level package author
    - `versions[{latest}].author` / `.maintainers` — per-release author
      info, sometimes populated when the top-level fields aren't
    npm has been redacting most maintainer emails since ~2022, so many
    come back as `npm@npmjs.com` or are simply absent — that's expected.
    We extract what's there and let the noise filter drop the placeholders.
    Source url: https://www.npmjs.com/package/{name}

  pypi: GET https://pypi.org/pypi/{name}/json
    - `info.author_email` / `info.maintainer_email` — these follow the
      RFC 822 address form and can be "Name <addr@host>" and/or
      comma-separated ("A <a@x>, B <b@y>"). We parse out the addresses.
    Source url: https://pypi.org/project/{name}/

Output: one EmailCandidate per distinct (lowercased) address, de-duped
across ALL packages. If the same address shows up in two packages, the
candidate carries one Source per package it was seen in. Every Source is
tagged `type="package_registry"`.

A single package failing to fetch (network error, 404, garbage JSON)
does NOT fail the batch — it's skipped and the rest continue. The public
function never raises; the worst case is status="empty" or "error".

This module re-implements a small local noise filter (mirroring
git_emails / gh_profile) rather than importing their private helpers, so
the sensors stay decoupled.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import getaddresses
from typing import Callable

from .normalize import normalize_email
from .schema import EmailCandidate, ResolverResult, Source

_DEFAULT_TIMEOUT_SEC = 6.0

# Conservative address shape: requires a dotted TLD, no whitespace.
_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)

# Placeholder / redacted / no-reply addresses to drop. Mirrors the noise
# filters in git_emails and gh_profile but tuned for registry metadata —
# npm in particular hands out `npm@npmjs.com` as a redaction sentinel.
_NOISE_DOMAINS = (
    "example.com", "example.org", "example.net",
    "localhost", "local", "test", "invalid",
)
_NOISE_LOCALPART_PREFIXES = ("noreply", "no-reply", "do-not-reply")
# Exact addresses npm/PyPI emit as redaction or boilerplate sentinels.
_NOISE_EXACT = (
    "npm@npmjs.com",
    "support@npmjs.com",
    "redacted@npmjs.com",
)

# Injectable fetcher signature: (url, timeout=...) -> dict.
HttpGetJson = Callable[..., dict]


def _is_extractable(email: str) -> bool:
    """True if `email` is a plausible personal/publisher address worth keeping.

    Drops malformed strings, placeholder domains, no-reply localparts,
    obvious redaction sentinels, and anything containing an asterisk
    (npm sometimes returns `***@***` style masks).
    """
    if not email or "@" not in email:
        return False
    if "*" in email:
        return False
    if not _EMAIL_RE.fullmatch(email):
        return False
    local, _, domain = email.lower().partition("@")
    if not local or not domain:
        return False
    if email.lower() in _NOISE_EXACT:
        return False
    if any(domain == d or domain.endswith("." + d) for d in _NOISE_DOMAINS):
        return False
    if any(local.startswith(lp) for lp in _NOISE_LOCALPART_PREFIXES):
        return False
    return True


def _parse_addresses(raw: str | None) -> list[str]:
    """Extract email addresses from a metadata field.

    Handles plain "a@b.com", RFC-822 "Name <a@b.com>", and comma-separated
    lists ("A <a@x>, b@y"). Returns normalized (lowercased localpart,
    IDNA domain) addresses — unfiltered; the caller applies _is_extractable.
    """
    if not raw or not isinstance(raw, str):
        return []
    out: list[str] = []
    seen: set[str] = set()
    # getaddresses understands "Name <addr>, Name2 <addr2>" robustly.
    for _name, addr in getaddresses([raw]):
        if not addr:
            continue
        norm = normalize_email(addr)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    # Defensive fallback: if getaddresses found nothing (malformed wrapping),
    # sweep the raw text with the email regex.
    if not out:
        for m in _EMAIL_RE.finditer(raw):
            norm = normalize_email(m.group(0))
            if norm and norm not in seen:
                seen.add(norm)
                out.append(norm)
    return out


def _default_http_get_json(url: str, *, timeout: float = _DEFAULT_TIMEOUT_SEC) -> dict:
    """Fetch and JSON-parse a URL body. Returns {} on any error (404, network,
    decode) so a single bad package never bubbles an exception into the batch."""
    req = urllib.request.Request(url, headers={"User-Agent": "snoop-skill"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {}
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            json.JSONDecodeError, ValueError):
        return {}


def _collect_npm(name: str, data: dict, observed_at: datetime,
                 by_addr: dict[str, list[Source]]) -> None:
    """Extract emails from an npm registry document into `by_addr`."""
    project_url = f"https://www.npmjs.com/package/{name}"

    def add(field: str | None) -> None:
        for addr in _parse_addresses(field):
            if _is_extractable(addr):
                by_addr.setdefault(addr, []).append(Source(
                    type="package_registry",
                    url=project_url,
                    observed_at=observed_at,
                    detail=f"npm publisher email for {name}",
                ))

    def add_author(author: object) -> None:
        if isinstance(author, dict):
            add(author.get("email"))
        elif isinstance(author, str):
            add(author)

    def add_maintainers(maintainers: object) -> None:
        if isinstance(maintainers, list):
            for m in maintainers:
                if isinstance(m, dict):
                    add(m.get("email"))
                elif isinstance(m, str):
                    add(m)

    add_maintainers(data.get("maintainers"))
    add_author(data.get("author"))

    # Latest published version's per-release author/maintainers.
    latest = (data.get("dist-tags") or {}).get("latest")
    versions = data.get("versions")
    if isinstance(latest, str) and isinstance(versions, dict):
        ver = versions.get(latest)
        if isinstance(ver, dict):
            add_author(ver.get("author"))
            add_maintainers(ver.get("maintainers"))


def _collect_pypi(name: str, data: dict, observed_at: datetime,
                  by_addr: dict[str, list[Source]]) -> None:
    """Extract emails from a PyPI JSON document into `by_addr`."""
    project_url = f"https://pypi.org/project/{name}/"
    info = data.get("info")
    if not isinstance(info, dict):
        return
    for field in ("author_email", "maintainer_email"):
        for addr in _parse_addresses(info.get(field)):
            if _is_extractable(addr):
                by_addr.setdefault(addr, []).append(Source(
                    type="package_registry",
                    url=project_url,
                    observed_at=observed_at,
                    detail=f"pypi publisher email for {name}",
                ))


def fetch_package_emails(
    packages: list[dict],
    *,
    http_get_json: HttpGetJson | None = None,
) -> ResolverResult:
    """Read npm/PyPI metadata for the given packages and surface publisher emails.

    Args:
        packages: list of `{"registry": "npm"|"pypi", "name": "<package>"}`
            dicts. Malformed entries (missing/blank name, unknown registry,
            non-dict) are ignored defensively.
        http_get_json: injectable `(url, timeout=...) -> dict` fetcher;
            defaults to `_default_http_get_json` (urllib + json, errors
            swallowed to `{}`). Tests pass a fake routing dict.

    Returns:
        ResolverResult(resolver="package_registry", ...):
          - status="ok" with one EmailCandidate per distinct address
            (de-duped across all packages; multiple Sources when seen in
            more than one package).
          - status="empty" when no extractable address was found.
          - status="error" only on a hard failure (e.g. `packages` is not
            a list); a single package fetch failing is NOT a hard failure.
        Never raises.
    """
    start = datetime.now(timezone.utc)
    fetch = http_get_json if http_get_json is not None else _default_http_get_json

    if not isinstance(packages, list):
        return ResolverResult(
            resolver="package_registry", candidates=[], status="error",
            error_detail="packages must be a list",
        )

    by_addr: dict[str, list[Source]] = {}
    notes: list[str] = []

    for entry in packages:
        if not isinstance(entry, dict):
            continue
        registry = entry.get("registry")
        name = entry.get("name")
        if not isinstance(registry, str) or not isinstance(name, str):
            continue
        registry = registry.strip().lower()
        name = name.strip()
        if not name or registry not in ("npm", "pypi"):
            continue

        if registry == "npm":
            url = f"https://registry.npmjs.org/{name}"
        else:
            url = f"https://pypi.org/pypi/{name}/json"

        try:
            data = fetch(url, timeout=_DEFAULT_TIMEOUT_SEC)
        except Exception as e:  # noqa: BLE001 — one package must never sink the batch
            notes.append(f"{registry}:{name} fetch failed: {type(e).__name__}")
            continue
        if not isinstance(data, dict) or not data:
            notes.append(f"{registry}:{name} returned no metadata")
            continue

        try:
            if registry == "npm":
                _collect_npm(name, data, start, by_addr)
            else:
                _collect_pypi(name, data, start, by_addr)
        except Exception as e:  # noqa: BLE001 — defensive: malformed shapes
            notes.append(f"{registry}:{name} parse failed: {type(e).__name__}")
            continue

    candidates = [
        EmailCandidate(
            address=addr,
            sources=sorted(srcs, key=lambda s: -s.observed_at.timestamp()),
        )
        for addr, srcs in by_addr.items()
    ]
    elapsed = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    status = "ok" if candidates else "empty"
    error_detail = None
    if not candidates:
        error_detail = (
            "; ".join(notes) if notes
            else "no publisher email found in package metadata"
        )
    return ResolverResult(
        resolver="package_registry",
        candidates=candidates,
        status=status,
        elapsed_ms=elapsed,
        error_detail=error_detail,
    )
