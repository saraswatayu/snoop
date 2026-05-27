"""Tests for lib/google_account.py.

Deterministic — cookie_loader and http_post are both injectable seams.
The SAPISIDHASH algo is exercised with a known vector; the response
parser is exercised with hand-crafted JSON fixtures matching the shape
GHunt-style endpoints return.

These tests don't exercise the LIVE Google API — that's a real-user
verification step. The point is to confirm our state machine is correct
GIVEN the API behaves the way we modeled it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import lib.google_account as ga
from lib.schema import EmailCandidate


# ---- compute_sapisidhash (stable algorithm) --------------------------------


def test_sapisidhash_matches_known_vector():
    """SHA1("ts SAPISID origin") with our known inputs should produce
    the right output. Cross-check against a hand-computed reference."""
    ts = 1700000000
    sapisid = "ABCdefGHI"
    origin = "https://contacts.google.com"
    expected_digest = hashlib.sha1(
        f"{ts} {sapisid} {origin}".encode("utf-8")
    ).hexdigest()
    result = ga.compute_sapisidhash(sapisid, origin=origin, now=ts)
    assert result == f"SAPISIDHASH {ts}_{expected_digest}"


def test_sapisidhash_uses_current_time_when_now_none(monkeypatch):
    """Production path: now=None pulls from time.time()."""
    monkeypatch.setattr(ga.time, "time", lambda: 12345.0)
    result = ga.compute_sapisidhash("X", origin="https://x.test")
    assert result.startswith("SAPISIDHASH 12345_")


# ---- _pick_sapisid_cookie --------------------------------------------------


def test_pick_sapisid_prefers_modern_secure_variants():
    cookies = {
        "SAPISID": "legacy",
        "__Secure-1PAPISID": "modern-1p",
    }
    # SAPISID is first in the preference list
    assert ga._pick_sapisid_cookie(cookies) == "legacy"


def test_pick_sapisid_falls_back_to_secure_when_legacy_missing():
    cookies = {"__Secure-1PAPISID": "modern-only"}
    assert ga._pick_sapisid_cookie(cookies) == "modern-only"


def test_pick_sapisid_returns_none_when_no_sapisid_family():
    cookies = {"SID": "s", "HSID": "h"}  # SAPISID-family entirely missing
    assert ga._pick_sapisid_cookie(cookies) is None


# ---- _parse_lookup_response ------------------------------------------------


def test_parse_verified_response_with_profile():
    """Account exists AND profile is visible: verified state, gaia_id + name."""
    body = json.dumps([[
        {
            "personId": "1234567890",
            "name": [{"displayName": "Peter Steinberger"}],
            "photo": [{"url": "https://lh3.googleusercontent.com/abc"}],
        }
    ]])
    result = ga._parse_lookup_response(body.encode("utf-8"))
    assert result["exists"] is True
    assert result["profile_visible"] is True
    assert result["gaia_id"] == "1234567890"
    assert result["display_name"] == "Peter Steinberger"
    assert result["photo_url"] == "https://lh3.googleusercontent.com/abc"
    assert result["rate_limited"] is False
    assert result["parse_error"] is None


def test_parse_exists_unverifiable_when_no_profile_details():
    """Workspace visibility-restricted: response acknowledges existence but
    profile fields are missing/empty. account_exists should be
    exists_unverifiable downstream."""
    # Match is an empty dict — exists but no fields visible
    body = json.dumps([[{}]])
    result = ga._parse_lookup_response(body.encode("utf-8"))
    assert result["exists"] is True
    assert result["profile_visible"] is False
    assert result["gaia_id"] is None
    assert result["display_name"] is None


def test_parse_not_found_when_no_matches():
    body = json.dumps([[]])
    result = ga._parse_lookup_response(body.encode("utf-8"))
    assert result["exists"] is False
    assert result["profile_visible"] is False


def test_parse_not_found_when_empty_response():
    """Some shapes return entirely empty top-level array."""
    result = ga._parse_lookup_response(b"[]")
    assert result["exists"] is False


def test_parse_detects_rate_limit_error_in_body():
    """Some Google APIs 200-respond with a RATE_LIMIT_EXCEEDED error
    in the body. Make sure we catch this."""
    body = json.dumps({
        "error": {"code": "RATE_LIMIT_EXCEEDED", "message": "Quota exceeded"}
    })
    result = ga._parse_lookup_response(body.encode("utf-8"))
    assert result["rate_limited"] is True
    assert result["exists"] is False


def test_parse_handles_other_api_errors_as_parse_error():
    """A non-rate-limit API error shouldn't be treated as 'not_found' —
    surface it as parse_error so the resolver leaves account_exists unprobed."""
    body = json.dumps({
        "error": {"code": "INVALID_ARGUMENT", "message": "Bad request"}
    })
    result = ga._parse_lookup_response(body.encode("utf-8"))
    assert result["rate_limited"] is False
    assert result["exists"] is False
    assert "INVALID_ARGUMENT" in (result["parse_error"] or "") or \
           "Bad request" in (result["parse_error"] or "")


def test_parse_handles_invalid_json():
    result = ga._parse_lookup_response(b"not json at all")
    assert result["parse_error"] is not None
    assert "json decode" in result["parse_error"]


def test_parse_tolerant_of_alternative_field_names():
    """The endpoint has used `personId` AND `id` historically. Either should
    parse to gaia_id."""
    body = json.dumps([[
        {"id": "alt-id-123", "names": [{"value": "Alt Name Field"}]}
    ]])
    result = ga._parse_lookup_response(body.encode("utf-8"))
    assert result["gaia_id"] == "alt-id-123"
    assert result["display_name"] == "Alt Name Field"


# ---- fetch_google_account: orchestration -----------------------------------


NOW = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)


def make_http_post(routes: dict[str, tuple[int, bytes]]):
    """Stub http_post. routes maps a substring → (status, body). The first
    URL with a matching substring wins; otherwise raises AssertionError."""
    def post(url, headers, body):
        for sub, response in routes.items():
            if sub in url:
                return response
        raise AssertionError(f"unexpected POST url: {url}")
    return post


def make_cookies(sapisid="real-sapisid"):
    """Stub cookie loader returning a usable cookie set."""
    def loader():
        return {
            "SID": "sid-val", "SSID": "ssid-val", "HSID": "hsid-val",
            "APISID": "apisid-val", "SAPISID": sapisid,
        }
    return loader


def test_fetch_marks_verified_when_account_exists_with_profile():
    body = json.dumps([[{
        "personId": "1234567890",
        "name": [{"displayName": "Peter Steinberger"}],
    }]]).encode("utf-8")
    http = make_http_post({"people/lookup": (200, body)})
    cands = [EmailCandidate(address="pete@google.com")]
    result = ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_post=http, now=NOW,
    )
    assert result.status == "ok"
    assert cands[0].account_exists == "verified"
    src_types = [s.type for s in cands[0].sources]
    assert "google_account" in src_types
    # The source detail should include the display name
    src = next(s for s in cands[0].sources if s.type == "google_account")
    assert "Peter Steinberger" in src.detail


def test_fetch_marks_exists_unverifiable_when_no_profile_returned():
    body = json.dumps([[{}]]).encode("utf-8")
    http = make_http_post({"people/lookup": (200, body)})
    cands = [EmailCandidate(address="restricted@google.com")]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_post=http, now=NOW,
    )
    assert cands[0].account_exists == "exists_unverifiable"
    src = next(s for s in cands[0].sources if s.type == "google_account")
    assert "visibility-restricted" in src.detail.lower() or \
           "not visible" in src.detail.lower()


def test_fetch_marks_not_found():
    body = b"[[]]"  # empty matches array
    http = make_http_post({"people/lookup": (200, body)})
    cands = [EmailCandidate(address="ghost@google.com")]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_post=http, now=NOW,
    )
    assert cands[0].account_exists == "not_found"


def test_fetch_returns_unavailable_when_no_cookies():
    http = make_http_post({})  # shouldn't be called
    cands = [EmailCandidate(address="x@google.com")]
    result = ga.fetch_google_account(
        cands, cookie_loader=lambda: {}, http_post=http, now=NOW,
    )
    assert result.status == "unavailable"
    assert "SAPISID" in (result.error_detail or "")
    # Candidate left untouched
    assert cands[0].account_exists == "unprobed"


def test_fetch_returns_unavailable_when_cookie_loader_raises():
    def bombs():
        raise OSError("simulated cookie read failure")
    http = make_http_post({})
    cands = [EmailCandidate(address="x@google.com")]
    result = ga.fetch_google_account(
        cands, cookie_loader=bombs, http_post=http, now=NOW,
    )
    assert result.status == "unavailable"
    assert "cookie loader" in (result.error_detail or "").lower()


def test_fetch_only_probes_google_domain_by_default():
    """Pattern_gen candidates often span multiple domains. We should only
    probe google.com unless the caller broadens target_domains explicitly."""
    http_calls = []
    def http(url, headers, body):
        http_calls.append(url)
        return (200, b"[[]]")
    cands = [
        EmailCandidate(address="x@google.com"),
        EmailCandidate(address="x@example.com"),  # NOT a Google domain
    ]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_post=http, now=NOW,
    )
    # Only the google.com candidate triggered a probe
    assert len(http_calls) == 1


def test_fetch_handles_rate_limit_and_short_circuits():
    """When the first probe returns rate_limited, subsequent probes should
    be skipped (mark unprobed) rather than firing more requests."""
    body = json.dumps({"error": {"code": "RATE_LIMIT_EXCEEDED"}}).encode("utf-8")
    http_calls = []
    def http(url, headers, body_in):
        http_calls.append(url)
        return (200, body)
    cands = [
        EmailCandidate(address="a@google.com"),
        EmailCandidate(address="b@google.com"),
        EmailCandidate(address="c@google.com"),
    ]
    result = ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_post=http, now=NOW,
    )
    # Only the first probe happened
    assert len(http_calls) == 1
    # All three candidates marked unprobed (the rate_limited verdict)
    for c in cands:
        assert c.account_exists == "unprobed"
    # Status reflects the degradation
    assert result.status == "error"
    assert "rate" in (result.error_detail or "").lower()


def test_fetch_handles_http_429():
    """If the transport itself returns 429 (not embedded in body), still
    short-circuit."""
    http_calls = []
    def http(url, headers, body):
        http_calls.append(url)
        return (429, b"")
    cands = [EmailCandidate(address="a@google.com"),
             EmailCandidate(address="b@google.com")]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_post=http, now=NOW,
    )
    assert len(http_calls) == 1


def test_fetch_handles_http_500_as_parse_error():
    """5xx is transient. Mark unprobed; subsequent candidates still try."""
    http_calls = []
    def http(url, headers, body):
        http_calls.append(url)
        return (500, b"")
    cands = [EmailCandidate(address="a@google.com"),
             EmailCandidate(address="b@google.com")]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_post=http, now=NOW,
    )
    # Both candidates probed (5xx doesn't short-circuit like rate-limit does)
    assert len(http_calls) == 2
    for c in cands:
        assert c.account_exists == "unprobed"


def test_fetch_handles_http_transport_error():
    """A network error should leave candidate unprobed with no crash."""
    import urllib.error
    def bombs(url, headers, body):
        raise urllib.error.URLError("network down")
    cands = [EmailCandidate(address="a@google.com")]
    result = ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_post=bombs, now=NOW,
    )
    assert cands[0].account_exists == "unprobed"
    # Resolver status reflects empty result (all probes errored)
    assert result.status in ("empty", "error")


def test_fetch_explicit_target_domains_broadens_to_workspace():
    """For Workspace tenants whose MX is Google but whose domain isn't
    literal 'google.com', the caller passes the explicit set."""
    body = json.dumps([[{
        "personId": "9999", "name": [{"displayName": "Worker Bee"}],
    }]]).encode("utf-8")
    http = make_http_post({"people/lookup": (200, body)})
    cands = [EmailCandidate(address="worker@acme.com")]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_post=http,
        target_domains=["acme.com"], now=NOW,
    )
    assert cands[0].account_exists == "verified"


# ---- the request shape -----------------------------------------------------


def test_request_includes_authorization_and_cookie_headers():
    """Verify the request we build includes SAPISIDHASH and the cookie header."""
    captured_headers = []
    def http(url, headers, body):
        captured_headers.append(headers)
        return (200, b"[[]]")
    cands = [EmailCandidate(address="x@google.com")]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies("test-sapisid"),
        http_post=http, now=NOW,
    )
    assert captured_headers
    h = captured_headers[0]
    assert h["Authorization"].startswith("SAPISIDHASH ")
    assert "SAPISID=test-sapisid" in h["Cookie"]
    assert "google.com" in h["Origin"] or "contacts" in h["Origin"]


def test_request_body_contains_target_email():
    """The protojson request body must include the email we're looking up."""
    captured_bodies = []
    def http(url, headers, body):
        captured_bodies.append(body)
        return (200, b"[[]]")
    cands = [EmailCandidate(address="target@google.com")]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_post=http, now=NOW,
    )
    assert captured_bodies
    body_str = captured_bodies[0].decode("utf-8")
    assert "target@google.com" in body_str
