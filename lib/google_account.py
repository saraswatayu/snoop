"""lib/google_account.py — Google People API existence + identity lookups.

Calls Google's internal People API (the same one GHunt wraps). Uses your
own logged-in Google session via cookies read from lib.chrome_cookies.
SAPISIDHASH auth, GET to people-pa.clients6.google.com/v2/people/lookup,
parse the JSON response.

**STABILITY WARNING.** The endpoint URL, API key, origin pairing, query
param schema, and response shape are all reverse-engineered against
GHunt's current source (the closest thing to a working reference). When
the live API breaks our parser, the resolver returns status="error" with
a clear message; the pipeline continues with non-Google signals. To fix:
re-read GHunt's `apis/peoplepa.py` and `knowledge/keys.py`, then update
the matching constants here.

What's stable across rotations:
  - SAPISIDHASH algorithm (SHA1(ts + " " + SAPISID + " " + origin))
  - Cookie names (SID/SSID/HSID/APISID/SAPISID and __Secure-1P* family)
  - The general shape: GET with X-Goog-Api-Key + SAPISIDHASH auth + cookies

What rotates (and breaks us when it does):
  - _LOOKUP_URL (endpoint hostname / path)
  - _API_KEY (Google's photos web client key)
  - _ORIGIN (must match the key's registered origin — SAPISIDHASH validates
    against the origin server-side, so a mismatch silently 401s)
  - Query param names / response field nesting

Four account_exists outcomes per candidate:

  "verified"          : account exists AND we got at least personId
                        (and usually display name) — strongest signal
  "exists_unverifiable": Google acknowledged existence but withheld
                        profile details (Workspace visibility-restricted)
  "not_found"         : explicit not-found response (empty people dict)
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
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .normalize import name_match
from .schema import EmailCandidate, ResolverResult, Source

logger = logging.getLogger(__name__)


# ---- volatile constants (mirror GHunt's apis/peoplepa.py + knowledge/keys.py) ----

# Endpoint. Same as GHunt's. Path rotates rarely; full URL is what changes.
_LOOKUP_URL = "https://people-pa.clients6.google.com/v2/people/lookup"

# Google's "photos" web client API key. This is the key GHunt uses for the
# People API lookup. The origin the server expects for SAPISIDHASH is THIS
# key's registered origin, not contacts.google.com (which is why our v1
# implementation silently 401'd every request).
_API_KEY = "AIzaSyAa2odBewW-sPJu3jMORr0aNedh3YlkiQc"
_ORIGIN = "https://photos.google.com"

# Field paths and containers we request. Minimal subset of GHunt's
# "max_details" template — we don't need Dynamite/Chat extension data,
# just enough to tell existence apart from visibility-restricted and
# (when available) to pull a display name for name-matching.
_FIELD_PATHS = [
    "person.metadata",
    "person.metadata.best_display_name",
    "person.name",
    "person.email",
    "person.photo",
    "person.read_only_profile_info",
]
_CONTAINERS = [
    "PROFILE",
    "DOMAIN_PROFILE",
    "ACCOUNT",
    "EXTERNAL_ACCOUNT",
]

# Default request timeout per lookup (seconds). Google's lookup is usually
# <1s when it works; >5s means rate limit or networking issue.
_DEFAULT_TIMEOUT_SEC = 5.0


# Type aliases for the injectable transports.
# HttpGet: (url, params, headers) -> (status, response_bytes).
# params is a list of (key, value) tuples to preserve multi-valued keys.
HttpGet = Callable[[str, list[tuple[str, str]], dict[str, str]],
                   tuple[int, bytes]]
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
    """Pick the best SAPISID-family cookie. Prefer the legacy SAPISID when
    present (GHunt's gen_sapisidhash uses it exclusively); fall back to the
    modern __Secure-* variants for browsers that have rotated to those."""
    for name in ("SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"):
        v = cookies.get(name)
        if v:
            return v
    return None


def _cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


# ---- request / response (volatile) ------------------------------------------


def _build_lookup_params(email: str) -> list[tuple[str, str]]:
    """Build URL query params for one email lookup.

    Returned as a list of (key, value) tuples so multi-valued keys
    (request_mask.include_field.paths, request_mask.include_container)
    survive urlencode(doseq=True) intact — Google expects each as a
    separate ?key=val repeat, not a single comma-joined value.
    """
    params: list[tuple[str, str]] = [
        ("id", email),
        ("type", "EMAIL"),
        ("match_type", "EXACT"),
        ("core_id_params.enable_private_names", "true"),
    ]
    for path in _FIELD_PATHS:
        params.append(("request_mask.include_field.paths", path))
    for container in _CONTAINERS:
        params.append(("request_mask.include_container", container))
    return params


def _real_display_name(display_name: str | None, email: str) -> str | None:
    """Return a usable human name, or None.

    Some locked-down Workspace tenants return the EMAIL ITSELF as the People
    API "best display name" when no real profile name is visible to an external
    querier. That's a placeholder, not a name — keeping it would
    produce a misleading `name_match=no` (read as "different person") when the
    truth is just "no name exposed." Treat display-name == email (or its
    localpart) as no name; callers then fall back to the photo artifact.
    """
    if not display_name or not display_name.strip():
        return None
    dn = display_name.strip()
    addr = email.strip().lower()
    local = addr.split("@", 1)[0]
    if dn.lower() in (addr, local):
        return None
    return dn


def _blank() -> dict[str, Any]:
    return {
        "exists": False, "profile_visible": False,
        "gaia_id": None, "display_name": None, "photo_url": None,
        "rate_limited": False, "parse_error": None,
    }


def _err(msg: str) -> dict[str, Any]:
    out = _blank()
    out["parse_error"] = msg
    return out


def _parse_lookup_response(payload_bytes: bytes) -> dict[str, Any]:
    """Parse the people-pa lookup response into our structured format.

    Returns a dict with keys:
      exists:        bool
      profile_visible: bool  (False == exists_unverifiable)
      gaia_id:       str | None
      display_name:  str | None
      photo_url:     str | None  (non-default avatar only; human-review artifact)
      rate_limited:  bool
      parse_error:   str | None

    Real response shape on success:
      {"people": {"<personId or e:email>": {<Person record>}}}

    Real response on not-found: empty object {} OR {"people": {}}.

    Real response on error: {"error": {"code": "...", "message": "...",
      "status": "..."}} — usually with the matching HTTP status, but
      some surfaces 200-respond with error in the body.
    """
    out = _blank()
    try:
        body = json.loads(payload_bytes.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError) as e:
        out["parse_error"] = f"json decode: {e}"
        return out

    if not isinstance(body, dict):
        out["parse_error"] = f"unexpected response type: {type(body).__name__}"
        return out

    err = body.get("error")
    if isinstance(err, dict):
        status_str = str(err.get("status") or err.get("code") or "").upper()
        if "RATE_LIMIT" in status_str or "RESOURCE_EXHAUSTED" in status_str:
            out["rate_limited"] = True
            return out
        msg = str(err.get("message", err))[:160]
        out["parse_error"] = f"api error: {msg}"
        return out

    people = body.get("people")
    if not isinstance(people, dict) or not people:
        return out  # exists=False

    # Lookup is one-email-per-request, so there's only ever one entry.
    first = next(iter(people.values()), None)
    if not isinstance(first, dict):
        return out

    out["exists"] = True

    pid = first.get("personId")
    if isinstance(pid, str) and pid:
        out["gaia_id"] = pid

    # Display name. Try metadata.bestDisplayName first (Google patched the
    # `name[]` field in 2022, but bestDisplayName still serves a usable
    # value on many profiles), then fall back to name[].
    metadata = first.get("metadata")
    if isinstance(metadata, dict):
        best = metadata.get("bestDisplayName")
        if isinstance(best, dict):
            v = best.get("displayName") or best.get("value")
            if isinstance(v, str) and v.strip():
                out["display_name"] = v.strip()

    if not out["display_name"]:
        names = first.get("name") or first.get("names")
        if isinstance(names, list):
            for entry in names:
                if not isinstance(entry, dict):
                    continue
                v = entry.get("displayName") or entry.get("value")
                if isinstance(v, str) and v.strip():
                    out["display_name"] = v.strip()
                    break

    # Profile photo — captured ONLY as a human-review artifact (a person can
    # compare it against, e.g., a LinkedIn photo). snoop never compares faces.
    # Drop default/placeholder avatars: a generic silhouette is useless for a
    # human cross-check and would only mislead. Response key has been seen as
    # both "photo" and "photos" across rotations; accept either.
    photos = first.get("photo") or first.get("photos")
    if isinstance(photos, list):
        for p in photos:
            if not isinstance(p, dict):
                continue
            url = p.get("url")
            if isinstance(url, str) and url.strip() and not p.get("isDefault"):
                out["photo_url"] = url.strip()
                break

    # profile_visible: gaia_id alone is enough to confirm a queryable
    # account. display_name is bonus signal for name-matching but not
    # required for the "verified" verdict.
    out["profile_visible"] = bool(out["gaia_id"] or out["display_name"])
    return out


# ---- HTTP transport ---------------------------------------------------------


def _default_http_get(
    url: str,
    params: list[tuple[str, str]],
    headers: dict[str, str],
    *,
    timeout: float = _DEFAULT_TIMEOUT_SEC,
) -> tuple[int, bytes]:
    """Real HTTP transport for production. Tests inject a fake."""
    encoded = urllib.parse.urlencode(params, doseq=True)
    full_url = f"{url}?{encoded}" if encoded else url
    req = urllib.request.Request(full_url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read() or b""
        except Exception:  # noqa: BLE001
            body = b""
        return e.code, body


# ---- public API -------------------------------------------------------------


def _lookup_one(
    email: str,
    cookies: dict[str, str],
    sapisid: str,
    http_get: HttpGet,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Do one People API lookup. Returns the parsed result dict."""
    auth = compute_sapisidhash(sapisid, now=now)
    headers = {
        "Authorization": auth,
        "Cookie": _cookie_header(cookies),
        "Origin": _ORIGIN,
        "Referer": _ORIGIN + "/",
        "User-Agent": "Mozilla/5.0 (snoop-skill)",
        "X-Goog-Api-Key": _API_KEY,
        "X-Goog-AuthUser": "0",
    }
    params = _build_lookup_params(email)
    try:
        status, payload = http_get(_LOOKUP_URL, params, headers)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return _err(f"http error: {type(e).__name__}: {e}")
    if status == 429:
        out = _blank()
        out["rate_limited"] = True
        return out
    if status in (401, 403):
        # Almost always: API key rotated, origin/key pairing wrong, or the
        # browser session lost its SAPISID. Surface as parse_error so the
        # candidate is marked unprobed rather than not_found.
        return _err(
            f"auth failed: HTTP {status} (API key may have rotated, or "
            "browser session is stale — re-sign-into Google in your browser)"
        )
    if status >= 500:
        return _err(f"server error: HTTP {status}")
    return _parse_lookup_response(payload)


def fetch_google_account(
    candidates: Iterable[EmailCandidate],
    *,
    cookie_loader: CookieLoader | None = None,
    http_get: HttpGet | None = None,
    target_domains: Iterable[str] | None = None,
    target_name: str | None = None,
    budget: Any | None = None,  # lib.verify_smtp.ProbeBudget; optional
    now: datetime | None = None,
) -> ResolverResult:
    """Probe each candidate against Google's People API.

    Args:
        candidates: EmailCandidates to probe. Mutated in place: account_exists
            field is set and a Source(type="google_account", ...) is appended
            when the lookup yields a real result.
        cookie_loader: Optional callable returning Google cookies. Default uses
            lib.chrome_cookies.get_google_cookies().
        http_get: Optional injected HTTP transport for tests. Signature is
            (url, params_list, headers) -> (status, body_bytes).
        target_domains: Restrict probing to candidates whose domain is in this
            set. Default: only literal "google.com". Pass a broader set for
            Workspace tenants whose MX is Google-hosted (the caller knows
            this via lib.normalize / MX lookup; v1 doesn't auto-detect).
        target_name: Target's name for verified-hit name-match check. When
            provided, the short-circuit only triggers on verified + display
            name match — a verified hit on a different person (common on
            multi-user Workspace tenants where pattern guesses can hit real
            accounts belonging to someone else with the same first name)
            does NOT short-circuit. When None, any verified hit short-circuits
            (legacy behavior; only safe on literal google.com one-target runs).
        budget: ProbeBudget for defense-in-depth rate limiting.
        now: For deterministic tests.

    Returns:
        ResolverResult with one candidate per probed entry (regardless of
        outcome). The mutation on the input list is the primary side effect;
        the returned list is for the cluster pass downstream.
    """
    started = datetime.now(timezone.utc) if now is None else now
    # Separate monotonic clock for elapsed_ms — if the caller pinned `started`
    # to a fixed datetime for determinism, subtracting wall-clock from it
    # would yield nonsense.
    started_monotonic = time.monotonic()

    if cookie_loader is None:
        from .chrome_cookies import get_google_cookies
        loader: CookieLoader = get_google_cookies
    else:
        loader = cookie_loader
    try:
        cookies = loader() or {}
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

    getter: HttpGet = http_get if http_get is not None else _default_http_get

    if target_domains is None:
        target_domain_set = {"google.com"}
    else:
        target_domain_set = {d.lower() for d in target_domains}

    candidates_list = list(candidates)
    rate_limited_seen = False
    verified_seen = False
    probed_any = False

    for c in candidates_list:
        if not c.address or "@" not in c.address:
            continue
        domain = c.address.rsplit("@", 1)[1].lower()
        if domain not in target_domain_set:
            continue

        # Short-circuit on first verified hit: snoop is one-person-per-run,
        # so a confirmed Gaia binding answers the question — additional
        # probes burn the daily budget and risk Google flagging the account.
        # Subsequent candidates are left as `unprobed` rather than `not_found`
        # so the scorer knows we abstained, not negated.
        if rate_limited_seen or verified_seen:
            c.account_exists = "unprobed"
            continue

        if budget is not None and not budget.allow("google_account"):
            c.account_exists = "unprobed"
            continue

        probed_any = True
        # Don't pin `now` for SAPISIDHASH: every candidate in the batch must
        # get its own fresh timestamp. Reusing started.timestamp() across N
        # lookups produces identical Authorization headers and risks a stale
        # timestamp signing future probes during long batches.
        result = _lookup_one(c.address, cookies, sapisid, getter)

        if result["rate_limited"]:
            rate_limited_seen = True
            c.account_exists = "unprobed"
            continue

        if result["parse_error"]:
            logger.info("google_account lookup error for %s: %s",
                        c.address, result["parse_error"])
            c.account_exists = "unprobed"
            continue

        # Only record successful exchanges against the budget. Auth/server
        # errors didn't produce a real lookup, so they shouldn't burn the
        # user's daily quota (otherwise a stale browser session can exhaust
        # the quota in one bad run).
        if budget is not None:
            budget.record("google_account")

        if not result["exists"]:
            c.account_exists = "not_found"
            c.sources.append(Source(
                type="google_account",
                url=None,
                observed_at=started,
                detail="Google account lookup: address not found",
            ))
            continue

        if result["profile_visible"]:
            c.account_exists = "verified"
            # Drop the email-as-display-name placeholder (see _real_display_name).
            real_name = _real_display_name(result["display_name"], c.address)
            if real_name:
                c.account_display_name = real_name
            if result.get("photo_url"):
                c.account_photo_url = result["photo_url"]
            # Short-circuit ONLY when we can confirm this is the target
            # (name match) OR the caller didn't pass a target name and so
            # opted into the legacy "any verified short-circuits" behavior.
            # On multi-user Workspace tenants a pattern guess can hit a real
            # account belonging to someone else; short-circuiting there would
            # leave the actual target's address unprobed.
            if target_name is None:
                verified_seen = True
            elif real_name and name_match(real_name, target_name):
                verified_seen = True
            detail_parts = ["Google account confirmed"]
            if result["gaia_id"]:
                detail_parts.append(f"Gaia {result['gaia_id'][:8]}…")
            if real_name:
                detail_parts.append(f"display name '{real_name}'")
            if result.get("photo_url"):
                detail_parts.append("profile photo available (human-review artifact)")
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

    elapsed_ms = int((time.monotonic() - started_monotonic) * 1000)

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
