"""lib/rel_me.py — rel="me" bidirectional identity verification.

A person's personal site links out to their profiles with `rel="me"`; a
genuine profile links back the same way. When BOTH directions exist, the two
endpoints have mutually attested "this is the same person" — the IndieAuth /
IndieWeb identity-binding model. It is self-asserted (no third party vouches),
but it has zero ToS surface: we only read public pages the author published,
and we only emit the URLs and platform tokens we parsed, never arbitrary page
text.

Scope v1: personal-site ↔ {Mastodon, Bluesky, GitHub} only.

Two signal tiers:
  - "asserted"  — bidirectional rel="me" confirmed (site→profile AND
                  profile→site), OR a Bluesky domain-handle proven via
                  /.well-known/atproto-did. The strong signal.
  - "possibly"  — only the site→profile direction was found (the backlink
                  fetch 404'd, was blocked, or simply didn't link back). We
                  surface it but don't claim the binding.

Every network read goes through lib.fetch.fetch (SSRF-hardened): https-only,
public-IP-pinned, redirect-revalidated, body-capped. Sub-step failures
(FetchBlocked / OSError) degrade a link to non-bidirectional rather than
raising — verify_rel_me never raises, and returns [] when the site itself
can't be fetched.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Callable

from .fetch import FetchBlocked, FetchResult, fetch as _default_fetch
from .normalize import normalize_domain

_DEFAULT_MAX_LINKS = 6

# Match an HTML tag (<a ...> or <link ...>) that carries BOTH a rel attribute
# whose token list contains "me" and an href. We extract the whole tag first,
# then pull rel + href out of it, so attribute order doesn't matter.
_TAG_RE = re.compile(r"<\s*(a|link)\b([^>]*)>", re.IGNORECASE)
_REL_ATTR_RE = re.compile(r"""\brel\s*=\s*['"]([^'"]*)['"]""", re.IGNORECASE)
_HREF_ATTR_RE = re.compile(r"""\bhref\s*=\s*['"]\s*([^'"\s]+)\s*['"]""", re.IGNORECASE)


@dataclass
class RelMeLink:
    """One rel="me" identity target discovered on a personal site.

    platform:       "mastodon" | "bluesky" | "github" | "other"
    url:            the absolute https profile URL
    bidirectional:  True when the profile links back to the site (or the
                    Bluesky well-known handle is self-attested)
    tier:           "asserted" when bidirectional, "possibly" otherwise
    detail:         optional human-readable note on how it was classified
    """

    platform: str
    url: str
    bidirectional: bool
    tier: str
    detail: str = ""


def _rel_has_me(rel_value: str) -> bool:
    """A rel attribute may be a space-separated token list (e.g.
    rel="me noopener"). True iff one of the tokens is exactly "me"."""
    return "me" in rel_value.lower().split()


def _extract_rel_me_hrefs(html: str) -> list[str]:
    """Pull href values from every <a rel="me"...> and <link rel="me"...>
    tag, in document order, deduplicated. Returns raw hrefs (not yet
    filtered to absolute https)."""
    if not html:
        return []
    seen: dict[str, None] = {}
    for tag in _TAG_RE.finditer(html):
        attrs = tag.group(2)
        rel = _REL_ATTR_RE.search(attrs)
        if not rel or not _rel_has_me(rel.group(1)):
            continue
        href = _HREF_ATTR_RE.search(attrs)
        if not href:
            continue
        raw = href.group(1).strip()
        if raw and raw not in seen:
            seen[raw] = None
    return list(seen.keys())


def _absolute_https(href: str) -> str | None:
    """Return the href iff it is a syntactically valid absolute https URL with
    a host. Relative paths, non-https schemes, and host-less URLs → None.
    Mailto/protocol-relative/fragment hrefs are rejected."""
    try:
        parsed = urllib.parse.urlsplit(href)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    return href


def _classify_platform(host: str) -> str:
    """Classify a target host into a known platform token. Conservative:
    only github.com and Bluesky's app/handle hosts are pinned; everything
    fediverse-shaped is labelled "mastodon" generically; the rest is "other".

    Treating any non-matching host as the fediverse would be wrong, so we only
    claim "mastodon" for hosts that actually look like a Mastodon instance
    (the canonical mastodon.* hosts, or a /@user path is handled by the caller
    via the url, not here). Without a server self-identification step in v1 we
    keep this host-only and default unknown hosts to "other"."""
    host = host.lower()
    if host == "github.com" or host.endswith(".github.com"):
        return "github"
    if host == "bsky.app" or host == "bsky.social" or host.endswith(".bsky.social"):
        return "bluesky"
    if host == "mastodon.social" or host.startswith("mastodon.") or ".mastodon." in host:
        return "mastodon"
    return "other"


def _host_of(url: str) -> str | None:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower() or None
    except ValueError:
        return None


def _links_back_to_domain(html: str, domain: str) -> bool:
    """True iff `html` carries a RECIPROCAL rel="me" link whose host equals
    `domain`. Host comparison is normalized (www. stripped, lowercased).

    Only a rel="me" backlink counts — NOT any href. The IndieAuth /
    IndieWeb model is mutual rel="me" attestation; accepting a plain href would
    let an attacker forge a bidirectional binding whenever the victim's profile
    merely links to the attacker-controlled domain (a bio URL, a pinned-repo
    homepage). The strong "asserted" tier must require the backlink itself to
    declare rel="me"."""
    if not html:
        return False
    target = _bare(domain)
    for href in _extract_rel_me_hrefs(html):
        host = _host_of(href)
        if host and _bare(host) == target:
            return True
    return False


def _bare(host_or_domain: str) -> str:
    """Lowercase + strip a leading www. for tolerant host equality."""
    h = (host_or_domain or "").lower().strip()
    return h[4:] if h.startswith("www.") else h


def _fetch_html(fetch_fn, url: str) -> FetchResult | None:
    """Fetch a URL expecting HTML; return None on any guard/network failure so
    the caller can degrade rather than raise."""
    try:
        return fetch_fn(url)
    except (FetchBlocked, OSError):
        return None


def _check_bluesky_handle(fetch_fn, domain: str) -> RelMeLink | None:
    """A domain can BE a Bluesky handle: bsky resolves it by GETting
    https://<domain>/.well-known/atproto-did, which returns the account's DID
    as a bare string. If that returns 200 with a body starting "did:", the
    domain is a self-attested Bluesky identity. JSON/plaintext both pass the
    default content-type allowlist."""
    url = f"https://{domain}/.well-known/atproto-did"
    try:
        res = fetch_fn(url)
    except (FetchBlocked, OSError):
        return None
    if res is None or res.status != 200:
        return None
    body = (res.text or "").strip()
    if not body.startswith("did:"):
        return None
    return RelMeLink(
        platform="bluesky",
        url=f"https://bsky.app/profile/{domain}",
        bidirectional=True,
        tier="asserted",
        detail="domain is a Bluesky handle (/.well-known/atproto-did)",
    )


def verify_rel_me(
    domain: str,
    *,
    fetch_fn: Callable[..., FetchResult] | None = None,
    max_links: int = _DEFAULT_MAX_LINKS,
) -> list[RelMeLink]:
    """Verify rel="me" identity bindings for a personal `domain`.

    Steps:
      1. Fetch https://<domain>/ and parse rel="me" targets from both
         <link rel="me" href> and <a rel="me" href> (rel may be a token list).
         Keep absolute https URLs only; cap at `max_links`.
      2. Classify each target's platform by host.
      3. Fetch each target; if it links back to <domain> with a reciprocal
         rel="me" link → bidirectional/asserted, else → possibly.
      4. Independently probe the Bluesky domain-handle well-known endpoint.

    Never raises. Returns [] if the site itself can't be fetched. A blocked or
    failing sub-fetch degrades that single link to "possibly" (or skips the
    well-known probe) without aborting the rest.

    `fetch_fn` defaults to lib.fetch.fetch; tests inject a fake routed by URL.
    """
    fetch_fn = fetch_fn or _default_fetch
    domain = normalize_domain(domain)
    if not domain:
        return []

    site = _fetch_html(fetch_fn, f"https://{domain}/")
    if site is None:
        # Total failure to fetch the site — nothing to verify. (The Bluesky
        # well-known probe targets the same host; if the site is unreachable
        # we don't speculatively emit a handle link.)
        return []

    links: list[RelMeLink] = []
    seen_urls: set[str] = set()

    for raw in _extract_rel_me_hrefs(site.text or ""):
        if len(links) >= max_links:
            break
        url = _absolute_https(raw)
        if url is None:
            continue
        host = _host_of(url)
        if host is None:
            continue
        # Don't treat a self-referential rel="me" (site linking to itself) as a
        # profile target.
        if _bare(host) == _bare(domain):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        platform = _classify_platform(host)
        profile = _fetch_html(fetch_fn, url)
        if profile is not None and _links_back_to_domain(profile.text or "", domain):
            links.append(RelMeLink(
                platform=platform,
                url=url,
                bidirectional=True,
                tier="asserted",
                detail="bidirectional rel=me (site and profile link to each other)",
            ))
        else:
            links.append(RelMeLink(
                platform=platform,
                url=url,
                bidirectional=False,
                tier="possibly",
                detail="site links to profile; no back-reference found",
            ))

    # Bluesky domain-handle: independent of the rel="me" link set. Only emit if
    # we didn't already record a bluesky handle for this domain.
    bsky = _check_bluesky_handle(fetch_fn, domain)
    if bsky is not None and bsky.url not in seen_urls:
        links.append(bsky)

    return links
