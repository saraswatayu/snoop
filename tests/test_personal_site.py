"""Tests for lib/personal_site.py.

Deterministic — http_get is injected. The v1 scope is mailto-only;
these tests verify that scope holds (no regex extraction from visible
text, no Cloudflare deobfuscation).
"""

from __future__ import annotations

from datetime import datetime, timezone

from lib.personal_site import (
    _extract_mailto_addresses,
    _is_extractable,
    fetch_personal_site,
)
from tests._http_harness import make_http  # shared fake-HTTP harness (ENG-7)


NOW = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)


# ---- _is_extractable --------------------------------------------------------


def test_is_extractable_keeps_generic_legitimate_inboxes():
    """One-person consultancies publish `hello@theirname.com` as personal
    contact. Don't drop these at extraction time — scorer downranks later."""
    assert _is_extractable("hello@theirname.com")
    assert _is_extractable("info@solo-consultant.com")
    assert _is_extractable("contact@designer.dev")


def test_is_extractable_drops_system_only_addresses():
    assert not _is_extractable("noreply@anywhere.com")
    assert not _is_extractable("postmaster@somesite.io")
    assert not _is_extractable("webmaster@oldsite.org")
    assert not _is_extractable("abuse@isp.net")
    assert not _is_extractable("mailer-daemon@server.com")


def test_is_extractable_drops_placeholder_domains():
    assert not _is_extractable("anyone@example.com")
    assert not _is_extractable("test@test.invalid")


def test_is_extractable_drops_localhost_addresses():
    """Consistency with gh_profile and git_emails — `localhost` and `local`
    are placeholder TLDs and shouldn't surface as candidates."""
    assert not _is_extractable("me@localhost")
    assert not _is_extractable("me@my.local")


# ---- _extract_mailto_addresses ----------------------------------------------


def test_extract_basic_mailto_anchor():
    html = '<a href="mailto:pete@openai.com">email me</a>'
    assert _extract_mailto_addresses(html) == ["pete@openai.com"]


def test_extract_handles_single_quoted_href():
    html = "<a href='mailto:pete@openai.com'>email</a>"
    assert _extract_mailto_addresses(html) == ["pete@openai.com"]


def test_extract_strips_query_string():
    """mailto:foo@bar.com?subject=Hello → just foo@bar.com"""
    html = '<a href="mailto:pete@openai.com?subject=Hi&body=Cheers">contact</a>'
    assert _extract_mailto_addresses(html) == ["pete@openai.com"]


def test_extract_handles_url_encoded_addresses():
    html = '<a href="mailto:pete%40openai.com">contact</a>'
    assert _extract_mailto_addresses(html) == ["pete@openai.com"]


def test_extract_dedups_repeated_anchors():
    html = '''
        <a href="mailto:pete@openai.com">email</a>
        <p>also</p>
        <a href="mailto:pete@openai.com">again</a>
    '''
    assert _extract_mailto_addresses(html) == ["pete@openai.com"]


def test_extract_returns_multiple_distinct_in_document_order():
    html = '''
        <a href="mailto:work@example.org">work</a>  <!-- dropped (example.org) -->
        <a href="mailto:work@acme.com">work</a>
        <a href="mailto:personal@gmail.com">personal</a>
    '''
    assert _extract_mailto_addresses(html) == ["work@acme.com", "personal@gmail.com"]


def test_extract_skips_cloudflare_protected_stubs():
    """Cloudflare rewrites real mailto: hrefs into /cdn-cgi/l/email-protection.
    We don't deobfuscate in v1 — just skip these without polluting candidates."""
    html = '''
        <a href="/cdn-cgi/l/email-protection#abc123def">[email protected]</a>
        <a href="mailto:pete@openai.com">visible address</a>
    '''
    assert _extract_mailto_addresses(html) == ["pete@openai.com"]


def test_extract_does_not_pick_up_raw_text_emails():
    """v1 scope: mailto-only. Raw text like 'reach me at pete@openai.com'
    should NOT be extracted — that's a v2 feature when we deal with the
    full HTML scraping fragility tax."""
    html = '''
        <p>Reach me at pete@openai.com — looking forward to chatting!</p>
        <p>Or alternative@somewhere.com works too.</p>
    '''
    assert _extract_mailto_addresses(html) == []


def test_extract_drops_system_addresses_from_mailto():
    html = '''
        <a href="mailto:webmaster@oldsite.com">webmaster</a>
        <a href="mailto:pete@oldsite.com">pete</a>
    '''
    assert _extract_mailto_addresses(html) == ["pete@oldsite.com"]


def test_extract_handles_empty_or_no_html():
    assert _extract_mailto_addresses("") == []
    assert _extract_mailto_addresses("just plain text, no anchors") == []


def test_extract_handles_messy_whitespace_in_href():
    html = '<a href = " mailto: pete@openai.com ">email</a>'
    assert _extract_mailto_addresses(html) == ["pete@openai.com"]


# ---- fetch_personal_site: happy paths ---------------------------------------


def test_fetch_extracts_mailto_from_homepage():
    http = make_http({
        "https://steipete.com/":
            '<html><body><a href="mailto:pete@steipete.com">contact</a></body></html>',
    })
    result = fetch_personal_site(["steipete.com"], http_get=http, now=NOW)
    assert result.status == "ok"
    assert [c.address for c in result.candidates] == ["pete@steipete.com"]
    src = result.candidates[0].sources[0]
    assert src.type == "personal_site"
    assert src.url == "https://steipete.com/"


def test_fetch_tries_about_when_homepage_has_no_mailto():
    """Homepage loads but has no mailto; /about has one. Both fetches happen
    and the /about hit is captured."""
    http = make_http({
        "https://example-site.com/":
            "<html>welcome, no contact info here</html>",
        "https://example-site.com/about":
            '<a href="mailto:jane@example-site.com">about-page email</a>',
    })
    result = fetch_personal_site(["example-site.com"], http_get=http, now=NOW)
    assert [c.address for c in result.candidates] == ["jane@example-site.com"]
    assert "/about" in result.candidates[0].sources[0].url


def test_fetch_aggregates_across_multiple_paths():
    """If both / and /contact have mailto: anchors, both surface as
    separate Source entries on the same EmailCandidate."""
    html = '<a href="mailto:jane@acme.com">email me</a>'
    http = make_http({
        "https://acme.com/": html,
        "https://acme.com/contact": html,
    })
    result = fetch_personal_site(["acme.com"], http_get=http, now=NOW)
    assert len(result.candidates) == 1
    assert len(result.candidates[0].sources) >= 2


def test_fetch_handles_multiple_personal_domains():
    """A target might have both steipete.com AND steipete.dev declared."""
    http = make_http({
        "https://steipete.com/":
            '<a href="mailto:pete@steipete.com">main</a>',
        "https://steipete.dev/":
            '<a href="mailto:dev@steipete.dev">dev blog</a>',
    })
    result = fetch_personal_site(
        ["steipete.com", "steipete.dev"], http_get=http, now=NOW,
    )
    addresses = {c.address for c in result.candidates}
    assert addresses == {"pete@steipete.com", "dev@steipete.dev"}


# ---- fetch_personal_site: empty / unavailable -------------------------------


def test_fetch_returns_unavailable_with_no_domains():
    result = fetch_personal_site([], http_get=make_http({}), now=NOW)
    assert result.status == "unavailable"
    assert result.candidates == []


def test_fetch_returns_empty_when_pages_load_but_no_mailto():
    """Differentiate 'site loaded successfully but author didn't publish
    a mailto' from 'site unreachable.'"""
    http = make_http({
        "https://noemail.com/": "<html>I never put my email online</html>",
        "https://noemail.com/about": "<html>still no email</html>",
        "https://noemail.com/contact": "<html>I prefer twitter</html>",
    })
    result = fetch_personal_site(["noemail.com"], http_get=http, now=NOW)
    assert result.status == "empty"
    assert result.candidates == []
    assert "no mailto" in (result.error_detail or "")


def test_fetch_returns_error_when_all_fetches_fail():
    import urllib.error
    def bombs(url):
        raise urllib.error.URLError("connection refused")
    result = fetch_personal_site(["unreachable.com"], http_get=bombs, now=NOW)
    assert result.status == "error"
    assert "URLError" in (result.error_detail or "") or "all fetches failed" in (result.error_detail or "")


def test_fetch_continues_when_some_paths_404():
    """A 404 on / shouldn't stop the resolver from trying /about."""
    http = make_http({
        # "/" not in routes => returns None (404)
        "https://patchy.com/about":
            '<a href="mailto:jane@patchy.com">about</a>',
    })
    result = fetch_personal_site(["patchy.com"], http_get=http, now=NOW)
    assert result.status == "ok"
    assert [c.address for c in result.candidates] == ["jane@patchy.com"]


def test_fetch_handles_empty_string_domains_gracefully():
    """Robustness: ["", " ", "real.com"] should ignore the empty strings."""
    http = make_http({
        "https://real.com/":
            '<a href="mailto:jane@real.com">email</a>',
    })
    result = fetch_personal_site(["", "   ", "real.com"], http_get=http, now=NOW)
    assert [c.address for c in result.candidates] == ["jane@real.com"]


def test_fetch_one_domain_fails_others_succeed():
    """When one domain is unreachable but another works, surface what works
    rather than returning error for the whole resolver."""
    import urllib.error

    routes = {
        "https://good.com/":
            '<a href="mailto:jane@good.com">email</a>',
    }
    def get(url):
        if url.startswith("https://bad.com/"):
            raise urllib.error.URLError("DNS failure")
        if url in routes:
            return routes[url]
        return None

    result = fetch_personal_site(["bad.com", "good.com"], http_get=get, now=NOW)
    # bad.com errors, good.com loads — overall status is ok because we
    # extracted at least one address.
    assert result.status == "ok"
    assert [c.address for c in result.candidates] == ["jane@good.com"]
