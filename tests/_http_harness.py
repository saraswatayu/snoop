"""Shared fake-HTTP harness for fetch-based sensor tests (ENG-7).

ONE routed HTTP fake, reused by every fetch sensor test (rel_me, pgp_keyserver,
personal_site, …) instead of each rolling its own. Keeping it here means a sensor
test can't silently diverge from the real `lib.fetch.FetchResult` shape, and the
SSRF / redirect / decompressed-size-cap guard matrix stays the single
responsibility of `test_fetch.py` against the real `lib.fetch` path — a sensor
test exercises behaviour with this fake, the guards are proven once, separately.

Two flavours:
- `make_fetch` yields `FetchResult` (for sensors taking a `fetch_fn`, i.e. anything
  that goes through `lib.fetch`: rel_me, pgp_keyserver).
- `make_http` yields a raw body string (for `fetch_personal_site`, which takes a
  plain `http_get` rather than `lib.fetch`).
"""

from __future__ import annotations

from typing import Any

from lib.fetch import FetchBlocked, FetchResult


def _as_result(url: str, resp: Any) -> FetchResult:
    """Coerce a routed value into a FetchResult. An Exception is raised; a
    FetchResult passes through; a (status, content_type, body) tuple is built
    out; a bare string is a 200 text/html page."""
    if isinstance(resp, Exception):
        raise resp
    if isinstance(resp, FetchResult):
        return resp
    if isinstance(resp, tuple):
        status, ctype, body = resp
        return FetchResult(url=url, status=status, content_type=ctype, text=body)
    return FetchResult(url=url, status=200, content_type="text/html", text=str(resp))


def make_fetch(routes: dict, *, record: list | None = None, unmapped: str = "block"):
    """A fake `fetch_fn(url, *, allowed_content_types=None, **kw) -> FetchResult`.

    `routes` maps an exact URL to a FetchResult, an html string (→ 200
    text/html), a (status, content_type, body) tuple, or an Exception (raised).
    An unmapped URL either raises FetchBlocked (`unmapped="block"`, the rel=me
    default — a host-not-public / refusal) or returns a 404 FetchResult
    (`unmapped="notfound"`, the keyserver default — "not a verified UID"). If
    `record` is a list, each (url, allowed_content_types) call is appended.
    """
    def fetch_fn(url, *, allowed_content_types=None, **kw):
        if record is not None:
            record.append((url, allowed_content_types))
        if url in routes:
            return _as_result(url, routes[url])
        if unmapped == "notfound":
            return FetchResult(url=url, status=404, content_type="text/plain",
                               text="not found")
        raise FetchBlocked(f"unmapped url: {url}")
    return fetch_fn


def make_http(routes: dict):
    """A fake `http_get(url) -> body str | None` for `fetch_personal_site` (which
    takes a plain http_get, not lib.fetch). Mapped value → body; an Exception is
    raised; an unmapped URL → None (treated as 404). Exact-match so mapping both
    "/" and "/about" doesn't let the first swallow the second."""
    def get(url):
        if url in routes:
            resp = routes[url]
            if isinstance(resp, Exception):
                raise resp
            return resp
        return None
    return get
