"""Tests for lib/rel_me.py — rel="me" bidirectional identity verification.

Deterministic — fetch_fn is injected and routed by URL, so no network. The
fake returns FetchResult objects (or raises FetchBlocked / OSError) keyed on
the exact URL requested.
"""

from __future__ import annotations

import pytest

from lib.fetch import FetchBlocked
from lib.rel_me import (
    RelMeLink,
    _absolute_https,
    _classify_platform,
    _extract_rel_me_hrefs,
    _links_back_to_domain,
    verify_rel_me,
)
from tests._http_harness import make_fetch  # shared fake-HTTP harness (ENG-7)


# ---- _extract_rel_me_hrefs --------------------------------------------------


def test_extract_from_anchor_tag():
    html = '<a rel="me" href="https://fosstodon.org/@jane">mastodon</a>'
    assert _extract_rel_me_hrefs(html) == ["https://fosstodon.org/@jane"]


def test_extract_from_link_tag():
    html = '<link rel="me" href="https://github.com/jane">'
    assert _extract_rel_me_hrefs(html) == ["https://github.com/jane"]


def test_extract_from_both_link_and_anchor():
    html = '''
        <link rel="me" href="https://github.com/jane">
        <a rel="me" href="https://fosstodon.org/@jane">masto</a>
    '''
    assert _extract_rel_me_hrefs(html) == [
        "https://github.com/jane",
        "https://fosstodon.org/@jane",
    ]


def test_extract_rel_token_list():
    """rel="me noopener" — must match the "me" token within a list."""
    html = '<a rel="me noopener noreferrer" href="https://github.com/jane">gh</a>'
    assert _extract_rel_me_hrefs(html) == ["https://github.com/jane"]


def test_extract_ignores_non_me_rel():
    html = '<a rel="nofollow" href="https://example.com/x">x</a>'
    assert _extract_rel_me_hrefs(html) == []


def test_extract_attribute_order_independent():
    html = '<a href="https://github.com/jane" rel="me">gh</a>'
    assert _extract_rel_me_hrefs(html) == ["https://github.com/jane"]


def test_extract_dedups():
    html = '''
        <a rel="me" href="https://github.com/jane">gh</a>
        <a rel="me" href="https://github.com/jane">gh again</a>
    '''
    assert _extract_rel_me_hrefs(html) == ["https://github.com/jane"]


def test_extract_empty_html():
    assert _extract_rel_me_hrefs("") == []


# ---- _absolute_https --------------------------------------------------------


def test_absolute_https_accepts_https():
    assert _absolute_https("https://github.com/jane") == "https://github.com/jane"


def test_absolute_https_rejects_relative():
    assert _absolute_https("/about") is None
    assert _absolute_https("me/profile") is None


def test_absolute_https_rejects_non_https():
    assert _absolute_https("http://insecure.example/x") is None
    assert _absolute_https("mailto:me@example.com") is None
    assert _absolute_https("//protocol-relative.example/x") is None


# ---- _classify_platform -----------------------------------------------------


def test_classify_github():
    assert _classify_platform("github.com") == "github"


def test_classify_bluesky():
    assert _classify_platform("bsky.app") == "bluesky"
    assert _classify_platform("jane.bsky.social") == "bluesky"


def test_classify_mastodon_canonical():
    assert _classify_platform("mastodon.social") == "mastodon"


def test_classify_other():
    assert _classify_platform("fosstodon.org") == "other"
    assert _classify_platform("randomhost.example") == "other"


# ---- _links_back_to_domain --------------------------------------------------


def test_links_back_via_rel_me():
    html = '<a rel="me" href="https://jane.com/">my site</a>'
    assert _links_back_to_domain(html, "jane.com")


def test_plain_href_backlink_is_not_reciprocal():
    """SECURITY: a plain (non-rel=me) link back to the domain is NOT a mutual
    rel=me attestation. Accepting any href lets an attacker forge a
    bidirectional binding whenever the victim's profile merely links to the
    attacker's domain (a bio URL, a pinned-repo homepage). IndieAuth requires
    the backlink itself to carry rel="me"."""
    html = '<a href="https://jane.com/contact">contact</a>'
    assert not _links_back_to_domain(html, "jane.com")


def test_links_back_strips_www():
    html = '<a rel="me" href="https://www.jane.com/">site</a>'
    assert _links_back_to_domain(html, "jane.com")


def test_no_back_link():
    html = '<a rel="me" href="https://someoneelse.com/">other</a>'
    assert not _links_back_to_domain(html, "jane.com")


# ---- verify_rel_me: bidirectional asserted ----------------------------------


def test_mastodon_bidirectional_asserted():
    """Site links to a Mastodon profile with rel=me; the profile links back
    to the domain → bidirectional, asserted."""
    fetch_fn = make_fetch({
        "https://jane.com/":
            '<a rel="me" href="https://fosstodon.org/@jane">mastodon</a>',
        "https://fosstodon.org/@jane":
            '<a rel="me" href="https://jane.com/">my website</a>',
    })
    links = verify_rel_me("jane.com", fetch_fn=fetch_fn)
    assert len(links) == 1
    link = links[0]
    assert link.url == "https://fosstodon.org/@jane"
    assert link.bidirectional is True
    assert link.tier == "asserted"


def test_github_no_backlink_possibly():
    """Site links to a GitHub profile with rel=me, but the GitHub page has no
    back-reference → possibly (not asserted)."""
    fetch_fn = make_fetch({
        "https://jane.com/":
            '<link rel="me" href="https://github.com/jane">',
        "https://github.com/jane":
            '<html><body>GitHub profile with no link back</body></html>',
    })
    links = verify_rel_me("jane.com", fetch_fn=fetch_fn)
    assert len(links) == 1
    link = links[0]
    assert link.platform == "github"
    assert link.bidirectional is False
    assert link.tier == "possibly"


def test_rel_me_parsed_from_both_tag_types():
    """One <link rel=me> (github, links back) and one <a rel=me> (mastodon,
    links back) — both surface, both asserted."""
    fetch_fn = make_fetch({
        "https://jane.com/": '''
            <link rel="me" href="https://github.com/jane">
            <a rel="me noopener" href="https://fosstodon.org/@jane">masto</a>
        ''',
        "https://github.com/jane":
            '<a rel="me" href="https://jane.com/">website</a>',
        "https://fosstodon.org/@jane":
            '<a rel="me" href="https://jane.com/">site</a>',
    })
    links = verify_rel_me("jane.com", fetch_fn=fetch_fn)
    urls = {l.url for l in links}
    assert urls == {"https://github.com/jane", "https://fosstodon.org/@jane"}
    assert all(l.bidirectional and l.tier == "asserted" for l in links)
    platforms = {l.platform for l in links}
    assert platforms == {"github", "other"}  # fosstodon classified "other"


def test_rel_token_list_is_matched():
    """rel="me noopener" must be recognized as a rel=me target."""
    fetch_fn = make_fetch({
        "https://jane.com/":
            '<a rel="me noopener" href="https://github.com/jane">gh</a>',
        "https://github.com/jane":
            '<a rel="me" href="https://jane.com/">back</a>',
    })
    links = verify_rel_me("jane.com", fetch_fn=fetch_fn)
    assert len(links) == 1
    assert links[0].platform == "github"
    assert links[0].bidirectional is True


# ---- verify_rel_me: bluesky well-known --------------------------------------


def test_bluesky_well_known_handle_asserted():
    """A domain that IS a Bluesky handle: /.well-known/atproto-did returns a
    did: string → asserted bluesky link, no rel=me needed."""
    fetch_fn = make_fetch({
        "https://jane.com/": "<html><body>no rel me here</body></html>",
        "https://jane.com/.well-known/atproto-did":
            (200, "text/plain", "did:plc:abc123xyz"),
    })
    links = verify_rel_me("jane.com", fetch_fn=fetch_fn)
    assert len(links) == 1
    link = links[0]
    assert link.platform == "bluesky"
    assert link.url == "https://bsky.app/profile/jane.com"
    assert link.bidirectional is True
    assert link.tier == "asserted"
    assert "atproto-did" in link.detail


def test_bluesky_well_known_non_did_body_ignored():
    """A 200 whose body is not a did: string is not a handle."""
    fetch_fn = make_fetch({
        "https://jane.com/": "<html>nothing</html>",
        "https://jane.com/.well-known/atproto-did":
            (200, "text/html", "<html>404 page</html>"),
    })
    links = verify_rel_me("jane.com", fetch_fn=fetch_fn)
    assert links == []


def test_bluesky_well_known_missing_is_skipped():
    """The well-known endpoint being unmapped (FetchBlocked) must not crash;
    it's simply skipped."""
    fetch_fn = make_fetch({
        "https://jane.com/": "<html>nothing</html>",
        # no atproto-did route -> FetchBlocked
    })
    links = verify_rel_me("jane.com", fetch_fn=fetch_fn)
    assert links == []


# ---- verify_rel_me: defensive degradation -----------------------------------


def test_target_fetch_blocked_degrades_to_possibly():
    """The profile fetch raising FetchBlocked degrades the link to possibly
    rather than crashing the whole verification."""
    fetch_fn = make_fetch({
        "https://jane.com/":
            '<a rel="me" href="https://github.com/jane">gh</a>',
        "https://github.com/jane": FetchBlocked("blocked target"),
    })
    links = verify_rel_me("jane.com", fetch_fn=fetch_fn)
    assert len(links) == 1
    assert links[0].bidirectional is False
    assert links[0].tier == "possibly"


def test_target_fetch_oserror_degrades_to_possibly():
    """An OSError (network/TLS) on the target also degrades, not raises."""
    fetch_fn = make_fetch({
        "https://jane.com/":
            '<a rel="me" href="https://github.com/jane">gh</a>',
        "https://github.com/jane": OSError("connection reset"),
    })
    links = verify_rel_me("jane.com", fetch_fn=fetch_fn)
    assert len(links) == 1
    assert links[0].tier == "possibly"


def test_site_fetch_fails_returns_empty():
    """If the site's OWN fetch fails, return [] (nothing to verify)."""
    fetch_fn = make_fetch({
        "https://jane.com/": FetchBlocked("site unreachable"),
    })
    assert verify_rel_me("jane.com", fetch_fn=fetch_fn) == []


def test_site_fetch_oserror_returns_empty():
    fetch_fn = make_fetch({
        "https://jane.com/": OSError("dns failure"),
    })
    assert verify_rel_me("jane.com", fetch_fn=fetch_fn) == []


# ---- verify_rel_me: parsing edge cases --------------------------------------


def test_relative_and_non_https_rel_me_ignored():
    """Relative and non-https rel=me hrefs are dropped; only the https one is
    kept."""
    fetch_fn = make_fetch({
        "https://jane.com/": '''
            <a rel="me" href="/about">relative</a>
            <a rel="me" href="http://insecure.example/me">non-https</a>
            <a rel="me" href="mailto:jane@jane.com">mailto</a>
            <a rel="me" href="https://github.com/jane">real</a>
        ''',
        "https://github.com/jane":
            '<a href="https://jane.com/">back</a>',
    })
    links = verify_rel_me("jane.com", fetch_fn=fetch_fn)
    assert len(links) == 1
    assert links[0].url == "https://github.com/jane"


def test_self_referential_rel_me_ignored():
    """A rel=me link from the site to itself is not a profile target."""
    fetch_fn = make_fetch({
        "https://jane.com/":
            '<a rel="me" href="https://jane.com/">myself</a>',
    })
    links = verify_rel_me("jane.com", fetch_fn=fetch_fn)
    assert links == []


def test_max_links_cap_respected():
    """With more rel=me targets than max_links, only the cap is processed."""
    targets = "".join(
        f'<a rel="me" href="https://host{i}.example/u">p{i}</a>'
        for i in range(10)
    )
    routes = {"https://jane.com/": targets}
    # No backlinks wired -> all "possibly", but capping is what we assert.
    fetch_fn = make_fetch(routes)
    links = verify_rel_me("jane.com", fetch_fn=fetch_fn, max_links=3)
    assert len(links) == 3


def test_empty_domain_returns_empty():
    fetch_fn = make_fetch({})
    assert verify_rel_me("", fetch_fn=fetch_fn) == []


def test_relmelink_dataclass_fields():
    """Contract check: the dataclass exposes the agreed fields."""
    link = RelMeLink(
        platform="github", url="https://github.com/jane",
        bidirectional=True, tier="asserted", detail="x",
    )
    assert link.platform == "github"
    assert link.url == "https://github.com/jane"
    assert link.bidirectional is True
    assert link.tier == "asserted"
    assert link.detail == "x"
    # detail is optional
    link2 = RelMeLink(platform="other", url="https://x.example/",
                      bidirectional=False, tier="possibly")
    assert link2.detail == ""
