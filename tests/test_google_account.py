"""Tests for lib/google_account.py.

Deterministic — cookie_loader and http_get are both injectable seams.
The SAPISIDHASH algo is exercised with a known vector; the response
parser is exercised with hand-crafted JSON fixtures matching the shape
GHunt's people-pa endpoint returns.

These tests don't exercise the LIVE Google API — that's a real-user
verification step. The point is to confirm our state machine is correct
GIVEN the API behaves the way we modeled it (mirrored from GHunt's
apis/peoplepa.py and parsers/people.py).
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from datetime import datetime, timezone

import lib.google_account as ga
from lib.schema import EmailCandidate


# ---- compute_sapisidhash (stable algorithm) --------------------------------


def test_sapisidhash_matches_known_vector():
    """SHA1("ts SAPISID origin") with our known inputs should produce
    the right output."""
    ts = 1700000000
    sapisid = "ABCdefGHI"
    origin = "https://photos.google.com"
    expected_digest = hashlib.sha1(
        f"{ts} {sapisid} {origin}".encode("utf-8")
    ).hexdigest()
    result = ga.compute_sapisidhash(sapisid, origin=origin, now=ts)
    assert result == f"SAPISIDHASH {ts}_{expected_digest}"


def test_sapisidhash_default_origin_matches_photos():
    """Default origin must match the photos API key — server validates
    SAPISIDHASH against the origin registered to the API key. Pairing
    the photos key with contacts.google.com origin (our v1 bug) silently
    401s every request."""
    result = ga.compute_sapisidhash("X", now=12345.0)
    expected = hashlib.sha1(
        b"12345 X https://photos.google.com"
    ).hexdigest()
    assert result == f"SAPISIDHASH 12345_{expected}"


def test_sapisidhash_uses_current_time_when_now_none(monkeypatch):
    monkeypatch.setattr(ga.time, "time", lambda: 12345.0)
    result = ga.compute_sapisidhash("X", origin="https://x.test")
    assert result.startswith("SAPISIDHASH 12345_")


# ---- _pick_sapisid_cookie --------------------------------------------------


def test_pick_sapisid_prefers_legacy_when_present():
    """GHunt's gen_sapisidhash uses legacy SAPISID exclusively, so we
    match that preference order to keep behavior consistent."""
    cookies = {
        "SAPISID": "legacy",
        "__Secure-1PAPISID": "modern-1p",
    }
    assert ga._pick_sapisid_cookie(cookies) == "legacy"


def test_pick_sapisid_falls_back_to_secure_when_legacy_missing():
    cookies = {"__Secure-1PAPISID": "modern-only"}
    assert ga._pick_sapisid_cookie(cookies) == "modern-only"


def test_pick_sapisid_returns_none_when_no_sapisid_family():
    cookies = {"SID": "s", "HSID": "h"}
    assert ga._pick_sapisid_cookie(cookies) is None


# ---- _build_lookup_params --------------------------------------------------


def test_build_params_includes_required_keys():
    params = ga._build_lookup_params("foo@google.com")
    keys = {k for k, _ in params}
    assert "id" in keys
    assert "type" in keys
    assert "match_type" in keys
    # Multi-valued
    assert "request_mask.include_field.paths" in keys
    assert "request_mask.include_container" in keys


def test_build_params_uses_exact_email_match():
    params = dict(ga._build_lookup_params("foo@google.com"))
    assert params["id"] == "foo@google.com"
    assert params["type"] == "EMAIL"
    assert params["match_type"] == "EXACT"


def test_build_params_repeats_multivalued_keys():
    """request_mask.include_container must repeat for each value, not
    comma-join — that's how Google's protojson-over-query expects it."""
    params = ga._build_lookup_params("foo@google.com")
    containers = [v for k, v in params if k == "request_mask.include_container"]
    assert "PROFILE" in containers
    # urlencode(doseq=True) round-trip survives the list
    encoded = urllib.parse.urlencode(params, doseq=True)
    assert encoded.count("request_mask.include_container=") == len(containers)


# ---- _parse_lookup_response ------------------------------------------------


def _people_response(person: dict) -> bytes:
    """Wrap a single person record in the {"people": {<id>: <person>}}
    envelope that people-pa returns."""
    pid = person.get("personId") or "anonymous"
    return json.dumps({"people": {pid: person}}).encode("utf-8")


def test_parse_verified_response_with_full_profile():
    """Account exists with personId AND a display name from
    metadata.bestDisplayName — verified state with name available."""
    body = _people_response({
        "personId": "1234567890",
        "metadata": {
            "bestDisplayName": {"displayName": "Peter Steinberger"},
        },
    })
    result = ga._parse_lookup_response(body)
    assert result["exists"] is True
    assert result["profile_visible"] is True
    assert result["gaia_id"] == "1234567890"
    assert result["display_name"] == "Peter Steinberger"
    assert result["rate_limited"] is False
    assert result["parse_error"] is None


def test_parse_verified_with_just_person_id():
    """Post-2022, Google patched the name[] field — many responses come
    back with personId only. Still verified (gaia_id is the strongest
    existence signal), display_name just isn't populated."""
    body = _people_response({"personId": "9999"})
    result = ga._parse_lookup_response(body)
    assert result["exists"] is True
    assert result["profile_visible"] is True
    assert result["gaia_id"] == "9999"
    assert result["display_name"] is None


def test_parse_falls_back_to_name_array_when_best_display_name_absent():
    body = _people_response({
        "personId": "1",
        "name": [{"displayName": "Fallback Name"}],
    })
    result = ga._parse_lookup_response(body)
    assert result["display_name"] == "Fallback Name"


def test_parse_extracts_non_default_profile_photo():
    """A real (non-default) avatar is captured as a human-review artifact."""
    body = _people_response({
        "personId": "1",
        "photo": [{"url": "https://lh3.googleusercontent.com/a/real-pic=s96", "isDefault": False}],
    })
    result = ga._parse_lookup_response(body)
    assert result["photo_url"] == "https://lh3.googleusercontent.com/a/real-pic=s96"


def test_parse_skips_default_silhouette_avatar():
    """A default/placeholder avatar tells a human nothing — drop it so we
    never surface a misleading 'photo' for a generic silhouette."""
    body = _people_response({
        "personId": "1",
        "photo": [{"url": "https://lh3.googleusercontent.com/default-user=s96", "isDefault": True}],
    })
    result = ga._parse_lookup_response(body)
    assert result["photo_url"] is None


def test_parse_accepts_photos_plural_key():
    """The response key has been seen as both 'photo' and 'photos'."""
    body = _people_response({
        "personId": "1",
        "photos": [{"url": "https://lh3.googleusercontent.com/a/plural=s96"}],
    })
    result = ga._parse_lookup_response(body)
    assert result["photo_url"] == "https://lh3.googleusercontent.com/a/plural=s96"


def test_parse_photo_url_none_when_absent():
    result = ga._parse_lookup_response(_people_response({"personId": "1"}))
    assert result["photo_url"] is None


def test_parse_exists_unverifiable_when_no_identifying_fields():
    """An empty person record (no personId, no name) means Google
    acknowledged the lookup but didn't return identifying info.
    Workspace visibility-restricted profiles look like this."""
    body = json.dumps({"people": {"anonymous": {}}}).encode("utf-8")
    result = ga._parse_lookup_response(body)
    assert result["exists"] is True
    assert result["profile_visible"] is False
    assert result["gaia_id"] is None


def test_parse_not_found_when_people_dict_empty():
    body = b'{"people": {}}'
    result = ga._parse_lookup_response(body)
    assert result["exists"] is False
    assert result["profile_visible"] is False


def test_parse_not_found_when_people_key_missing():
    body = b"{}"
    result = ga._parse_lookup_response(body)
    assert result["exists"] is False


def test_parse_detects_rate_limit_in_error_object():
    body = json.dumps({
        "error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                  "message": "Quota exceeded"}
    }).encode("utf-8")
    result = ga._parse_lookup_response(body)
    assert result["rate_limited"] is True
    assert result["exists"] is False


def test_parse_detects_legacy_rate_limit_string():
    body = json.dumps({
        "error": {"code": "RATE_LIMIT_EXCEEDED", "message": "slow down"}
    }).encode("utf-8")
    result = ga._parse_lookup_response(body)
    assert result["rate_limited"] is True


def test_parse_handles_other_api_errors_as_parse_error():
    """A non-rate-limit API error shouldn't be treated as 'not_found' —
    surface it as parse_error so the resolver leaves account_exists unprobed."""
    body = json.dumps({
        "error": {"code": 400, "status": "INVALID_ARGUMENT",
                  "message": "Bad request"}
    }).encode("utf-8")
    result = ga._parse_lookup_response(body)
    assert result["rate_limited"] is False
    assert result["exists"] is False
    assert "INVALID_ARGUMENT" in (result["parse_error"] or "") or \
           "Bad request" in (result["parse_error"] or "")


def test_parse_handles_invalid_json():
    result = ga._parse_lookup_response(b"not json at all")
    assert result["parse_error"] is not None
    assert "json decode" in result["parse_error"]


def test_parse_handles_non_object_top_level():
    """If Google returns a bare array (old shape, or breakage), don't crash."""
    result = ga._parse_lookup_response(b"[1, 2, 3]")
    assert result["exists"] is False
    assert result["parse_error"] is not None


# ---- fetch_google_account: orchestration -----------------------------------


NOW = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)


def make_http_get(routes: dict[str, tuple[int, bytes]]):
    """Stub http_get. routes maps a substring → (status, body). The first
    URL with a matching substring wins; otherwise raises AssertionError."""
    def get(url, params, headers):
        for sub, response in routes.items():
            if sub in url:
                return response
        raise AssertionError(f"unexpected GET url: {url}")
    return get


def make_cookies(sapisid="real-sapisid"):
    def loader():
        return {
            "SID": "sid-val", "SSID": "ssid-val", "HSID": "hsid-val",
            "APISID": "apisid-val", "SAPISID": sapisid,
        }
    return loader


def test_fetch_marks_verified_when_account_exists_with_profile():
    body = _people_response({
        "personId": "1234567890",
        "metadata": {"bestDisplayName": {"displayName": "Peter Steinberger"}},
    })
    http = make_http_get({"people/lookup": (200, body)})
    cands = [EmailCandidate(address="pete@google.com")]
    result = ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_get=http, now=NOW,
    )
    assert result.status == "ok"
    assert cands[0].account_exists == "verified"
    assert cands[0].account_display_name == "Peter Steinberger"
    src = next(s for s in cands[0].sources if s.type == "google_account")
    assert "Peter Steinberger" in src.detail


def test_real_display_name_keeps_a_real_name():
    assert ga._real_display_name("Jordan Vega", "jvega@globex.com") == "Jordan Vega"


def test_real_display_name_drops_email_placeholder():
    """Locked-down Workspace tenants echo the email as the display name."""
    assert ga._real_display_name("jvega@globex.com", "jvega@globex.com") is None
    assert ga._real_display_name("JVega@Globex.com", "jvega@globex.com") is None


def test_real_display_name_drops_localpart_placeholder():
    assert ga._real_display_name("jvega", "jvega@globex.com") is None


def test_real_display_name_none_when_empty():
    assert ga._real_display_name(None, "x@globex.com") is None
    assert ga._real_display_name("   ", "x@globex.com") is None


def test_fetch_treats_email_display_name_as_no_name_but_keeps_photo():
    """The misleading-name_match guard: an email-as-display-name must NOT
    populate account_display_name (else the bundle shows name_match=no for a
    placeholder). The photo still surfaces as the real disambiguator."""
    body = _people_response({
        "personId": "1",
        "metadata": {"bestDisplayName": {"displayName": "jvega@globex.com"}},
        "photo": [{"url": "https://lh3.googleusercontent.com/a/avatar=s96", "isDefault": False}],
    })
    http = make_http_get({"people/lookup": (200, body)})
    cands = [EmailCandidate(address="jvega@globex.com")]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_get=http, now=NOW,
        target_domains=["globex.com"], target_name="Jordan Vega",
    )
    assert cands[0].account_exists == "verified"
    assert cands[0].account_display_name is None
    assert cands[0].account_photo_url == "https://lh3.googleusercontent.com/a/avatar=s96"
    src = next(s for s in cands[0].sources if s.type == "google_account")
    assert "display name" not in src.detail


def test_fetch_sets_photo_url_and_notes_artifact_in_source():
    body = _people_response({
        "personId": "1234567890",
        "metadata": {"bestDisplayName": {"displayName": "Peter Steinberger"}},
        "photo": [{"url": "https://lh3.googleusercontent.com/a/pete=s96", "isDefault": False}],
    })
    http = make_http_get({"people/lookup": (200, body)})
    cands = [EmailCandidate(address="pete@google.com")]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_get=http, now=NOW,
    )
    assert cands[0].account_photo_url == "https://lh3.googleusercontent.com/a/pete=s96"
    src = next(s for s in cands[0].sources if s.type == "google_account")
    assert "human-review artifact" in src.detail


def test_fetch_leaves_photo_url_none_for_default_avatar():
    body = _people_response({
        "personId": "1234567890",
        "photo": [{"url": "https://lh3.googleusercontent.com/default=s96", "isDefault": True}],
    })
    http = make_http_get({"people/lookup": (200, body)})
    cands = [EmailCandidate(address="pete@google.com")]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_get=http, now=NOW,
    )
    assert cands[0].account_exists == "verified"
    assert cands[0].account_photo_url is None


def test_fetch_marks_verified_when_only_person_id_returned():
    """Post-2022 patched-name case — still a strong existence verdict."""
    body = _people_response({"personId": "9999"})
    http = make_http_get({"people/lookup": (200, body)})
    cands = [EmailCandidate(address="pete@google.com")]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_get=http, now=NOW,
    )
    assert cands[0].account_exists == "verified"
    assert cands[0].account_display_name is None


def test_fetch_marks_exists_unverifiable_when_no_identifying_fields():
    """Person record present but stripped of personId/name —
    visibility-restricted Workspace target."""
    body = json.dumps({"people": {"anonymous": {}}}).encode("utf-8")
    http = make_http_get({"people/lookup": (200, body)})
    cands = [EmailCandidate(address="restricted@google.com")]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_get=http, now=NOW,
    )
    assert cands[0].account_exists == "exists_unverifiable"
    src = next(s for s in cands[0].sources if s.type == "google_account")
    assert "visibility-restricted" in src.detail.lower() or \
           "not visible" in src.detail.lower()


def test_fetch_marks_not_found():
    http = make_http_get({"people/lookup": (200, b'{"people": {}}')})
    cands = [EmailCandidate(address="ghost@google.com")]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_get=http, now=NOW,
    )
    assert cands[0].account_exists == "not_found"


def test_fetch_returns_unavailable_when_no_cookies():
    http = make_http_get({})  # shouldn't be called
    cands = [EmailCandidate(address="x@google.com")]
    result = ga.fetch_google_account(
        cands, cookie_loader=lambda: {}, http_get=http, now=NOW,
    )
    assert result.status == "unavailable"
    assert "SAPISID" in (result.error_detail or "")
    assert cands[0].account_exists == "unprobed"


def test_fetch_returns_unavailable_when_cookie_loader_raises():
    def bombs():
        raise OSError("simulated cookie read failure")
    http = make_http_get({})
    cands = [EmailCandidate(address="x@google.com")]
    result = ga.fetch_google_account(
        cands, cookie_loader=bombs, http_get=http, now=NOW,
    )
    assert result.status == "unavailable"
    assert "cookie loader" in (result.error_detail or "").lower()


def test_fetch_only_probes_google_domain_by_default():
    http_calls = []
    def http(url, params, headers):
        http_calls.append(url)
        return (200, b'{"people": {}}')
    cands = [
        EmailCandidate(address="x@google.com"),
        EmailCandidate(address="x@example.com"),
    ]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_get=http, now=NOW,
    )
    assert len(http_calls) == 1


def test_fetch_short_circuits_on_verified_name_match():
    """One-person-per-run: a verified hit WITH a matching display name
    answers the question. Don't burn the daily probe budget on additional
    candidates — mark them unprobed rather than continuing to call Google."""
    verified_body = _people_response({
        "personId": "424242",
        "metadata": {"bestDisplayName": {"displayName": "Real Person"}},
    })
    not_found_body = b'{"people": {}}'
    http_calls = []

    def http(url, params, headers):
        http_calls.append(dict(params).get("id"))
        if dict(params).get("id") == "first@google.com":
            return (200, verified_body)
        return (200, not_found_body)

    cands = [
        EmailCandidate(address="first@google.com"),
        EmailCandidate(address="second@google.com"),
        EmailCandidate(address="third@google.com"),
    ]
    result = ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_get=http,
        target_name="Real Person", now=NOW,
    )
    assert result.status == "ok"
    assert http_calls == ["first@google.com"]
    assert cands[0].account_exists == "verified"
    assert cands[0].account_display_name == "Real Person"
    # Subsequent candidates abstained, not negated — scorer needs to know
    # we didn't actually confirm they're absent.
    assert cands[1].account_exists == "unprobed"
    assert cands[2].account_exists == "unprobed"


def test_fetch_short_circuits_when_no_target_name(monkeypatch):
    """Legacy behavior: when caller didn't pass target_name, any verified
    short-circuits. Preserved for callers (tests, future use) that don't
    have a target name to check against."""
    verified_body = _people_response({
        "personId": "1", "metadata": {"bestDisplayName": {"displayName": "X"}},
    })
    http_calls = []
    def http(url, params, headers):
        http_calls.append(dict(params).get("id"))
        return (200, verified_body)

    cands = [
        EmailCandidate(address="a@google.com"),
        EmailCandidate(address="b@google.com"),
    ]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_get=http, now=NOW,
    )
    assert http_calls == ["a@google.com"]
    assert cands[0].account_exists == "verified"
    assert cands[1].account_exists == "unprobed"


def test_fetch_does_not_short_circuit_on_verified_wrong_name():
    """The reliability fix: a verified hit on a different person (common
    on multi-user Workspace tenants where pattern guesses can hit real
    accounts belonging to someone else with the same first name) does NOT
    short-circuit. Continue probing until either name match or all done."""
    wrong_person_body = _people_response({
        "personId": "1",
        "metadata": {"bestDisplayName": {"displayName": "Other Pete"}},
    })
    right_person_body = _people_response({
        "personId": "2",
        "metadata": {"bestDisplayName": {"displayName": "Peter Steinberger"}},
    })
    not_found_body = b'{"people": {}}'
    http_calls = []

    def http(url, params, headers):
        addr = dict(params).get("id")
        http_calls.append(addr)
        if addr == "pete@bigco.com":
            return (200, wrong_person_body)
        if addr == "peter.s@bigco.com":
            return (200, right_person_body)
        return (200, not_found_body)

    cands = [
        EmailCandidate(address="pete@bigco.com"),
        EmailCandidate(address="peter.s@bigco.com"),
        EmailCandidate(address="ps@bigco.com"),
    ]
    result = ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_get=http,
        target_domains={"bigco.com"},
        target_name="Peter Steinberger", now=NOW,
    )
    assert result.status == "ok"
    # First two probed; third is short-circuited because second matched.
    assert http_calls == ["pete@bigco.com", "peter.s@bigco.com"]
    assert cands[0].account_exists == "verified"
    assert cands[0].account_display_name == "Other Pete"
    assert cands[1].account_exists == "verified"
    assert cands[1].account_display_name == "Peter Steinberger"
    assert cands[2].account_exists == "unprobed"


def test_fetch_handles_rate_limit_and_short_circuits():
    body = json.dumps({
        "error": {"status": "RESOURCE_EXHAUSTED"}
    }).encode("utf-8")
    http_calls = []
    def http(url, params, headers):
        http_calls.append(url)
        return (200, body)
    cands = [
        EmailCandidate(address="a@google.com"),
        EmailCandidate(address="b@google.com"),
        EmailCandidate(address="c@google.com"),
    ]
    result = ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_get=http, now=NOW,
    )
    assert len(http_calls) == 1
    for c in cands:
        assert c.account_exists == "unprobed"
    assert result.status == "error"
    assert "rate" in (result.error_detail or "").lower()


def test_fetch_handles_http_429():
    http_calls = []
    def http(url, params, headers):
        http_calls.append(url)
        return (429, b"")
    cands = [EmailCandidate(address="a@google.com"),
             EmailCandidate(address="b@google.com")]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_get=http, now=NOW,
    )
    assert len(http_calls) == 1


def test_fetch_handles_http_401_as_auth_failure():
    """401/403 means SAPISIDHASH validation failed server-side (key/origin
    mismatch or stale browser session). Surface as unprobed with a
    descriptive parse_error in the logs."""
    http_calls = []
    def http(url, params, headers):
        http_calls.append(url)
        return (401, b"")
    cands = [EmailCandidate(address="a@google.com"),
             EmailCandidate(address="b@google.com")]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_get=http, now=NOW,
    )
    # Both probed (auth failure isn't transient — but we don't short-circuit
    # because the second might succeed if cookies were partially racy).
    assert len(http_calls) == 2
    for c in cands:
        assert c.account_exists == "unprobed"


def test_fetch_handles_http_500_as_parse_error():
    http_calls = []
    def http(url, params, headers):
        http_calls.append(url)
        return (500, b"")
    cands = [EmailCandidate(address="a@google.com"),
             EmailCandidate(address="b@google.com")]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_get=http, now=NOW,
    )
    assert len(http_calls) == 2
    for c in cands:
        assert c.account_exists == "unprobed"


def test_fetch_handles_http_transport_error():
    import urllib.error
    def bombs(url, params, headers):
        raise urllib.error.URLError("network down")
    cands = [EmailCandidate(address="a@google.com")]
    result = ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_get=bombs, now=NOW,
    )
    assert cands[0].account_exists == "unprobed"
    assert result.status in ("empty", "error")


def test_fetch_explicit_target_domains_broadens_to_workspace():
    body = _people_response({
        "personId": "9999",
        "metadata": {"bestDisplayName": {"displayName": "Worker Bee"}},
    })
    http = make_http_get({"people/lookup": (200, body)})
    cands = [EmailCandidate(address="worker@acme.com")]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_get=http,
        target_domains=["acme.com"], now=NOW,
    )
    assert cands[0].account_exists == "verified"


# ---- the request shape -----------------------------------------------------


def test_request_includes_auth_headers_with_photos_origin():
    """Verify the request we build includes SAPISIDHASH, X-Goog-Api-Key, and
    photos.google.com as Origin/Referer. These are the three things that
    have to be right together or the server 401s."""
    captured_headers = []
    def http(url, params, headers):
        captured_headers.append(headers)
        return (200, b'{"people": {}}')
    cands = [EmailCandidate(address="x@google.com")]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies("test-sapisid"),
        http_get=http, now=NOW,
    )
    assert captured_headers
    h = captured_headers[0]
    assert h["Authorization"].startswith("SAPISIDHASH ")
    assert "SAPISID=test-sapisid" in h["Cookie"]
    assert h["Origin"] == "https://photos.google.com"
    assert h["Referer"].startswith("https://photos.google.com")
    assert h["X-Goog-Api-Key"].startswith("AIza")


def test_request_url_and_params_contain_target_email():
    """Email goes in the `id` query param, not a POST body."""
    captured = []
    def http(url, params, headers):
        captured.append((url, params))
        return (200, b'{"people": {}}')
    cands = [EmailCandidate(address="target@google.com")]
    ga.fetch_google_account(
        cands, cookie_loader=make_cookies(), http_get=http, now=NOW,
    )
    assert captured
    url, params = captured[0]
    assert "people-pa.clients6.google.com" in url
    assert "/v2/people/lookup" in url
    params_dict = {k: v for k, v in params}
    assert params_dict.get("id") == "target@google.com"
    assert params_dict.get("type") == "EMAIL"
