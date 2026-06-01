"""Tests for lib/social_links.py — self-published social profile collection.

Deterministic and no-network: collect_social_links is a pure transform over an
already-resolved Person, so a fixed `now` is the only injection needed.

The headline guarantees:
  - links from the VALIDATED github surface bind "asserted",
  - links merely declared as channel hints bind "possibly",
  - and NOTHING is inferred: a person with no twitter/handle yields no x/github
    link at all.
"""

from __future__ import annotations

from datetime import datetime, timezone

from lib.schema import Person
from lib.social_links import collect_social_links


def _now():
    return datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


# Two validating anchors == the single_plausible_match threshold, so the github
# handle is bound and profile-derived links assert.
_BOUND_ANCHORS = [
    ("github_name_match", "Peter Steinberger"),
    ("github_employer_match", "OpenAI"),
]


def _person(**overrides):
    base = dict(
        name="Peter Steinberger",
        ambiguity="single_plausible_match",
    )
    base.update(overrides)
    return Person(**base)


def _by_platform(result):
    return {c.platform: c for c in result.contributions}


# ---- validated-profile links are asserted -----------------------------------


def test_github_and_twitter_from_validated_profile_are_asserted():
    person = _person(
        handles={"github": "steipete"},
        gh_twitter="steipete",
        bound_anchors=list(_BOUND_ANCHORS),
    )
    result = collect_social_links(person, now=_now())
    assert result.status == "ok"
    assert result.resolver == "social_links"
    assert result.candidates == []

    by = _by_platform(result)
    assert "github" in by and "x" in by

    gh = by["github"]
    assert gh.url == "https://github.com/steipete"
    assert gh.handle == "steipete"
    assert gh.kind == "social_link"
    assert gh.bind_tier == "asserted"
    assert gh.bind_reasons  # non-empty receipts

    x = by["x"]
    assert x.url == "https://x.com/steipete"
    assert x.handle == "steipete"
    assert x.bind_tier == "asserted"
    # The cross-link was observed ON the github profile.
    assert x.sources[0].type == "gh_profile"
    assert x.sources[0].detail == "twitter_username on github profile"


# ---- channel-hint links are only possibly -----------------------------------


def test_linkedin_channel_hint_is_possibly():
    person = _person(
        channel_hints={"linkedin": "https://linkedin.com/in/steipete"},
    )
    result = collect_social_links(person, now=_now())
    assert result.status == "ok"
    by = _by_platform(result)
    assert "linkedin" in by

    li = by["linkedin"]
    assert li.url == "https://linkedin.com/in/steipete"
    assert li.bind_tier == "possibly"
    src = li.sources[0]
    assert src.type == "channel_hint"
    assert src.detail == "channel_hint:linkedin"
    assert src.url == "https://linkedin.com/in/steipete"  # looked like a url


def test_non_url_channel_hint_records_no_source_url():
    """A handle-shaped hint (not a url) still produces a link, but its Source
    has url=None."""
    person = _person(channel_hints={"bluesky": "@steipete.com"})
    result = collect_social_links(person, now=_now())
    by = _by_platform(result)
    assert "bluesky" in by
    assert by["bluesky"].sources[0].url is None
    assert by["bluesky"].bind_tier == "possibly"


# ---- gh_blog website link ---------------------------------------------------


def test_gh_blog_becomes_a_website_link():
    person = _person(
        handles={"github": "steipete"},
        gh_blog="https://steipete.com",
        bound_anchors=list(_BOUND_ANCHORS),
    )
    result = collect_social_links(person, now=_now())
    by = _by_platform(result)
    assert "website" in by
    assert by["website"].url == "https://steipete.com"
    assert by["website"].sources[0].detail == "blog on github profile"
    assert by["website"].bind_tier == "asserted"


# ---- empty case -------------------------------------------------------------


def test_person_with_no_relevant_fields_is_empty():
    person = _person()  # no handles, no gh_twitter/gh_blog, no channel_hints
    result = collect_social_links(person, now=_now())
    assert result.status == "empty"
    assert result.contributions == []
    assert result.candidates == []


# ---- dedupe -----------------------------------------------------------------


def test_dedupe_same_link_from_two_sources_appears_once():
    """gh_blog AND a website channel_hint can name the same url. The link is
    deduped by (platform, lowercased url) and surfaces once."""
    person = _person(
        handles={"github": "steipete"},
        gh_blog="https://steipete.com",
        channel_hints={"website": "https://STEIPETE.com"},
        bound_anchors=list(_BOUND_ANCHORS),
    )
    result = collect_social_links(person, now=_now())
    website_links = [c for c in result.contributions if c.platform == "website"]
    assert len(website_links) == 1
    # The first contribution (gh_blog, asserted) wins the dedupe slot.
    assert website_links[0].bind_tier == "asserted"


# ---- NO inference -----------------------------------------------------------


def test_no_inference_without_twitter_or_handle():
    """A person with NO github handle and NO gh_twitter must not yield an x or
    github link from thin air."""
    person = _person(
        channel_hints={"linkedin": "https://linkedin.com/in/steipete"},
    )
    result = collect_social_links(person, now=_now())
    platforms = {c.platform for c in result.contributions}
    assert "x" not in platforms
    assert "github" not in platforms
    assert platforms == {"linkedin"}


# ---- determinism: stable sort -----------------------------------------------


def test_contributions_sorted_by_platform_then_url():
    person = _person(
        handles={"github": "steipete"},
        gh_twitter="steipete",
        gh_blog="https://steipete.com",
        bound_anchors=list(_BOUND_ANCHORS),
    )
    result = collect_social_links(person, now=_now())
    platforms = [c.platform for c in result.contributions]
    assert platforms == sorted(platforms)
    assert platforms == ["github", "website", "x"]
