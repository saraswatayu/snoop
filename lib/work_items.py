"""lib/work_items.py — the body-of-work resolver.

snoop's profile grows past "an email" into "what has this person built and
published." This resolver produces WorkItem contributions along two paths,
both gated by lib.binding so a namesake's output never gets attributed:

  1. ANCHORED PATH (deterministic, no network): re-shape the recently-pushed
     public repos already fetched into person.gh_recent_repos. These come from
     the validated GitHub identity surface, so a Source(type="github_repo")
     binds "asserted" when the handle is independently bound (>=2 anchors) and
     "possibly" otherwise. Either way it is attributable; only "unbound" drops.

  2. FREE-TEXT SEARCH PATH: talks, articles, podcasts, papers found by web
     search. The provider (T8) is the HOST MODEL's built-in WebSearch, not a
     bundled scraper or a paid API: the host model runs the searches it already
     does during planning and passes results into snoop via the plan's
     `work_search_results` (snoop.py builds a `search_fn` from them). A
     `search_fn` callable is the injection point (tests pass one directly;
     standalone CLI runs without a host model simply get no results here and
     fall back to anchored sources). The binding gate is the safety guarantee
     (D3): a result is KEPT only when one of its sources cross-links back to a
     bound signal (e.g. a URL on the person's bound personal domain). A bare
     conference page that merely names the person comes back "unbound" and is
     dropped, never attributed. A web_search source can be "possibly" at most,
     never "asserted", which is correct for free-text discovery.

Mirrors lib.gh_profile: Source construction, ResolverResult return, injectable
params, status semantics ("ok" if any contributions else "empty").
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from .binding import bind_and_keep
from .schema import GitHubRepo, Person, ResolverResult, Source, WorkItem

# Injectable search provider signature. Each result dict has keys:
#   title, url, item_type, published_at, summary, and optionally crosslink_url
# (a URL on the page that links back to the person, e.g. their bound domain).
SearchFn = Callable[[str], list[dict]]

# Allowed WorkItem.item_type values. The host model fully controls the
# item_type field on a search result (T8), so it is clamped to this set rather
# than trusted; anything else collapses to "other".
_ITEM_TYPES = frozenset({"repo", "article", "talk", "podcast", "paper", "other"})


def _as_str(value: object) -> str | None:
    """Coerce an untrusted result field to a non-empty str, else None.

    Host-model search results are untrusted input: a field may be missing, a
    non-string (int/dict), or empty. Anything that is not a non-empty string
    becomes None so it never reaches url.strip() (binding._host) or a card line.
    """
    return value if isinstance(value, str) and value.strip() else None

# When search is enabled but no results were supplied this run (no host-model
# WebSearch results in the plan, e.g. a standalone CLI run). Surfaced as
# error_detail so the caller can show the gap rather than silently shipping
# fewer contributions. NOT an error: anchored sources still ran.
_SEARCH_NO_RESULTS = (
    "free-text search: no results supplied this run "
    "(host model passes them via plan.work_search_results; "
    "standalone runs use anchored sources only)"
)

# Cap on free-text results processed per person. Search is unbounded; the
# binding gate drops namesakes, but the budget bounds cost regardless.
_SEARCH_BUDGET = 5


def _anchored_work_items(person: Person, *, now: datetime) -> list[WorkItem]:
    """Re-shape person.gh_recent_repos into bound WorkItems. Drops unbound."""
    items: list[WorkItem] = []
    for repo in person.gh_recent_repos:
        if not isinstance(repo, GitHubRepo):
            continue
        items.append(
            WorkItem(
                title=repo.name,
                url=repo.html_url,
                item_type="repo",
                published_at=repo.pushed_at,
                summary=repo.description,
                sources=[Source(
                    type="github_repo",
                    url=repo.html_url,
                    observed_at=now,
                    detail="recent public repo",
                )],
            )
        )
    return bind_and_keep(items, person)


def _crosslink_source(crosslink_url: str, *, now: datetime) -> Source:
    """Represent a result's back-link to the person as a Source.

    The crosslink is what lets a free-text hit bind: if it lands on a bound
    personal domain, lib.binding lifts it from "unbound" (drop) to "possibly"
    (kept) — but never to "asserted", because snoop never fetched the page to
    verify the link (it is an unverified host-model claim). Typed "web_search"
    so the binding gate applies that cap; binding keys on host AND type here.
    """
    return Source(
        type="web_search",
        url=crosslink_url,
        observed_at=now,
        detail="result cross-links to a bound signal",
    )


def _search_work_items(
    person: Person,
    *,
    search_fn: SearchFn | None = None,
    now: datetime | None = None,
) -> list[WorkItem]:
    """Free-text search path (SCAFFOLD). No-op unless `search_fn` is injected.

    For each result, build a WorkItem with a Source(type="web_search") for the
    result URL, plus an additional cross-link Source when the result carries a
    "crosslink_url". Bind across all the item's sources and DROP any item whose
    tier is "unbound" (the namesake gate). Caps processing at _SEARCH_BUDGET.

    Every field is untrusted host-model input: url/crosslink_url/title/summary/
    published_at are coerced to str-or-None (so a non-string never reaches
    url.strip()), and item_type is clamped to _ITEM_TYPES (fallback "other").
    """
    if search_fn is None:
        return []  # real provider not configured (T8)
    now = datetime.now(timezone.utc) if now is None else now
    query = f"{person.name} talk OR article OR podcast OR paper"
    results = search_fn(query) or []

    items: list[WorkItem] = []
    for result in results[:_SEARCH_BUDGET]:
        if not isinstance(result, dict):
            continue  # untrusted: skip a malformed (non-dict) result
        url = _as_str(result.get("url"))
        sources = [
            Source(
                type="web_search",
                url=url,
                observed_at=now,
                detail="free-text search result",
            )
        ]
        crosslink_url = _as_str(result.get("crosslink_url"))
        if crosslink_url:
            sources.append(_crosslink_source(crosslink_url, now=now))

        item_type = result.get("item_type")
        items.append(
            WorkItem(
                title=_as_str(result.get("title")) or url or "(untitled)",
                url=url,
                item_type=item_type if item_type in _ITEM_TYPES else "other",
                published_at=_as_str(result.get("published_at")),
                summary=_as_str(result.get("summary")),
                sources=sources,
            )
        )
    # Bind across each item's sources; DROP unbound (the namesake gate: a result
    # with no cross-link to a bound signal does not survive).
    return bind_and_keep(items, person)


def collect_work_items(
    person: Person,
    *,
    enable_search: bool = True,
    search_fn: SearchFn | None = None,
    now: datetime | None = None,
) -> ResolverResult:
    """Collect a person's body of work as WorkItem contributions.

    Always runs the anchored path over person.gh_recent_repos. When
    `enable_search`, also runs the free-text search scaffold (a no-op returning
    [] unless `search_fn` is injected). Each path binds and drops unbound items
    internally, so the combined list is attribution-safe.

    Status is "ok" when any contributions survive, else "empty". When
    `enable_search` is on but no `search_fn` is wired, error_detail records the
    T8 capability gap so the renderer can surface the degradation.
    """
    now = datetime.now(timezone.utc) if now is None else now

    contributions: list[WorkItem] = _anchored_work_items(person, now=now)
    if enable_search:
        contributions += _search_work_items(person, search_fn=search_fn, now=now)

    error_detail: str | None = None
    if enable_search and search_fn is None:
        error_detail = _SEARCH_NO_RESULTS

    status = "ok" if contributions else "empty"
    return ResolverResult(
        resolver="work_items",
        candidates=[],
        status=status,
        error_detail=error_detail,
        contributions=list(contributions),
    )
