"""lib/work_items.py — the body-of-work resolver.

snoop's profile grows past "an email" into "what has this person built and
published." This resolver produces WorkItem contributions along two paths,
both gated by lib.binding so a namesake's output never gets attributed:

  1. ANCHORED PATH (deterministic, no network): re-shape the recently-pushed
     public repos already fetched into person.gh_recent_repos. These come from
     the validated GitHub identity surface, so a Source(type="github_repo")
     binds "asserted" when the handle is independently bound (>=2 anchors) and
     "possibly" otherwise. Either way it is attributable; only "unbound" drops.

  2. FREE-TEXT SEARCH PATH (SCAFFOLD): a query against a search provider for
     talks, articles, podcasts, and papers. The real provider is BLOCKED on T8
     (search provider / ToS decision), so this path is an injectable scaffold:
     a `search_fn` callable supplies results in tests, and with no `search_fn`
     it is a no-op returning []. The binding gate is the safety guarantee here
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

from .binding import bind_best
from .schema import GitHubRepo, Person, ResolverResult, Source, WorkItem

# Injectable search provider signature. Each result dict has keys:
#   title, url, item_type, published_at, summary, and optionally crosslink_url
# (a URL on the page that links back to the person, e.g. their bound domain).
SearchFn = Callable[[str], list[dict]]

# T8: the real free-text search provider is not wired (blocked on a search
# provider / ToS decision). Surfaced as error_detail so the caller can render
# the capability degradation rather than silently shipping fewer contributions.
_T8_NOT_CONFIGURED = (
    "free-text search not configured (blocked on T8: search provider/ToS decision)"
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
        sources = [
            Source(
                type="github_repo",
                url=repo.html_url,
                observed_at=now,
                detail="recent public repo",
            )
        ]
        binding = bind_best(sources, person)
        if binding.tier == "unbound":
            continue
        items.append(
            WorkItem(
                title=repo.name,
                url=repo.html_url,
                item_type="repo",
                published_at=repo.pushed_at,
                summary=repo.description,
                sources=sources,
                bind_tier=binding.tier,
                bind_reasons=binding.reasons,
            )
        )
    return items


def _crosslink_source(crosslink_url: str, *, now: datetime) -> Source:
    """Represent a result's back-link to the person as a Source.

    The crosslink is what lets a free-text hit bind: if it lands on a bound
    personal domain, lib.binding resolves it to "asserted"/"possibly" and the
    item survives the gate. Typed as "web_search" for simplicity (a feed-host
    refinement to "rss" is a later concern; binding keys on host, not type).
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
    """
    if search_fn is None:
        return []  # real provider not configured (T8)
    now = datetime.now(timezone.utc) if now is None else now
    query = f"{person.name} talk OR article OR podcast OR paper"
    results = search_fn(query) or []

    items: list[WorkItem] = []
    for result in results[:_SEARCH_BUDGET]:
        url = result.get("url")
        sources = [
            Source(
                type="web_search",
                url=url,
                observed_at=now,
                detail="free-text search result",
            )
        ]
        crosslink_url = result.get("crosslink_url")
        if crosslink_url:
            sources.append(_crosslink_source(crosslink_url, now=now))

        binding = bind_best(sources, person)
        if binding.tier == "unbound":
            continue  # namesake gate: no cross-link to a bound signal -> drop

        items.append(
            WorkItem(
                title=result.get("title") or url or "(untitled)",
                url=url,
                item_type=result.get("item_type") or "other",
                published_at=result.get("published_at"),
                summary=result.get("summary"),
                sources=sources,
                bind_tier=binding.tier,
                bind_reasons=binding.reasons,
            )
        )
    return items


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
        error_detail = _T8_NOT_CONFIGURED

    status = "ok" if contributions else "empty"
    return ResolverResult(
        resolver="work_items",
        candidates=[],
        status=status,
        error_detail=error_detail,
        contributions=list(contributions),
    )
