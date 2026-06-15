"""Tests for lib/fetch.py — the SSRF-hardened HTTP GET.

No network: the IP-classification and scheme/redirect/size/content-type guards
are exercised directly with an injected `opener` (and `resolve`), so we test the
defenses, not the wire.
"""

from __future__ import annotations

import pytest

from lib.fetch import FetchBlocked, fetch, is_public_host


# ---- is_public_host (the SSRF core) ------------------------------------------


def test_public_ip_literals_pass():
    assert is_public_host("8.8.8.8")
    assert is_public_host("1.1.1.1")


@pytest.mark.parametrize("ip", [
    "127.0.0.1",         # loopback
    "10.0.0.5",          # private
    "192.168.1.1",       # private
    "172.16.0.1",        # private
    "169.254.169.254",   # link-local (cloud metadata)
    "0.0.0.0",           # unspecified
    "::1",               # ipv6 loopback
    "fd00::1",           # ipv6 unique-local
])
def test_private_and_reserved_ip_literals_blocked(ip):
    assert not is_public_host(ip)


def test_host_resolving_to_private_is_blocked():
    # a name that resolves to a private IP must fail closed
    assert not is_public_host("evil.example", resolve=lambda h: ["10.1.2.3"])


def test_host_with_any_private_address_is_blocked():
    # one public + one private resolution → blocked (all must be public)
    assert not is_public_host("mixed.example", resolve=lambda h: ["8.8.8.8", "127.0.0.1"])


def test_unresolvable_host_blocked():
    def boom(host):
        raise OSError("nxdomain")
    assert not is_public_host("nope.example", resolve=boom)


def test_empty_host_blocked():
    assert not is_public_host(None)
    assert not is_public_host("")


# ---- fetch guards ------------------------------------------------------------


def _opener(status=200, headers=None, body=b"<html>ok</html>"):
    calls = []
    def open_(host, port, path, timeout):
        calls.append((host, port, path))
        return status, headers or {"Content-Type": "text/html"}, body
    open_.calls = calls
    return open_


_PUB = lambda h: ["93.184.216.34"]  # example.com-ish public IP


def test_rejects_non_https_scheme():
    with pytest.raises(FetchBlocked) as e:
        fetch("http://example.com/", opener=_opener(), resolve=_PUB)
    assert "non-https" in str(e.value)


def test_rejects_private_target():
    with pytest.raises(FetchBlocked) as e:
        fetch("https://internal.example/", opener=_opener(),
              resolve=lambda h: ["10.0.0.1"])
    assert "public address" in str(e.value)


def test_returns_body_on_success():
    r = fetch("https://example.com/page", opener=_opener(body=b"hello"),
              resolve=_PUB)
    assert r.status == 200
    assert r.content_type == "text/html"
    assert "hello" in r.text


def test_content_type_allowlist_enforced():
    op = _opener(headers={"Content-Type": "application/zip"}, body=b"PK\x03\x04")
    with pytest.raises(FetchBlocked) as e:
        fetch("https://example.com/x", opener=op, resolve=_PUB)
    assert "content-type" in str(e.value)


def test_redirect_to_private_host_is_blocked_not_followed():
    op = _opener(status=302, headers={"Location": "https://169.254.169.254/latest/"})
    with pytest.raises(FetchBlocked) as e:
        fetch("https://example.com/r", opener=op, resolve=_PUB)
    # the redirect target is re-validated through is_public_host → blocked
    assert "public address" in str(e.value)


def test_redirect_followed_to_public_host():
    seen = []
    def op(host, port, path, timeout):
        seen.append(host)
        if host == "example.com":
            return 301, {"Location": "https://cdn.example.org/final"}, b""
        return 200, {"Content-Type": "text/html"}, b"<html>final</html>"
    r = fetch("https://example.com/start", opener=op,
              resolve=lambda h: ["93.184.216.34"])
    assert "final" in r.text
    assert seen == ["example.com", "cdn.example.org"]


def test_too_many_redirects():
    op = _opener(status=307, headers={"Location": "https://example.com/loop"})
    with pytest.raises(FetchBlocked) as e:
        fetch("https://example.com/loop", opener=op, resolve=_PUB, max_redirects=2)
    assert "too many redirects" in str(e.value)


def test_decompressed_size_cap_truncates():
    # a gzip payload that decompresses past the cap is truncated, not exploded
    import gzip as _gz
    big = _gz.compress(b"A" * 100_000)
    op = _opener(headers={"Content-Type": "text/plain", "Content-Encoding": "gzip"},
                 body=big)
    r = fetch("https://example.com/big", opener=op, resolve=_PUB, max_bytes=1000)
    assert len(r.text) == 1000


def test_raw_deflate_body_is_decoded():
    # Some servers send headerless (raw) DEFLATE for Content-Encoding: deflate.
    # zlib.decompressobj() (zlib-wrapped) raises zlib.error on it; the decoder
    # must fall back to raw deflate, not crash the sensor with an uncaught
    # zlib.error (which is neither FetchBlocked nor OSError).
    import zlib
    co = zlib.compressobj(9, zlib.DEFLATED, -15)  # wbits<0 → raw deflate
    raw = co.compress(b"hello raw deflate") + co.flush()
    op = _opener(headers={"Content-Type": "text/plain", "Content-Encoding": "deflate"},
                 body=raw)
    r = fetch("https://example.com/d", opener=op, resolve=_PUB)
    assert "hello raw deflate" in r.text


def test_undecodable_deflate_raises_fetchblocked_not_zliberror():
    # A garbage deflate body must surface as FetchBlocked (which sensors catch),
    # never an uncaught zlib.error.
    op = _opener(headers={"Content-Type": "text/plain", "Content-Encoding": "deflate"},
                 body=b"this is plainly not a deflate stream")
    with pytest.raises(FetchBlocked):
        fetch("https://example.com/d", opener=op, resolve=_PUB)


def test_truncated_gzip_raises_fetchblocked_not_eoferror():
    # A gzip body cut off mid-stream (server closed the connection mid-body) makes
    # gzip.GzipFile.read raise EOFError, which is NOT an OSError subclass — so
    # without a guard it escapes a sensor's `except (FetchBlocked, OSError)` and
    # crashes pgp/package_registry. Like the deflate path it must surface as
    # FetchBlocked.
    import gzip as _gz
    good = _gz.compress(b"A" * 100_000)
    truncated = good[: len(good) // 2]
    op = _opener(headers={"Content-Type": "text/plain", "Content-Encoding": "gzip"},
                 body=truncated)
    with pytest.raises(FetchBlocked):
        fetch("https://example.com/g", opener=op, resolve=_PUB)


def test_garbage_gzip_raises_fetchblocked():
    # A body that is not a gzip stream at all raises gzip.BadGzipFile; surface it
    # as FetchBlocked too (parity with the deflate guard), not a raw decode error.
    op = _opener(headers={"Content-Type": "text/plain", "Content-Encoding": "gzip"},
                 body=b"this is plainly not a gzip stream")
    with pytest.raises(FetchBlocked):
        fetch("https://example.com/g", opener=op, resolve=_PUB)


def test_default_resolve_is_time_bounded(monkeypatch):
    # The default DNS resolver must not hang indefinitely on a slow/blackholing
    # nameserver — getaddrinfo has no timeout of its own, so the helper bounds it.
    import socket
    import threading
    import time as _time
    from lib import fetch as fetchmod

    def hang(*a, **k):
        _time.sleep(30)
    monkeypatch.setattr(socket, "getaddrinfo", hang)
    monkeypatch.setattr(fetchmod, "_RESOLVE_TIMEOUT_SEC", 0.2)

    start = _time.monotonic()
    with pytest.raises(OSError):
        fetchmod._default_resolve("slow.example")
    assert _time.monotonic() - start < 5  # returned promptly, didn't hang


# ---- is_global-class ranges (CGNAT/benchmarking/doc/IPv4-mapped) --------------


@pytest.mark.parametrize("ip", [
    "100.64.0.1",              # CGNAT / RFC 6598 shared space (Tailscale tailnets)
    "100.100.100.100",        # Tailscale MagicDNS resolver
    "198.18.0.5",             # benchmarking (RFC 2544)
    "192.0.2.1",              # documentation (TEST-NET-1)
    "::ffff:127.0.0.1",       # IPv4-mapped IPv6 loopback
    "::ffff:169.254.169.254", # IPv4-mapped IPv6 link-local (cloud metadata)
])
def test_non_global_ranges_blocked(ip):
    # is_global rejects globally-unroutable ranges the hand-rolled is_private/
    # is_reserved flags miss (CGNAT, benchmarking, documentation). A target-
    # controlled name resolving here must not reach the operator's overlay
    # (e.g. Tailscale) or internal network.
    assert not is_public_host(ip)


def test_host_resolving_to_cgnat_is_blocked():
    # the same guard must hold for a NAME that resolves into CGNAT space
    assert not is_public_host("tailnet.example", resolve=lambda h: ["100.64.1.2"])


# ---- _pinned_https_open: the DNS-rebinding pin (was monkeypatched away) -------


def test_pinned_open_blocks_rebind_to_private(monkeypatch):
    # _pinned_https_open re-resolves and pins to a validated public IP. If the
    # name now resolves only to a private address (DNS rebinding between the
    # is_public_host check and the connect), it must raise FetchBlocked rather
    # than open a socket to the internal host.
    from lib import fetch as fetchmod
    monkeypatch.setattr(fetchmod, "_getaddrinfo_bounded",
                        lambda *a, **k: [(2, 1, 6, "", ("10.0.0.1", 443))])
    with pytest.raises(fetchmod.FetchBlocked) as e:
        fetchmod._pinned_https_open("evil.example", 443, "/", 1.0)
    assert "public address" in str(e.value)


# ---- redirect without a Location header --------------------------------------


def test_redirect_without_location_blocked():
    op = _opener(status=302, headers={"Content-Type": "text/html"})  # no Location
    with pytest.raises(FetchBlocked) as e:
        fetch("https://example.com/r", opener=op, resolve=_PUB)
    assert "redirect without Location" in str(e.value)


# ---- resolve_public_ip (the pin helper, used by SMTP too) --------------------


def test_resolve_public_ip_picks_public_skips_private(monkeypatch):
    from lib import fetch as fetchmod
    monkeypatch.setattr(fetchmod, "_getaddrinfo_bounded", lambda *a, **k: [
        (2, 1, 6, "", ("10.0.0.1", 443)),   # private — skipped
        (2, 1, 6, "", ("8.8.8.8", 443)),    # public — chosen
    ])
    assert fetchmod.resolve_public_ip("mixed.example") == "8.8.8.8"


def test_resolve_public_ip_none_when_all_private(monkeypatch):
    from lib import fetch as fetchmod
    monkeypatch.setattr(fetchmod, "_getaddrinfo_bounded",
                        lambda *a, **k: [(2, 1, 6, "", ("10.0.0.1", 443))])
    assert fetchmod.resolve_public_ip("internal.example") is None


def test_resolve_public_ip_none_on_resolver_error(monkeypatch):
    from lib import fetch as fetchmod
    def boom(*a, **k):
        raise OSError("nxdomain")
    monkeypatch.setattr(fetchmod, "_getaddrinfo_bounded", boom)
    assert fetchmod.resolve_public_ip("nope.example") is None
