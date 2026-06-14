"""lib/pgp_keyserver.py — owner-verified email confirmation via keys.openpgp.org.

keys.openpgp.org is a *validating* keyserver: it only publishes an email as a
key UID after the address owner clicked a confirmation link sent to that
address. So a hit on its by-email endpoint is an OWNER-VERIFIED positive by
construction — the person demonstrably controlled that inbox.

The Verifying Keyserver (VKS) by-email endpoint:

    GET https://keys.openpgp.org/vks/v1/by-email/<url-encoded-email>

returns:
  - 200 + an ASCII-armored key (Content-Type: application/pgp-keys) when that
    email is a verified UID, and
  - 404 when the address isn't a verified UID on the server.

We do NOT parse the returned key (no PGP-library dependency): the 200 status
*is* the verified-ownership signal. A non-200 (404 or anything else) means "not
on the keyserver" and yields no candidate.

This sensor fetches arbitrary, target-influenced URLs (the email address shapes
the path), so it goes through `lib.fetch.fetch` — the SSRF-hardened GET. Tests
inject a fake `fetch_fn` to stay off the network.

Evidence tier note (CEO plan T-minor C): a keys.openpgp.org hit is a strong
owner-verified positive — the host marks it [+]. This sensor does not set tiers
(the host does); the Source.detail says no more than "verified UID on
keys.openpgp.org".
"""

from __future__ import annotations

import ssl
import urllib.parse
from datetime import datetime, timezone
from typing import Callable

from .fetch import FetchBlocked, FetchResult
from .fetch import fetch as _default_fetch
from .normalize import SYNTAX_RE, normalize_email
from .schema import EmailCandidate, ResolverResult, Source

# Base of the VKS by-email endpoint. The url-encoded address is appended.
_VKS_BY_EMAIL_BASE = "https://keys.openpgp.org/vks/v1/by-email/"

# The server answers with an ASCII-armored key (application/pgp-keys). We accept
# text/plain too, since some intermediaries relabel the armored block and we
# only care about the status, not the body.
_ALLOWED_CONTENT_TYPES = frozenset({"application/pgp-keys", "text/plain"})

# The conservative address validator is shared (lib.normalize.SYNTAX_RE):
# malformed addresses are skipped before we ever hit the network.

# Bound the number of network queries per batch. Keyserver lookups are cheap but
# unbounded input shouldn't translate into unbounded I/O. If the caller passes
# more than this many distinct valid emails, we query the first N and stop.
_QUERY_BUDGET = 8

# A fetch_fn(url, *, allowed_content_types=...) -> FetchResult. Defaults to the
# SSRF-hardened lib.fetch.fetch; tests inject a fake.
FetchFn = Callable[..., FetchResult]


def by_email_url(email: str) -> str:
    """Build the VKS by-email URL for `email`, url-encoding the whole address.

    The address is encoded with `quote(..., safe="")` so that '+', '@', and any
    unicode are percent-escaped into a single path segment — '+' must not be
    left as a literal (it would otherwise be ambiguous with a space) and unicode
    must be UTF-8 percent-encoded.
    """
    return _VKS_BY_EMAIL_BASE + urllib.parse.quote(email, safe="")


def _is_valid_email(email: str) -> bool:
    return bool(email) and bool(SYNTAX_RE.match(email))


def fetch_pgp_emails(
    emails: list[str],
    *,
    fetch_fn: FetchFn | None = None,
) -> ResolverResult:
    """Confirm which of `emails` are owner-verified UIDs on keys.openpgp.org.

    For each unique, syntactically-valid address (deduped, lowercased; malformed
    skipped), GET the VKS by-email endpoint. A 200 means the address is a
    verified keyserver UID → emit an owner-verified EmailCandidate. A non-200
    (404 or other) means not on the keyserver → no candidate. A per-email guard
    refusal (FetchBlocked) or network/TLS error (OSError / ssl.SSLError) skips
    that address but never sinks the batch.

    Args:
        emails: Candidate addresses to confirm against the keyserver.
        fetch_fn: Optional injected fetcher; defaults to lib.fetch.fetch.

    Returns:
        ResolverResult(resolver="pgp"):
          - status="ok" with one Source(type="pgp") per verified address,
          - status="empty" if none verified (or nothing to query),
          - status="error" only on a hard, non-recoverable failure.
        Never raises.
    """
    start = datetime.now(timezone.utc)
    fetch = fetch_fn if fetch_fn is not None else _default_fetch

    if not emails:
        return ResolverResult(
            resolver="pgp", candidates=[], status="empty",
            error_detail="no emails to query",
        )

    # Dedupe + lowercase (via normalize_email) and keep only valid addresses,
    # preserving first-seen order so the budget truncation is deterministic.
    queryable: list[str] = []
    seen: set[str] = set()
    for raw in emails:
        if not isinstance(raw, str):
            continue
        email = normalize_email(raw)
        if not _is_valid_email(email) or email in seen:
            continue
        seen.add(email)
        queryable.append(email)

    if not queryable:
        return ResolverResult(
            resolver="pgp", candidates=[], status="empty",
            error_detail="no syntactically-valid emails to query",
        )

    truncated = len(queryable) > _QUERY_BUDGET
    queryable = queryable[:_QUERY_BUDGET]

    candidates: list[EmailCandidate] = []
    for email in queryable:
        url = by_email_url(email)
        try:
            result = fetch(url, allowed_content_types=_ALLOWED_CONTENT_TYPES)
        except (FetchBlocked, OSError, ssl.SSLError):
            # Guard refusal or network/TLS error for this address — skip it and
            # keep going. One bad lookup must not fail the batch.
            continue
        if getattr(result, "status", None) != 200:
            # 404 (not a verified UID) or any other non-200 → no candidate.
            continue
        candidates.append(EmailCandidate(
            address=email,
            sources=[Source(
                type="pgp",
                url=url,
                observed_at=start,
                detail="verified UID on keys.openpgp.org",
            )],
        ))

    elapsed = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    status = "ok" if candidates else "empty"
    if candidates:
        error_detail = None
    elif truncated:
        error_detail = (
            f"no verified UIDs among first {_QUERY_BUDGET} of "
            f"{len(seen)} candidate emails"
        )
    else:
        error_detail = "no candidate emails are verified UIDs on keys.openpgp.org"

    return ResolverResult(
        resolver="pgp",
        candidates=candidates,
        status=status,
        elapsed_ms=elapsed,
        error_detail=error_detail,
    )
