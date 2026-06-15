"""Tests for lib/package_registry.py.

Deterministic — `http_get_json` is injected as a dict-routing fake, so no
network is required (mirrors the gh_caller / http_get routing in
test_gh_profile and test_person_resolve).
"""

from __future__ import annotations

from lib.package_registry import (
    _is_extractable,
    _parse_addresses,
    fetch_package_emails,
)


def make_http_json(routes):
    """A fake http_get_json that returns the canned dict for matching URLs.

    `routes` maps a URL prefix → a dict (or an Exception to raise). An
    unmatched URL returns {} (mirrors the real fetcher's error path: a bad
    package just yields no metadata, it doesn't blow up the batch).
    """
    def get(url, timeout=None):
        for key, resp in routes.items():
            if url.startswith(key):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return {}
    return get


# ---- _is_extractable --------------------------------------------------------


def test_is_extractable_accepts_real_addresses():
    assert _is_extractable("pete@openai.com")
    assert _is_extractable("jane.doe@acme.io")


def test_is_extractable_rejects_npm_redaction_sentinel():
    assert not _is_extractable("npm@npmjs.com")
    assert not _is_extractable("support@npmjs.com")


def test_is_extractable_rejects_masked_and_placeholder():
    assert not _is_extractable("***@***")
    assert not _is_extractable("test@example.com")
    assert not _is_extractable("noreply@anywhere.com")
    assert not _is_extractable("not-an-email")
    assert not _is_extractable("foo@")


# ---- _parse_addresses -------------------------------------------------------


def test_parse_plain_address():
    assert _parse_addresses("pete@openai.com") == ["pete@openai.com"]


def test_parse_name_angle_address_form():
    assert _parse_addresses("Peter Steinberger <pete@openai.com>") == ["pete@openai.com"]


def test_parse_comma_separated_list():
    out = _parse_addresses("Alice <alice@x.com>, bob@y.com")
    assert out == ["alice@x.com", "bob@y.com"]


def test_parse_empty_or_none():
    assert _parse_addresses(None) == []
    assert _parse_addresses("") == []


# ---- fetch_package_emails: npm ----------------------------------------------


def test_npm_author_and_maintainers():
    http = make_http_json({
        "https://registry.npmjs.org/cool-lib": {
            "author": {"name": "Pete", "email": "pete@openai.com"},
            "maintainers": [
                {"name": "Jane", "email": "jane@acme.io"},
                {"name": "npmbot", "email": "npm@npmjs.com"},  # redacted → dropped
            ],
        },
    })
    result = fetch_package_emails(
        [{"registry": "npm", "name": "cool-lib"}],
        http_get_json=http,
    )
    assert result.status == "ok"
    addresses = {c.address for c in result.candidates}
    assert addresses == {"pete@openai.com", "jane@acme.io"}
    # Source shape + type
    src = result.candidates[0].sources[0]
    assert src.type == "package_registry"
    assert src.url == "https://www.npmjs.com/package/cool-lib"
    assert "cool-lib" in src.detail


def test_npm_pulls_latest_version_author():
    """Top-level author absent; the latest published version carries it."""
    http = make_http_json({
        "https://registry.npmjs.org/widget": {
            "dist-tags": {"latest": "2.1.0"},
            "versions": {
                "2.1.0": {"author": {"name": "V", "email": "v@widgets.dev"}},
                "1.0.0": {"author": {"name": "Old", "email": "old@widgets.dev"}},
            },
        },
    })
    result = fetch_package_emails(
        [{"registry": "npm", "name": "widget"}],
        http_get_json=http,
    )
    assert {c.address for c in result.candidates} == {"v@widgets.dev"}


# ---- fetch_package_emails: pypi ---------------------------------------------


def test_pypi_author_email_name_angle_form():
    http = make_http_json({
        "https://pypi.org/pypi/coolpkg/json": {
            "info": {
                "author_email": "Jane Doe <jane@acme.io>",
                "maintainer_email": None,
            },
        },
    })
    result = fetch_package_emails(
        [{"registry": "pypi", "name": "coolpkg"}],
        http_get_json=http,
    )
    assert result.status == "ok"
    assert [c.address for c in result.candidates] == ["jane@acme.io"]
    src = result.candidates[0].sources[0]
    assert src.type == "package_registry"
    assert src.url == "https://pypi.org/project/coolpkg/"
    assert "pypi" in src.detail and "coolpkg" in src.detail


def test_pypi_comma_separated_maintainer_email():
    http = make_http_json({
        "https://pypi.org/pypi/multi/json": {
            "info": {
                "author_email": None,
                "maintainer_email": "A One <a@x.com>, b@y.com",
            },
        },
    })
    result = fetch_package_emails(
        [{"registry": "pypi", "name": "multi"}],
        http_get_json=http,
    )
    assert {c.address for c in result.candidates} == {"a@x.com", "b@y.com"}


# ---- merge-by-address across packages ---------------------------------------


def test_same_email_from_two_packages_merges_into_one_candidate():
    http = make_http_json({
        "https://registry.npmjs.org/pkg-a": {
            "author": {"email": "pete@openai.com"},
        },
        "https://pypi.org/pypi/pkg-b/json": {
            "info": {"author_email": "Pete <pete@openai.com>"},
        },
    })
    result = fetch_package_emails(
        [
            {"registry": "npm", "name": "pkg-a"},
            {"registry": "pypi", "name": "pkg-b"},
        ],
        http_get_json=http,
    )
    assert len(result.candidates) == 1
    cand = result.candidates[0]
    assert cand.address == "pete@openai.com"
    assert len(cand.sources) == 2
    urls = {s.url for s in cand.sources}
    assert urls == {
        "https://www.npmjs.com/package/pkg-a",
        "https://pypi.org/project/pkg-b/",
    }
    assert all(s.type == "package_registry" for s in cand.sources)


# ---- resilience: one bad package doesn't fail the batch ---------------------


def test_failed_package_is_skipped_not_fatal():
    """An unknown / failing package is skipped; the good one still resolves."""
    import urllib.error

    http = make_http_json({
        "https://registry.npmjs.org/ghost": urllib.error.URLError("boom"),
        "https://pypi.org/pypi/good/json": {
            "info": {"author_email": "real@good.dev"},
        },
    })
    result = fetch_package_emails(
        [
            {"registry": "npm", "name": "ghost"},
            {"registry": "pypi", "name": "good"},
        ],
        http_get_json=http,
    )
    assert result.status == "ok"
    assert [c.address for c in result.candidates] == ["real@good.dev"]


def test_unknown_package_returns_empty_metadata_no_crash():
    """A package the fake doesn't know about yields {} → no candidates,
    but the batch completes cleanly."""
    http = make_http_json({})  # everything returns {}
    result = fetch_package_emails(
        [{"registry": "npm", "name": "nope"}],
        http_get_json=http,
    )
    assert result.status == "empty"
    assert result.candidates == []


# ---- noise filtering --------------------------------------------------------


def test_redacted_and_noise_emails_filtered():
    http = make_http_json({
        "https://registry.npmjs.org/noisy": {
            "author": {"email": "npm@npmjs.com"},
            "maintainers": [
                {"email": "***@***"},
                {"email": "noreply@noisy.dev"},
                {"email": "test@example.com"},
            ],
        },
    })
    result = fetch_package_emails(
        [{"registry": "npm", "name": "noisy"}],
        http_get_json=http,
    )
    assert result.status == "empty"
    assert result.candidates == []


# ---- empty + malformed input ------------------------------------------------


def test_empty_packages_list_returns_empty():
    result = fetch_package_emails([], http_get_json=make_http_json({}))
    assert result.status == "empty"
    assert result.candidates == []


def test_malformed_entries_are_ignored():
    """Non-dict entries, blank names, and unknown registries are skipped;
    only the one valid entry contributes."""
    http = make_http_json({
        "https://registry.npmjs.org/valid": {
            "author": {"email": "pete@openai.com"},
        },
    })
    result = fetch_package_emails(
        [
            "not-a-dict",
            {"registry": "npm"},                       # missing name
            {"name": "x"},                             # missing registry
            {"registry": "rubygems", "name": "y"},     # unknown registry
            {"registry": "npm", "name": "  "},         # blank name
            {"registry": "npm", "name": "valid"},      # the only good one
        ],
        http_get_json=http,
    )
    assert result.status == "ok"
    assert [c.address for c in result.candidates] == ["pete@openai.com"]


def test_packages_not_a_list_is_hard_error():
    result = fetch_package_emails("nope", http_get_json=make_http_json({}))  # type: ignore[arg-type]
    assert result.status == "error"
    assert result.error_detail


def test_source_type_is_package_registry():
    http = make_http_json({
        "https://pypi.org/pypi/typed/json": {
            "info": {"author_email": "x@typed.dev"},
        },
    })
    result = fetch_package_emails(
        [{"registry": "pypi", "name": "typed"}],
        http_get_json=http,
    )
    assert all(
        s.type == "package_registry"
        for c in result.candidates for s in c.sources
    )


# ---- default fetcher routes through the SSRF-hardened lib.fetch --------------


def test_default_http_get_json_routes_through_lib_fetch(monkeypatch):
    """The production fetcher must go through lib.fetch.fetch, not a raw
    urlopen that follows redirects to internal hosts and reads an unbounded
    body."""
    from lib import package_registry
    from lib.fetch import FetchResult

    calls = []

    def fake_fetch(url, *, timeout=None, **kwargs):
        calls.append(url)
        return FetchResult(url=url, status=200, content_type="application/json",
                           text='{"ok": true}')

    monkeypatch.setattr(package_registry, "fetch", fake_fetch)
    data = package_registry._default_http_get_json("https://registry.npmjs.org/x")
    assert calls == ["https://registry.npmjs.org/x"]
    assert data == {"ok": True}


def test_default_http_get_json_returns_empty_on_block(monkeypatch):
    from lib import package_registry
    from lib.fetch import FetchBlocked

    def boom(url, **kwargs):
        raise FetchBlocked("refused")

    monkeypatch.setattr(package_registry, "fetch", boom)
    assert package_registry._default_http_get_json(
        "https://registry.npmjs.org/x") == {}


# ---- name encoding (path-safety hardening) ----------------------------------


def test_npm_scoped_name_is_url_encoded_into_the_path():
    """A scoped npm name keeps '@' but its '/' becomes %2F (the registry form),
    so the name lands in the path it's meant to, not a reshaped one."""
    http = make_http_json({
        "https://registry.npmjs.org/@scope%2Fpkg": {
            "maintainers": [{"name": "Dev", "email": "dev@scope.example"}],
        },
    })
    result = fetch_package_emails(
        [{"registry": "npm", "name": "@scope/pkg"}],
        http_get_json=http,
    )
    assert {c.address for c in result.candidates} == {"dev@scope.example"}


def test_npm_name_query_chars_are_encoded_not_reshaping_the_request():
    """A name carrying '?' must be percent-encoded, never turned into a query."""
    seen: dict[str, str] = {}

    def get(url, timeout=None):
        seen["url"] = url
        return {}

    fetch_package_emails([{"registry": "npm", "name": "pkg?inject=1"}],
                         http_get_json=get)
    path = seen["url"].split("registry.npmjs.org/", 1)[1]
    assert "?" not in path
    assert "%3F" in seen["url"]
