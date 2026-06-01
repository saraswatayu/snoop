"""Tests for lib/work_items.py — the body-of-work resolver.

Deterministic, no network. The anchored path re-shapes pre-fetched repos; the
free-text path is exercised through an injected `search_fn`.

The headline guarantee (IRON RULE, inherited from lib.binding D3): a free-text
result that merely names the person, with no cross-link back to a bound signal,
is DROPPED — never attributed. A result whose cross-link lands on a bound
personal domain survives the gate.
"""

from __future__ import annotations

from datetime import datetime, timezone

from lib.schema import GitHubRepo, Person
from lib.work_items import (
    _SEARCH_BUDGET,
    _search_work_items,
    collect_work_items,
)


NOW = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)


def _bound_person(**overrides) -> Person:
    """A person whose GitHub handle is independently bound (>=2 validating
    anchors) AND whose personal domain is cross-link bound."""
    base = dict(
        name="Peter Steinberger",
        handles={"github": "steipete"},
        personal_domains=["steipete.com"],
        bound_anchors=[
            ("github_name_match", "Peter Steinberger"),
            ("github_employer_match", "OpenAI"),
            ("github_personal_domain_match", "steipete.com"),
        ],
        ambiguity="single_plausible_match",
    )
    base.update(overrides)
    return Person(**base)


def _repo(name="steipete/x"):
    return GitHubRepo(
        name=name,
        description="d",
        html_url=f"https://github.com/{name}",
        pushed_at="2026-05-01T00:00:00Z",
    )


def _search_fn(results):
    """An injectable search provider returning a fixed result list."""
    def fn(query):
        return list(results)
    return fn


# ---- anchored path ----------------------------------------------------------


def test_anchored_repo_becomes_asserted_work_item():
    """A recent repo from the validated profile surface, with >=2 binding
    anchors, becomes a repo WorkItem at tier 'asserted'."""
    person = _bound_person(gh_recent_repos=[_repo("steipete/x")])
    result = collect_work_items(person, enable_search=False, now=NOW)

    assert result.status == "ok"
    assert result.resolver == "work_items"
    assert len(result.contributions) == 1
    item = result.contributions[0]
    assert item.kind == "work_item"
    assert item.item_type == "repo"
    assert item.title == "steipete/x"
    assert item.url == "https://github.com/steipete/x"
    assert item.published_at == "2026-05-01T00:00:00Z"
    assert item.summary == "d"
    assert item.bind_tier == "asserted"
    src = item.sources[0]
    assert src.type == "github_repo"
    assert src.detail == "recent public repo"


def test_anchored_repo_only_possibly_when_handle_not_bound():
    """With <2 validating anchors the github_repo source binds 'possibly' (not
    unbound), so the item is still kept, just weaker."""
    person = _bound_person(
        bound_anchors=[("github_name_match", "Peter Steinberger")],
        personal_domains=[],
        gh_recent_repos=[_repo("steipete/x")],
    )
    result = collect_work_items(person, enable_search=False, now=NOW)
    assert len(result.contributions) == 1
    assert result.contributions[0].bind_tier == "possibly"


def test_empty_when_no_repos_and_no_search():
    person = _bound_person(gh_recent_repos=[])
    result = collect_work_items(person, enable_search=False, now=NOW)
    assert result.status == "empty"
    assert result.contributions == []
    assert result.error_detail is None


# ---- search not configured (T8) ---------------------------------------------


def test_enable_search_without_results_yields_only_anchored_and_note():
    """enable_search=True with no search results this run: only anchored items
    appear, and error_detail records that no results were supplied."""
    person = _bound_person(gh_recent_repos=[_repo("steipete/x")])
    result = collect_work_items(person, enable_search=True, search_fn=None, now=NOW)

    assert [i.item_type for i in result.contributions] == ["repo"]
    assert result.error_detail is not None
    assert "no results supplied" in result.error_detail
    assert "work_search_results" in result.error_detail


def test_search_fn_none_path_returns_empty_list_directly():
    person = _bound_person()
    assert _search_work_items(person, search_fn=None, now=NOW) == []


# ---- IRON RULE: namesake gate -----------------------------------------------


def test_namesake_result_without_crosslink_is_dropped():
    """IRON RULE: a free-text hit (e.g. a random conference page) with no
    crosslink_url comes back 'unbound' and is DROPPED, never attributed."""
    person = _bound_person(gh_recent_repos=[])
    results = [{
        "title": "Keynote by some Peter Steinberger",
        "url": "https://randomconf.example/2026/schedule/keynote",
        "item_type": "talk",
        "published_at": "2026-03-01",
        "summary": "A talk at a conference.",
        # no crosslink_url
    }]
    result = collect_work_items(
        person, enable_search=True, search_fn=_search_fn(results), now=NOW
    )
    assert result.contributions == []
    assert result.status == "empty"


def test_search_result_crosslinking_bound_domain_is_kept():
    """A result whose crosslink_url is on the person's bound personal domain
    survives the gate (tier 'asserted' or 'possibly')."""
    person = _bound_person(gh_recent_repos=[])
    results = [{
        "title": "My talk on InterposeKit",
        "url": "https://confvideos.example/watch/abc",
        "item_type": "talk",
        "published_at": "2026-04-10",
        "summary": "Recording of a conference talk.",
        "crosslink_url": "https://steipete.com/talks",
    }]
    result = collect_work_items(
        person, enable_search=True, search_fn=_search_fn(results), now=NOW
    )
    assert len(result.contributions) == 1
    item = result.contributions[0]
    assert item.item_type == "talk"
    assert item.title == "My talk on InterposeKit"
    assert item.bind_tier in ("asserted", "possibly")
    # both the result URL and the crosslink are recorded as sources
    urls = {s.url for s in item.sources}
    assert "https://confvideos.example/watch/abc" in urls
    assert "https://steipete.com/talks" in urls


# ---- search budget ----------------------------------------------------------


def test_search_budget_caps_processed_results_at_five():
    """7 results all cross-linking the bound domain: at most _SEARCH_BUDGET (5)
    are processed; the rest are ignored."""
    person = _bound_person(gh_recent_repos=[])
    results = [
        {
            "title": f"Talk {i}",
            "url": f"https://confvideos.example/watch/{i}",
            "item_type": "talk",
            "published_at": "2026-04-10",
            "summary": "x",
            "crosslink_url": "https://steipete.com/talks",
        }
        for i in range(7)
    ]
    assert _SEARCH_BUDGET == 5
    items = _search_work_items(person, search_fn=_search_fn(results), now=NOW)
    assert len(items) <= _SEARCH_BUDGET
    assert len(items) == 5


def test_search_budget_via_collect_combines_with_anchored():
    """End-to-end: anchored repo + 7 bound search hits => 1 repo + 5 talks."""
    person = _bound_person(gh_recent_repos=[_repo("steipete/x")])
    results = [
        {
            "title": f"Talk {i}",
            "url": f"https://confvideos.example/watch/{i}",
            "item_type": "talk",
            "published_at": "2026-04-10",
            "summary": "x",
            "crosslink_url": "https://steipete.com/talks",
        }
        for i in range(7)
    ]
    result = collect_work_items(
        person, enable_search=True, search_fn=_search_fn(results), now=NOW
    )
    kinds = [i.item_type for i in result.contributions]
    assert kinds.count("repo") == 1
    assert kinds.count("talk") == 5
    assert len(result.contributions) == 6
