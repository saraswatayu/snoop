"""Tests for lib/hn_profile.py.

Deterministic — http_get is injected so no network is required. The canned
HTML mirrors the shape of a real news.ycombinator.com/user?id=<handle> page:
a table with `user:`, `created:`, `karma:`, `about:`, and (when public) an
`email:` row.
"""

from __future__ import annotations

from lib.hn_profile import (
    _extract_emails_from_text,
    _is_extractable,
    fetch_hn_profile,
)


PROFILE_URL = "https://news.ycombinator.com/user?id=pg"


def make_http(routes):
    """A fake http_get returning the body for matching URLs, None for 404."""
    def get(url):
        for key, resp in routes.items():
            if url.startswith(key):
                if isinstance(resp, Exception):
                    raise resp
                return resp  # may be None to simulate 404
        return None
    return get


def _profile_html(*, email_row: str = "", about: str = "") -> str:
    """Build an HN-profile-shaped page. `email_row` is the raw HTML for the
    email row's value cell; `about` is the raw HTML for the about cell."""
    return f"""<html><body><table>
    <tr><td valign="top">user:</td><td>pg</td></tr>
    <tr><td valign="top">created:</td><td>September 30, 2006</td></tr>
    <tr><td valign="top">karma:</td><td>155000</td></tr>
    <tr><td valign="top">about:</td><td>{about}</td></tr>
    <tr><td valign="top">email:</td><td>{email_row}</td></tr>
    </table></body></html>"""


# ---- _is_extractable --------------------------------------------------------


def test_is_extractable_accepts_real_addresses():
    assert _is_extractable("pg@ycombinator.com")
    assert _is_extractable("hello@example.dev")


def test_is_extractable_rejects_placeholders():
    assert not _is_extractable("test@example.com")
    assert not _is_extractable("a@localhost")
    assert not _is_extractable("b@test.invalid")


def test_is_extractable_rejects_noreply():
    assert not _is_extractable("noreply@ycombinator.com")
    assert not _is_extractable("do-not-reply@anywhere.com")


def test_is_extractable_rejects_malformed():
    assert not _is_extractable("not-an-email")
    assert not _is_extractable("@example.com")
    assert not _is_extractable("foo@")


# ---- _extract_emails_from_text ----------------------------------------------


def test_extract_finds_plain_text_email():
    assert _extract_emails_from_text("reach me at pg@ycombinator.com") == [
        "pg@ycombinator.com"
    ]


def test_extract_finds_mailto_anchor():
    text = '<a href="mailto:pg@ycombinator.com">email</a>'
    assert _extract_emails_from_text(text) == ["pg@ycombinator.com"]


def test_extract_dedups_and_lowercases():
    text = "PG@Ycombinator.com and again pg@ycombinator.com"
    assert _extract_emails_from_text(text) == ["pg@ycombinator.com"]


def test_extract_decodes_html_entities():
    """HN sometimes encodes @ and . as entities to thwart naive scrapers."""
    text = "pg&#x40;ycombinator&#x2e;com"
    assert _extract_emails_from_text(text) == ["pg@ycombinator.com"]


def test_extract_drops_noise_addresses():
    text = "noreply@example.com but really pg@ycombinator.com"
    assert _extract_emails_from_text(text) == ["pg@ycombinator.com"]


def test_extract_handles_empty():
    assert _extract_emails_from_text("") == []
    assert _extract_emails_from_text("no email here at all") == []


# ---- fetch_hn_profile: email row --------------------------------------------


def test_fetch_picks_up_public_email_row():
    html = _profile_html(email_row="pg@ycombinator.com")
    http = make_http({PROFILE_URL: html})
    result = fetch_hn_profile("pg", http_get=http)
    assert result.status == "ok"
    assert [c.address for c in result.candidates] == ["pg@ycombinator.com"]
    src = result.candidates[0].sources[0]
    assert src.type == "hn_profile"
    assert src.url == PROFILE_URL
    assert src.detail == "public email on Hacker News profile"


def test_fetch_picks_up_email_in_about_box():
    html = _profile_html(about="Founder. Contact: jane@acme.io for inquiries.")
    http = make_http({PROFILE_URL: html})
    result = fetch_hn_profile("pg", http_get=http)
    assert result.status == "ok"
    assert [c.address for c in result.candidates] == ["jane@acme.io"]
    assert result.candidates[0].sources[0].type == "hn_profile"


def test_fetch_picks_up_mailto_in_about_box():
    html = _profile_html(
        about='Reach me: <a href="mailto:jane@acme.io">here</a>.'
    )
    http = make_http({PROFILE_URL: html})
    result = fetch_hn_profile("pg", http_get=http)
    assert [c.address for c in result.candidates] == ["jane@acme.io"]


def test_fetch_decodes_entity_encoded_email():
    html = _profile_html(email_row="pg&#x40;ycombinator&#x2e;com")
    http = make_http({PROFILE_URL: html})
    result = fetch_hn_profile("pg", http_get=http)
    assert result.status == "ok"
    assert [c.address for c in result.candidates] == ["pg@ycombinator.com"]


def test_fetch_dedups_email_in_both_row_and_about():
    """Same address in the email row and the about box → one candidate."""
    html = _profile_html(
        email_row="pg@ycombinator.com",
        about="Or email pg@ycombinator.com directly.",
    )
    http = make_http({PROFILE_URL: html})
    result = fetch_hn_profile("pg", http_get=http)
    assert len(result.candidates) == 1
    assert result.candidates[0].address == "pg@ycombinator.com"


def test_fetch_returns_multiple_distinct_addresses():
    html = _profile_html(
        email_row="work@acme.io",
        about="personal: home@example.dev",
    )
    http = make_http({PROFILE_URL: html})
    result = fetch_hn_profile("pg", http_get=http)
    addrs = {c.address for c in result.candidates}
    assert addrs == {"work@acme.io", "home@example.dev"}


# ---- fetch_hn_profile: empty / noise ----------------------------------------


def test_fetch_returns_empty_when_no_email():
    html = _profile_html(about="No contact info shared here.")
    http = make_http({PROFILE_URL: html})
    result = fetch_hn_profile("pg", http_get=http)
    assert result.status == "empty"
    assert result.candidates == []


def test_fetch_filters_noise_addresses():
    html = _profile_html(email_row="noreply@example.com")
    http = make_http({PROFILE_URL: html})
    result = fetch_hn_profile("pg", http_get=http)
    assert result.status == "empty"
    assert result.candidates == []


# ---- fetch_hn_profile: failure modes ----------------------------------------


def test_fetch_returns_unavailable_for_blank_handle():
    result = fetch_hn_profile("", http_get=make_http({}))
    assert result.status == "unavailable"
    assert result.candidates == []


def test_fetch_returns_error_when_http_get_raises():
    def bombs(url):
        raise ConnectionError("dns failed")
    result = fetch_hn_profile("pg", http_get=bombs)
    assert result.status == "error"
    assert "ConnectionError" in (result.error_detail or "")
    assert result.candidates == []


def test_fetch_returns_error_on_404():
    """A 404 (None body) means no such public profile."""
    http = make_http({})  # everything 404s -> None
    result = fetch_hn_profile("ghost", http_get=http)
    assert result.status == "error"
    assert result.candidates == []


def test_fetch_source_type_and_url_point_at_profile_page():
    html = _profile_html(email_row="pg@ycombinator.com")
    http = make_http({"https://news.ycombinator.com/user?id=pg": html})
    result = fetch_hn_profile("pg", http_get=http)
    src = result.candidates[0].sources[0]
    assert src.type == "hn_profile"
    assert src.url == "https://news.ycombinator.com/user?id=pg"


# ---- default fetcher routes through the SSRF-hardened lib.fetch --------------


def test_default_http_get_routes_through_lib_fetch(monkeypatch):
    """The production fetcher must go through lib.fetch.fetch (https-only,
    public-IP-pinned, redirect-revalidated, body-capped), not a raw urlopen."""
    from lib import hn_profile
    from lib.fetch import FetchResult

    calls = []

    def fake_fetch(url, *, timeout=None, **kwargs):
        calls.append(url)
        return FetchResult(url=url, status=200, content_type="text/html",
                           text="<html>hi</html>")

    monkeypatch.setattr(hn_profile, "fetch", fake_fetch)
    body = hn_profile._default_http_get("https://news.ycombinator.com/user?id=pg")
    assert calls == ["https://news.ycombinator.com/user?id=pg"]
    assert body == "<html>hi</html>"


def test_default_http_get_returns_none_on_404(monkeypatch):
    from lib import hn_profile
    from lib.fetch import FetchResult

    monkeypatch.setattr(hn_profile, "fetch",
                        lambda url, **k: FetchResult(url=url, status=404,
                                                     content_type="text/html",
                                                     text="No such user."))
    assert hn_profile._default_http_get(
        "https://news.ycombinator.com/user?id=ghost") is None
