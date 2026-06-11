"""Tests for lib/pgp_keyserver.py.

Deterministic — a fake `fetch_fn` is injected so no network is required. The
fake routes by URL: a verified address gets a 200 + application/pgp-keys
FetchResult; a not-found address either gets a 404 FetchResult or raises
FetchBlocked (the keyserver returns 404 for a non-UID; lib.fetch surfaces that
as a FetchResult since 404 isn't a redirect or guard refusal).
"""

from __future__ import annotations

import functools
import urllib.parse

import pytest

from lib.fetch import FetchBlocked, FetchResult
from lib.pgp_keyserver import (
    _QUERY_BUDGET,
    by_email_url,
    fetch_pgp_emails,
)
from tests._http_harness import make_fetch as _make_fetch  # shared harness (ENG-7)

_BASE = "https://keys.openpgp.org/vks/v1/by-email/"

# The keyserver answers a non-UID with a 404 (not a guard refusal), so unmapped
# URLs return a 404 FetchResult rather than raising FetchBlocked.
make_fetch = functools.partial(_make_fetch, unmapped="notfound")


def _key_result(url: str) -> FetchResult:
    """A 200 armored-key response — the verified-UID signal."""
    return FetchResult(
        url=url,
        status=200,
        content_type="application/pgp-keys",
        text="-----BEGIN PGP PUBLIC KEY BLOCK-----\n...\n-----END PGP PUBLIC KEY BLOCK-----\n",
    )


# ---- by_email_url -----------------------------------------------------------


def test_by_email_url_basic():
    assert by_email_url("pete@openai.com") == _BASE + "pete%40openai.com"


def test_by_email_url_encodes_plus_address():
    """A '+' in the localpart must be percent-escaped, not left literal."""
    url = by_email_url("jane+tag@acme.com")
    assert "+" not in url.removeprefix(_BASE)
    assert url == _BASE + "jane%2Btag%40acme.com"


def test_by_email_url_encodes_unicode_address():
    """A unicode address is UTF-8 percent-encoded into one path segment."""
    email = "joão@example.com"
    url = by_email_url(email)
    # The segment after the base round-trips back to the original address.
    segment = url.removeprefix(_BASE)
    assert "ã" not in segment  # actually escaped, not raw unicode
    assert urllib.parse.unquote(segment) == email


# ---- fetch_pgp_emails: the verified-UID positive ----------------------------


def test_verified_email_yields_one_pgp_candidate():
    url = by_email_url("pete@openai.com")
    fetch_fn = make_fetch({url: _key_result(url)})
    result = fetch_pgp_emails(["pete@openai.com"], fetch_fn=fetch_fn)

    assert result.resolver == "pgp"
    assert result.status == "ok"
    assert [c.address for c in result.candidates] == ["pete@openai.com"]

    src = result.candidates[0].sources[0]
    assert src.type == "pgp"
    assert src.url == url
    assert src.detail == "verified UID on keys.openpgp.org"
    assert src.observed_at.tzinfo is not None  # UTC-aware


def test_calls_fetch_with_pgp_keys_content_type():
    """The fetch must whitelist application/pgp-keys for the armored response."""
    url = by_email_url("pete@openai.com")
    record: list = []
    fetch_fn = make_fetch({url: _key_result(url)}, record=record)
    fetch_pgp_emails(["pete@openai.com"], fetch_fn=fetch_fn)

    assert len(record) == 1
    called_url, allowed = record[0]
    assert called_url == url
    assert allowed is not None
    assert "application/pgp-keys" in allowed


# ---- fetch_pgp_emails: the not-found negative -------------------------------


def test_notfound_email_yields_no_candidate_via_404():
    """A 404 FetchResult (not a verified UID) → no candidate, status empty."""
    fetch_fn = make_fetch({})  # everything 404s
    result = fetch_pgp_emails(["ghost@nowhere.dev"], fetch_fn=fetch_fn)
    assert result.status == "empty"
    assert result.candidates == []


def test_notfound_email_yields_no_candidate_via_fetchblocked():
    """If the fetcher raises FetchBlocked for an address, skip it cleanly."""
    url = by_email_url("blocked@nowhere.dev")
    fetch_fn = make_fetch({url: FetchBlocked("disallowed content-type: (none)")})
    result = fetch_pgp_emails(["blocked@nowhere.dev"], fetch_fn=fetch_fn)
    assert result.status == "empty"
    assert result.candidates == []


def test_non_200_non_404_yields_no_candidate():
    """Any non-200 (e.g. a 500) is treated as 'not verified', not a crash."""
    url = by_email_url("flaky@nowhere.dev")
    server_error = FetchResult(url=url, status=500, content_type="text/plain", text="boom")
    fetch_fn = make_fetch({url: server_error})
    result = fetch_pgp_emails(["flaky@nowhere.dev"], fetch_fn=fetch_fn)
    assert result.status == "empty"
    assert result.candidates == []


# ---- fetch_pgp_emails: mixed batch ------------------------------------------


def test_mixed_batch_returns_only_verified():
    verified = by_email_url("pete@openai.com")
    also_verified = by_email_url("jane@acme.com")
    fetch_fn = make_fetch({
        verified: _key_result(verified),
        also_verified: _key_result(also_verified),
        # "ghost@nowhere.dev" not in routes → default 404
    })
    result = fetch_pgp_emails(
        ["pete@openai.com", "ghost@nowhere.dev", "jane@acme.com"],
        fetch_fn=fetch_fn,
    )
    assert result.status == "ok"
    addrs = {c.address for c in result.candidates}
    assert addrs == {"pete@openai.com", "jane@acme.com"}
    for c in result.candidates:
        assert c.sources[0].type == "pgp"


def test_one_fetchblocked_does_not_sink_the_batch():
    """A FetchBlocked on one address must not prevent verifying the others."""
    good = by_email_url("pete@openai.com")
    bad = by_email_url("blocked@nowhere.dev")
    fetch_fn = make_fetch({
        good: _key_result(good),
        bad: FetchBlocked("guard refused"),
    })
    result = fetch_pgp_emails(
        ["blocked@nowhere.dev", "pete@openai.com"],
        fetch_fn=fetch_fn,
    )
    assert result.status == "ok"
    assert [c.address for c in result.candidates] == ["pete@openai.com"]


def test_one_oserror_does_not_sink_the_batch():
    """A network/TLS error (OSError) on one address is skipped, batch survives."""
    good = by_email_url("pete@openai.com")
    bad = by_email_url("oops@nowhere.dev")
    fetch_fn = make_fetch({
        good: _key_result(good),
        bad: OSError("connection reset"),
    })
    result = fetch_pgp_emails(
        ["oops@nowhere.dev", "pete@openai.com"],
        fetch_fn=fetch_fn,
    )
    assert result.status == "ok"
    assert [c.address for c in result.candidates] == ["pete@openai.com"]


# ---- fetch_pgp_emails: dedupe + malformed -----------------------------------


def test_duplicate_emails_queried_once():
    url = by_email_url("pete@openai.com")
    record: list = []
    fetch_fn = make_fetch({url: _key_result(url)}, record=record)
    result = fetch_pgp_emails(
        ["pete@openai.com", "PETE@openai.com", "pete@openai.com"],
        fetch_fn=fetch_fn,
    )
    # Deduped + lowercased → exactly one query, one candidate.
    assert len(record) == 1
    assert [c.address for c in result.candidates] == ["pete@openai.com"]


def test_malformed_emails_skipped_without_query():
    record: list = []
    fetch_fn = make_fetch({}, record=record)
    result = fetch_pgp_emails(
        ["not-an-email", "@no-local.com", "foo@", "missing-tld@x"],
        fetch_fn=fetch_fn,
    )
    # None are syntactically valid → never queried, status empty.
    assert record == []
    assert result.status == "empty"
    assert result.candidates == []


def test_malformed_skipped_but_valid_still_queried():
    url = by_email_url("real@acme.com")
    record: list = []
    fetch_fn = make_fetch({url: _key_result(url)}, record=record)
    result = fetch_pgp_emails(
        ["not-an-email", "real@acme.com", "foo@"],
        fetch_fn=fetch_fn,
    )
    assert [u for u, _ in record] == [url]
    assert [c.address for c in result.candidates] == ["real@acme.com"]


# ---- fetch_pgp_emails: empty + budget ---------------------------------------


def test_empty_input_is_empty_status():
    result = fetch_pgp_emails([], fetch_fn=make_fetch({}))
    assert result.status == "empty"
    assert result.candidates == []


def test_query_budget_caps_network_calls():
    """More than the budget of distinct valid emails → only the first N queried."""
    emails = [f"user{i}@acme.com" for i in range(_QUERY_BUDGET + 5)]
    record: list = []
    fetch_fn = make_fetch({}, record=record)  # all 404
    result = fetch_pgp_emails(emails, fetch_fn=fetch_fn)
    assert len(record) == _QUERY_BUDGET
    assert result.status == "empty"


def test_budget_truncation_reports_in_error_detail_when_none_verified():
    emails = [f"user{i}@acme.com" for i in range(_QUERY_BUDGET + 3)]
    fetch_fn = make_fetch({})  # all 404
    result = fetch_pgp_emails(emails, fetch_fn=fetch_fn)
    assert result.status == "empty"
    assert str(_QUERY_BUDGET) in (result.error_detail or "")


def test_detail_is_single_line():
    """Source.detail must be one line (render._oneline marker-forgery defense)."""
    url = by_email_url("pete@openai.com")
    fetch_fn = make_fetch({url: _key_result(url)})
    result = fetch_pgp_emails(["pete@openai.com"], fetch_fn=fetch_fn)
    detail = result.candidates[0].sources[0].detail
    assert "\n" not in detail
    assert "\r" not in detail
