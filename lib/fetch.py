"""lib/fetch.py — the one SSRF-hardened HTTP GET for sensors that fetch
arbitrary, target-influenced URLs (personal sites, rel="me" targets, keyservers).

These URLs come from the target's own pages — a personal site links to a rel=me
URL; a profile lists a keyserver — i.e. untrusted input. A naive fetch turns that
input into a request the operator's machine makes, so it must never be steerable
into the operator's internal network (SSRF), nor be a denial-of-service vector.

Guards (CEO-plan finding 3A / T3):
  - **HTTPS only.** No http://, file://, ftp://, or anything else.
  - **Public-IP only, pinned.** Resolve the host, reject any private / loopback /
    link-local / reserved / multicast address, then connect to the *resolved IP*
    we validated — not the name. That closes the DNS-rebinding hole where a name
    resolves to a public IP for the check and a private IP for the connect.
  - **Redirects re-validated.** ≤3 hops; every Location is re-checked through the
    same guards (a 30x pointing at 169.254.169.254 is blocked, not followed).
  - **Bounded body.** Streamed read with a hard decompressed-size cap (zip-bomb
    proof) and a content-type allowlist.

Sensors inject `opener=` in tests to avoid the network; the guard logic
(`is_public_host`, scheme/redirect/size checks) is exercised directly.
"""

from __future__ import annotations

import gzip
import http.client
import io
import ipaddress
import socket
import ssl
import urllib.parse
import zlib
from dataclasses import dataclass
from typing import Callable

_DEFAULT_TIMEOUT_SEC = 6.0
_DEFAULT_MAX_BYTES = 2_000_000
_DEFAULT_MAX_REDIRECTS = 3
_DEFAULT_CONTENT_TYPES = frozenset({
    "text/html", "application/xhtml+xml",
    "text/plain", "application/json",
})
_USER_AGENT = "snoop/0.2 (+https://github.com/saraswatayu/snoop)"


class FetchBlocked(Exception):
    """The request was refused by a guard (bad scheme, private host, too many
    redirects, oversize body, disallowed content type). Distinct from a network
    error so a sensor can report it honestly."""


@dataclass
class FetchResult:
    url: str            # final URL after redirects
    status: int
    content_type: str   # lowercased, parameters stripped
    text: str


def is_public_host(host: str | None,
                   *, resolve: Callable[[str], list[str]] | None = None) -> bool:
    """True only if every resolved address for `host` is a public IP. Empty/
    unresolvable/any-private → False (fail closed)."""
    if not host:
        return False
    # A bare IP literal is checked directly (no resolution needed).
    try:
        return _ip_is_public(host)
    except ValueError:
        pass
    resolve = resolve or _default_resolve
    try:
        addrs = resolve(host)
    except OSError:
        return False
    return bool(addrs) and all(_safe_ip_is_public(a) for a in addrs)


def _default_resolve(host: str) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None,
                                                           proto=socket.IPPROTO_TCP)]


def _ip_is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)  # raises ValueError if not an IP literal
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def _safe_ip_is_public(ip: str) -> bool:
    try:
        return _ip_is_public(ip)
    except ValueError:
        return False


# An opener turns a validated (host, port, path) into a (status, headers, body)
# tuple. The default opens a real pinned-IP HTTPS connection; tests inject a fake.
Opener = Callable[[str, int, str, float], "tuple[int, dict, bytes]"]


def fetch(
    url: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SEC,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    max_redirects: int = _DEFAULT_MAX_REDIRECTS,
    allowed_content_types: frozenset[str] = _DEFAULT_CONTENT_TYPES,
    opener: Opener | None = None,
    resolve: Callable[[str], list[str]] | None = None,
) -> FetchResult:
    """GET `url` through the SSRF guards and return a bounded FetchResult.

    Raises FetchBlocked when a guard refuses; lets network/TLS errors (OSError,
    ssl.SSLError) propagate so the caller can mark the sensor degraded."""
    opener = opener or _pinned_https_open
    current = url
    for _hop in range(max_redirects + 1):
        parsed = urllib.parse.urlparse(current)
        if parsed.scheme != "https":
            raise FetchBlocked(f"non-https scheme: {parsed.scheme or '(none)'}")
        host = parsed.hostname
        if not host or not is_public_host(host, resolve=resolve):
            raise FetchBlocked(f"host not a public address: {host}")
        port = parsed.port or 443
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        status, headers, body = opener(host, port, path, timeout)

        if status in (301, 302, 303, 307, 308):
            loc = _header(headers, "location")
            if not loc:
                raise FetchBlocked("redirect without Location")
            current = urllib.parse.urljoin(current, loc)  # re-validated next hop
            continue

        ctype = (_header(headers, "content-type") or "").split(";")[0].strip().lower()
        if allowed_content_types and ctype not in allowed_content_types:
            raise FetchBlocked(f"disallowed content-type: {ctype or '(none)'}")

        text = _decode_capped(body, _header(headers, "content-encoding"), max_bytes)
        return FetchResult(url=current, status=status, content_type=ctype, text=text)

    raise FetchBlocked(f"too many redirects (> {max_redirects})")


def _header(headers: dict, name: str) -> str | None:
    """Case-insensitive header lookup over a plain dict."""
    name = name.lower()
    for k, v in headers.items():
        if k.lower() == name:
            return v
    return None


def _decode_capped(body: bytes, encoding: str | None, max_bytes: int) -> str:
    """Decompress (gzip/deflate) with a hard output cap, then decode as UTF-8
    (replacing errors). The cap is on the DECOMPRESSED size — a small gzip bomb
    can't blow past it."""
    enc = (encoding or "").strip().lower()
    if enc in ("gzip", "x-gzip"):
        raw = _gunzip_capped(body, max_bytes)
    elif enc == "deflate":
        raw = zlib.decompressobj().decompress(body, max_bytes + 1)
    else:
        raw = body
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
    return raw.decode("utf-8", errors="replace")


def _gunzip_capped(body: bytes, max_bytes: int) -> bytes:
    out = bytearray()
    with gzip.GzipFile(fileobj=io.BytesIO(body)) as gz:
        while len(out) <= max_bytes:
            chunk = gz.read(65536)
            if not chunk:
                break
            out.extend(chunk)
    return bytes(out)


def _pinned_https_open(host: str, port: int, path: str,
                       timeout: float) -> tuple[int, dict, bytes]:
    """Open one HTTPS request to the *resolved, validated* IP for `host`, with
    SNI + Host kept as the hostname so TLS and vhosts still work. Reads up to a
    bounded number of bytes off the socket (the precise cap is enforced after
    decompression in _decode_capped)."""
    # Re-resolve and pin a public IP (is_public_host already vetted the name;
    # pin the same address we connect to).
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    pinned = next((i for i in infos if _safe_ip_is_public(str(i[4][0]))), None)
    if pinned is None:
        raise FetchBlocked(f"host not a public address: {host}")
    ip = str(pinned[4][0])

    ctx = ssl.create_default_context()
    raw = socket.create_connection((ip, port), timeout=timeout)
    try:
        sock = ctx.wrap_socket(raw, server_hostname=host)
    except Exception:
        raw.close()
        raise
    try:
        conn = http.client.HTTPSConnection(host, port, timeout=timeout)
        conn.sock = sock
        conn.request("GET", path, headers={
            "Host": host,
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/json,text/plain;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "gzip",
            "Connection": "close",
        })
        resp = conn.getresponse()
        # Read a bounded slab; the decompressed cap is enforced downstream.
        body = resp.read(8 * _DEFAULT_MAX_BYTES)
        headers = {k: v for k, v in resp.getheaders()}
        return resp.status, headers, body
    finally:
        try:
            sock.close()
        except OSError:
            pass
