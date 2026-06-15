"""Tests for the shared fake-HTTP harness (ENG-7) and a meta-check that the
fetch sensor tests actually reuse it rather than rolling their own fakes."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.fetch import FetchBlocked, FetchResult
from tests._http_harness import make_fetch, make_http


def test_make_fetch_string_route_is_200_html():
    f = make_fetch({"https://x.test/": "<html>hi</html>"})
    r = f("https://x.test/")
    assert isinstance(r, FetchResult)
    assert r.status == 200 and r.content_type == "text/html" and "hi" in r.text


def test_make_fetch_tuple_route_is_tailored():
    f = make_fetch({"https://x.test/": (404, "text/plain", "nope")})
    r = f("https://x.test/")
    assert r.status == 404 and r.content_type == "text/plain"


def test_make_fetch_passthrough_fetchresult_and_exception():
    fr = FetchResult(url="u", status=200, content_type="application/pgp-keys", text="k")
    f = make_fetch({"u": fr, "bad": FetchBlocked("refused")})
    assert f("u") is fr
    with pytest.raises(FetchBlocked):
        f("bad")


def test_make_fetch_unmapped_block_vs_notfound():
    blocker = make_fetch({})                       # default: block
    with pytest.raises(FetchBlocked):
        blocker("https://nope.test/")
    notfounder = make_fetch({}, unmapped="notfound")
    r = notfounder("https://nope.test/")
    assert r.status == 404


def test_make_fetch_records_calls_with_content_types():
    rec: list = []
    f = make_fetch({}, record=rec, unmapped="notfound")
    f("https://x.test/", allowed_content_types=frozenset({"application/pgp-keys"}))
    assert rec == [("https://x.test/", frozenset({"application/pgp-keys"}))]


def test_make_http_body_none_and_raise():
    g = make_http({"https://s.test/": "<html>body</html>",
                   "https://s.test/boom": RuntimeError("x")})
    assert "body" in g("https://s.test/")
    assert g("https://s.test/missing") is None      # unmapped → 404/None
    with pytest.raises(RuntimeError):
        g("https://s.test/boom")


def test_fetch_sensor_tests_reuse_the_shared_harness():
    """ENG-7: rel_me / pgp_keyserver / personal_site must NOT define their own
    HTTP fake — they import the one in tests/_http_harness.py."""
    tests_dir = Path(__file__).resolve().parent
    for name in ("test_rel_me.py", "test_pgp_keyserver.py", "test_personal_site.py"):
        src = (tests_dir / name).read_text()
        assert "_http_harness import" in src, f"{name} does not import the shared harness"
        assert "def make_fetch(" not in src, f"{name} still defines a local make_fetch"
        assert "def make_http(" not in src, f"{name} still defines a local make_http"
