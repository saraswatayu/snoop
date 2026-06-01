"""lib/social_links.py — collect the social profiles a person self-published.

A DETERMINISTIC, NO-NETWORK transform. It reads ONLY fields the person already
declared about themselves (on their validated GitHub profile, or as declared
channel hints) and turns each into a SocialLink contribution. There is NO
inference here: we never guess an unlinked account. If the person did not link
it, it does not appear.

Each link's strength is decided by lib.binding.bind_best over its sources, so a
link from the validated GitHub surface comes back "asserted" while a link merely
declared as a channel hint comes back "possibly". Any link whose binding is
"unbound" is dropped (the caller never renders it).

This mirrors lib/gh_profile.py: it builds Source objects with an injectable
`now` for deterministic tests and returns a ResolverResult with status "ok"
when it produced anything, "empty" otherwise.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .binding import bind_best
from .schema import Person, ResolverResult, SocialLink, Source


# channel_hints keys that carry a self-linked profile URL/handle, mapped to the
# SocialLink.platform they represent. Boolean reachability keys (e.g.
# "x_dms_open") are intentionally absent: those are channels, not links.
_HINT_PLATFORMS: dict[str, str] = {
    "linkedin": "linkedin",
    "bluesky": "bluesky",
    "mastodon": "mastodon",
    "instagram": "instagram",
    "website": "website",
    "calendly": "calendly",
    "x": "x",
    "x_handle": "x",
}


def _looks_like_url(value: str) -> bool:
    """True when the string is an http(s) URL we can record as Source.url."""
    v = value.strip().lower()
    return v.startswith("http://") or v.startswith("https://")


def collect_social_links(
    person: Person, *, now: datetime | None = None
) -> ResolverResult:
    """Collect the social links the person published about themselves.

    Args:
        person: the already-resolved identity. Only its self-declared fields are
            read (handles["github"], gh_twitter, gh_blog, channel_hints).
        now: for deterministic tests; defaults to datetime.now(timezone.utc).

    Returns:
        ResolverResult(resolver="social_links", candidates=[], contributions=[
            SocialLink ...]). status is "ok" when any link survived binding,
        "empty" otherwise. Each SocialLink has bind_tier/bind_reasons set from
        bind_best; "unbound" links are dropped. Links are deduped by
        (platform, lowercased url) and sorted by (platform, url).
    """
    start = datetime.now(timezone.utc) if now is None else now
    links: list[SocialLink] = []

    gh_url = None
    gh_handle = person.handles.get("github")
    if gh_handle:
        gh_url = f"https://github.com/{gh_handle}"
        links.append(SocialLink(
            platform="github",
            url=gh_url,
            handle=gh_handle,
            sources=[Source(
                type="gh_profile", url=gh_url, observed_at=start,
                detail="github profile link",
            )],
        ))

    # twitter_username on the github profile -> an x link. The source is the
    # github profile URL (where we observed the cross-link), so it binds off the
    # validated github surface, not off x.com.
    if person.gh_twitter:
        handle = person.gh_twitter
        links.append(SocialLink(
            platform="x",
            url=f"https://x.com/{handle}",
            handle=handle,
            sources=[Source(
                type="gh_profile", url=gh_url, observed_at=start,
                detail="twitter_username on github profile",
            )],
        ))

    # blog/website declared on the github profile.
    if person.gh_blog:
        links.append(SocialLink(
            platform="website",
            url=person.gh_blog,
            sources=[Source(
                type="gh_profile", url=gh_url, observed_at=start,
                detail="blog on github profile",
            )],
        ))

    # declared channel hints: string values only. Booleans (x_dms_open, etc.)
    # are reachability channels, not links, and are skipped.
    for key, platform in _HINT_PLATFORMS.items():
        value = person.channel_hints.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        links.append(SocialLink(
            platform=platform,
            url=value,
            sources=[Source(
                type="channel_hint",
                url=value if _looks_like_url(value) else None,
                observed_at=start,
                detail=f"channel_hint:{key}",
            )],
        ))

    # bind, drop unbound, dedupe by (platform, lowercased url), sort.
    by_key: dict[tuple[str, str], SocialLink] = {}
    for link in links:
        binding = bind_best(link.sources, person)
        if binding.tier == "unbound":
            continue
        link.bind_tier = binding.tier
        link.bind_reasons = binding.reasons
        key = (link.platform, link.url.lower())
        if key not in by_key:
            by_key[key] = link

    contributions = sorted(by_key.values(), key=lambda s: (s.platform, s.url))
    elapsed = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    return ResolverResult(
        resolver="social_links",
        candidates=[],
        status="ok" if contributions else "empty",
        elapsed_ms=elapsed,
        contributions=contributions,
    )
