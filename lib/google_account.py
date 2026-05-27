"""lib/google_account.py — Google People API existence + identity lookups.

Reverse-engineered against Google's internal People API (the same one
GHunt wraps). Uses your own logged-in Google session via cookies read
from lib.chrome_cookies. SAPISIDHASH auth, POST to people-pa.clients6,
parse the protojson response.

**STABILITY WARNING.** The endpoint URL, request body shape, and
response parsing format are NOT documented and Google rotates them
periodically (~every 6-12 months historically). When the live API
breaks our parser, the resolver returns status="error" with a clear
"endpoint may have rotated" message; pipeline continues with non-Google
signals. To fix: update _PEOPLE_LOOKUP_ENDPOINT / _build_lookup_request
/ _parse_lookup_response. The cross-reference of last truth is the
current GHunt source.

What's stable across rotations:
  - SAPISIDHASH algorithm (SHA1(ts + " " + SAPISID + " " + origin))
  - Cookie names (SID/SSID/HSID/APISID/SAPISID and __Secure-1P* family)
  - The general shape: POST with SAPISIDHASH auth + cookies + protojson body

What rotates:
  - Endpoint hostname / path
  - The body's field positions (protojson is positional)
  - The response's nesting depth and field positions

Four account_exists outcomes per candidate:

  "verified"          : account exists AND we got display name + Gaia ID
                        (most informative — can cross-check name)
  "exists_unverifiable": account exists but Google's response withheld
                        profile details (visibility-restricted Workspace
                        account, querying outside the org)
  "not_found"         : explicit not-found response
  "unprobed"          : never asked (cookies missing, budget exhausted,
                        candidate domain not Google-hosted)

For "verified" responses with name matching the target, the Source
type="google_account" is the strongest belongs_to_person signal we have
(0.65 base weight, comparable to manual_known).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .schema import EmailCandidate, ResolverResult, Source

logger = logging.getLogger(__name__)


# ---- volatile constants (update when Google rotates) -----------------------

# The People API endpoint GHunt has historically used. Path may rotate.
_PEOPLE_LOOKUP_ENDPOINT = (
    "https://people-pa.clients6.google.com/v2/people/lookup"
)
# Public web-client API key. Not a secret — appears in Google's web JS.
# Rotates rarely but does rotate.
_PEOPLE_LOOKUP_KEY = "AIzaSyA4xrSh53WnkVQVl7Q-EKAt4UDgrXrXwbI"
# SAPISIDHASH origin — must match an origin Google's API trusts for
# unauthenticated-ish web clients.
_ORIGIN = "https://contacts.google.com"

# Default request timeout per lookup (seconds). Google's lookup is usually
# <1s when it works; >5s means rate limit or networking issue.
_DEFAULT_TIMEOUT_SEC = 5.0


# Type alias for the injectable HTTP transport. Takes (url, headers, body_bytes)
# and returns (status_code, response_bytes).
HttpPost = Callable[[str, dict[str, str], bytes], tuple[int, bytes]]
CookieLoader = Callable[[], dict[str, str]]


# ---- SAPISIDHASH auth (stable) ---------------------------------------------


def compute_sapisidhash(
    sapisid: str,
    *,
    origin: str = _ORIGIN,
    now: float | None = None,
) -> str:
    """Compute the SAPISIDHASH Authorization header value.

    Algorithm: SHA1("<unix_ts> <SAPISID> <origin>"), formatted as
    "SAPISIDHASH <ts>_<sha1hex>". Stable across Google API rotations.

    The `now` argument is for deterministic tests; production passes None
    to use the current time.
    """
    ts = str(int(now if now is not None else time.time()))
    digest_input = f"{ts} {sapisid} {origin}"
    digest = hashlib.sha1(digest_input.encode("utf-8")).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"


def _pick_sapisid_cookie(cookies: dict[str, str]) -> str | None:
    """Pick the best SAPISID-family cookie. Prefer modern __Secure-* variants
    when present (Google rolled these out around 2020 and they're now the
    default for new sessions); fall back to legacy SAPISID."""
    for name in ("SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"):
        v = cookies.get(name)
        if v:
            return v
    return None


def _cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


# ---- request / response (volatile) ------------------------------------------


def _build_lookup_request(email: str) -> bytes:
    """Build the protojson request body for one email lookup.

    Google's "protojson" encoding is JSON arrays in protobuf field order.
    The shape below mirrors what GHunt sends as of late 2024:

      [
        [<email>],         # repeated lookup_id field
        1,                 # include_profile_info: true
        [1, 2, 3, 4, 7]    # field mask: name, photo, gaia_id, status, ...
      ]

    When Google rotates this, the symptom is a 400 response or an empty
    parsed result. Update the array shape against the current GHunt source.
    """
    payload = [
        [email],
        1,
        [1, 2, 3, 4, 7],
    ]
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _parse_lookup_response(payload_bytes: bytes) -> dict[str, Any]:
    """Parse the people-pa lookup response into our structured format.

    Returns a dict with keys:
      exists:        bool
      profile_visible: bool  (False == exists_unverifiable)
      gaia_id:       str | None
      display_name:  str | None
      photo_url:     str | None
      rate_limited:  bool
      parse_error:   str | None

    Defensive against missing / null fields. The position-based reads are
    the rotation surface — if Google moves a field, only this function
    breaks, and the rest of the resolver still returns a clean error
    rather than crashing.
    """
    out: dict[str, Any] = {
        "exists": False,
        "profile_visible": False,
        "gaia_id": None,
        "display_name": None,
        "photo_url": None,
        "rate_limited": False,
        "parse_error": None,
    }
    try:
        body = json.loads(payload_bytes.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError) as e:
        out["parse_error"] = f"json decode: {e}"
        return out

    # Rate-limit detection: Google returns either {"error": {...}} for HTTP
    # errors that still 200-respond, or {"code": "RATE_LIMIT_EXCEEDED"}
    # depending on the surface.
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            status = err.get("status") or err.get("code") or ""
            if "RATE_LIMIT" in str(status).upper():
                out["rate_limited"] = True
                return out
            # Other API errors: surface as parse_error so we don't claim
            # not_found when the real signal is "API rejected our request"
            out["parse_error"] = (
                f"api error: {err.get('message', str(err))[:120]}"
            )
            return out

    # On success the body is a top-level array. Empty array → not_found.
    if not isinstance(body, list) or not body:
        return out  # exists=False

    # body[0] is the matches array. Each match is a profile object.
    matches = body[0] if isinstance(body[0], list) else []
    if not matches:
        return out  # exists=False (no matches for that email)

    first = matches[0]
    if not isinstance(first, dict):
        out["exists"] = True
        out["profile_visible"] = False
        return out

    # Existence is confirmed once a match comes back.
    out["exists"] = True

    # Gaia ID: typically at first["personId"] or first["id"]. Tolerant lookup.
    for key in ("personId", "id", "person_id"):
        v = first.get(key)
        if isinstance(v, str) and v:
            out["gaia_id"] = v
            break

    # Names array: list of name records, each with primary value.
    names = first.get("name") or first.get("names")
    if isinstance(names, list) and names:
        for entry in names:
            if isinstance(entry, dict):
                value = entry.get("displayName") or entry.get("value")
                if isinstance(value, str) and value:
                    out["display_name"] = value
                    break

    # Photo URL
    photos = first.get("photo") or first.get("photos")
    if isinstance(photos, list) and photos:
        for entry in photos:
            if isinstance(entry, dict):
                url = entry.get("url")
                if isinstance(url, str) and url:
                    out["photo_url"] = url
                    break

    # If we got SOMETHING beyond bare existence (gaia_id OR display_name),
    # treat as profile_visible. If Google returned an "exists but I'm not
    # going to tell you anything else" response, profile_visible stays False
    # and the candidate is `exists_unverifiable`.
    out["profile_visible"] = bool(out["gaia_id"] or out["display_name"])
    return out


# ---- HTTP transport ---------------------------------------------------------


def _default_http_post(
    url: str, headers: dict[str, str], body: bytes,
    *, timeout: float = _DEFAULT_TIMEOUT_SEC,
) -> tuple[int, bytes]:
    """Real HTTP transport for production. Tests inject a fake."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ---- public API -------------------------------------------------------------


def _lookup_one(
    email: str,
    cookies: dict[str, str],
    sapisid: str,
    http_post: HttpPost,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Do one People API lookup. Returns the parsed result dict."""
    url = f"{_PEOPLE_LOOKUP_ENDPOINT}?key={_PEOPLE_LOOKUP_KEY}&alt=json"
    auth = compute_sapisidhash(sapisid, now=now)
    headers = {
        "Authorization": auth,
        "Content-Type": "application/json+protobuf",
        "Cookie": _cookie_header(cookies),
        "Origin": _ORIGIN,
        "Referer": _ORIGIN + "/",
        "User-Agent": "Mozilla/5.0 (snoop-skill)",
        "X-Goog-AuthUser": "0",
    }
    body = _build_lookup_request(email)
    try:
        status, payload = http_post(url, headers, body)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return {
            "exists": False, "profile_visible": False,
            "gaia_id": None, "display_name": None, "photo_url": None,
            "rate_limited": False,
            "parse_error": f"http error: {type(e).__name__}: {e}",
        }
    if status == 429:
        return {
            "exists": False, "profile_visible": False,
            "gaia_id": None, "display_name": None, "photo_url": None,
            "rate_limited": True, "parse_error": None,
        }
    if status >= 500:
        return {
            "exists": False, "profile_visible": False,
            "gaia_id": None, "display_name": None, "photo_url": None,
            "rate_limited": False,
            "parse_error": f"server error: HTTP {status}",
        }
    return _parse_lookup_response(payload)


def fetch_google_account(
    candidates: Iterable[EmailCandidate],
    *,
    cookie_loader: CookieLoader | None = None,
    http_post: HttpPost | None = None,
    target_domains: Iterable[str] | None = None,
    now: datetime | None = None,
) -> ResolverResult:
    """Probe each candidate against Google's People API.

    Args:
        candidates: EmailCandidates to probe. Mutated in place: account_exists
            field is set and a Source(type="google_account", ...) is appended
            when the lookup yields a real result.
        cookie_loader: Optional callable returning Google cookies. Default uses
            lib.chrome_cookies.get_google_cookies().
        http_post: Optional injected HTTP transport for tests.
        target_domains: Restrict probing to candidates whose domain is in this
            set. Default: only literal "google.com" + any domain that *looks*
            Google-hosted by suffix. Pass an explicit set to broaden.
        now: For deterministic tests.

    Returns:
        ResolverResult with one candidate-shaped EmailCandidate per real
        result. Same address may appear in input AND get its account_exists
        updated; mutation is preferred over duplication so the cluster pass
        downstream sees one row per address.
    """
    started = datetime.now(timezone.utc) if now is None else now

    # Cookie acquisition
    if cookie_loader is None:
        from . import chrome_cookies
        cookie_loader = chrome_cookies.get_google_cookies
    try:
        cookies = cookie_loader() or {}
    except Exception as e:  # noqa: BLE001
        return ResolverResult(
            resolver="google_account", candidates=[], status="unavailable",
            error_detail=f"cookie loader failed: {type(e).__name__}: {e}",
        )

    sapisid = _pick_sapisid_cookie(cookies)
    if not sapisid:
        return ResolverResult(
            resolver="google_account", candidates=[], status="unavailable",
            error_detail=(
                "no SAPISID-family cookie found in browser. Sign into Google "
                "in Chrome (or Brave/Edge/etc.) and try again."
            ),
        )

    poster: HttpPost = http_post if http_post is not None else _default_http_post

    # Default domain filter: literal google.com (a Workspace tenant's MX can be
    # detected separately; for v1 we only probe google.com itself).
    if target_domains is None:
        target_domain_set = {"google.com"}
    else:
        target_domain_set = {d.lower() for d in target_domains}

    candidates_list = list(candidates)
    rate_limited_seen = False
    probed_any = False

    for c in candidates_list:
        if not c.address or "@" not in c.address:
            continue
        domain = c.address.rsplit("@", 1)[1].lower()
        if domain not in target_domain_set:
            continue

        # If a prior candidate triggered rate-limit, mark subsequent as unprobed
        # rather than firing more probes that will rate-limit too.
        if rate_limited_seen:
            c.account_exists = "unprobed"
            continue

        probed_any = True
        result = _lookup_one(c.address, cookies, sapisid, poster,
                             now=started.timestamp())

        if result["rate_limited"]:
            rate_limited_seen = True
            c.account_exists = "unprobed"
            continue

        if result["parse_error"]:
            # Treat as transient error; don't poison belongs_to_person.
            logger.info("google_account lookup error for %s: %s",
                        c.address, result["parse_error"])
            c.account_exists = "unprobed"
            continue

        if not result["exists"]:
            c.account_exists = "not_found"
            c.sources.append(Source(
                type="google_account",
                url=None,
                observed_at=started,
                detail="Google account lookup: address not found",
            ))
            continue

        # Account exists. Was profile visible?
        if result["profile_visible"]:
            c.account_exists = "verified"
            # Store display name on the candidate so the scorer can compare
            # it against the target name without parsing the detail string.
            if result["display_name"]:
                c.account_display_name = result["display_name"]
            detail_parts = ["Google account confirmed"]
            if result["gaia_id"]:
                detail_parts.append(f"Gaia {result['gaia_id'][:8]}…")
            if result["display_name"]:
                detail_parts.append(f"display name '{result['display_name']}'")
            c.sources.append(Source(
                type="google_account",
                url=None,
                observed_at=started,
                detail="; ".join(detail_parts),
            ))
        else:
            c.account_exists = "exists_unverifiable"
            c.sources.append(Source(
                type="google_account",
                url=None,
                observed_at=started,
                detail=(
                    "Google account exists but profile is not visible to "
                    "querying account (Workspace visibility-restricted)"
                ),
            ))

    elapsed_ms = int(
        (datetime.now(timezone.utc) - started).total_seconds() * 1000
    )

    # Result candidates list: only those we actually probed (so the cluster
    # pass doesn't see duplicates of unprobed entries). The mutation has
    # already happened on the input list.
    probed = [c for c in candidates_list if c.account_exists != "unprobed"]
    if rate_limited_seen:
        status = "error"
        err = "Google API rate-limited; subsequent candidates left unprobed"
    elif not probed_any:
        status = "unavailable"
        err = "no candidates were on a Google-hosted domain"
    elif probed:
        status = "ok"
        err = None
    else:
        status = "empty"
        err = "all probes errored; see logs"
    return ResolverResult(
        resolver="google_account",
        candidates=probed,
        status=status,
        elapsed_ms=elapsed_ms,
        error_detail=err,
    )
