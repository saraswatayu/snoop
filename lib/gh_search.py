"""lib/gh_search.py — find a GitHub handle from name + employer hint.

Closes the zero-config CLI gap: when the user types `snoop "Dan Neil"
"Formation Bio"` without --github, this module asks GitHub's user
search API who matches that name and (optionally) company. The handle
flows back into person_resolve where it gets the same anchor-binding
treatment as a plan-provided handle.

Search-then-validate, not search-and-trust. GitHub's /search/users
sorts by best-match score, which is good but not authoritative — a
wrong but popular profile can win. So we:

  1. Search `"{name}" in:fullname type:user` (quoted phrase, exact-fullname).
  2. Fetch the top N candidates' profiles for the actual name/company fields.
  3. Filter to candidates whose profile name matches the target (via the
     same normalize.name_match used elsewhere).
  4. If `employer_name` is supplied, further filter to profiles whose
     company text matches (via the same _employer_match logic person_resolve
     uses for anchor binding).
  5. Return the handle ONLY when exactly one candidate survives. Multiple
     survivors = ambiguity = abstain. Zero survivors = no usable match.

Best-effort: any error (no auth, rate limit, network) returns None. The
rest of the pipeline runs without the github handle the way it did
before this resolver existed.

Cost: 1 search request + up to N profile fetches per invocation. With
N=3 (the default) and gh CLI authed, this is 4 requests against the
5000 req/hr quota — comfortably affordable.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.parse
from typing import Any

from . import _gh_api
from ._gh_api import GhCaller
from .normalize import fold_ascii, name_match


# Reuse the org-suffix token set from person_resolve so company matching
# stays consistent across modules. Imported lazily to avoid a circular
# dependency (person_resolve imports nothing from gh_search).
def _employer_match(observed_company: str | None, target_employer_name: str) -> bool:
    """Same tolerant token-set match person_resolve uses for anchor binding.
    Replicated here (vs. imported) to keep gh_search standalone — it gets
    called from snoop.py's pipeline, not from person_resolve, so there's
    no need to couple them."""
    if not observed_company or not target_employer_name:
        return False
    _ORG_SUFFIX_TOKENS = frozenset({
        "inc", "llc", "ltd", "corp", "co", "company",
        "gmbh", "ag", "sa", "bv", "kg", "ltda", "srl", "spa",
        "lp", "llp", "plc",
    })

    def _tokens(s: str) -> set[str]:
        folded = fold_ascii(s).lstrip("@")
        raw = [t.strip(",.()[]{}\"'") for t in folded.split()]
        return {t for t in raw if t and t not in _ORG_SUFFIX_TOKENS}

    obs = _tokens(observed_company)
    tgt = _tokens(target_employer_name)
    if not obs or not tgt:
        return False
    return obs.issubset(tgt) or tgt.issubset(obs)


def _default_gh_caller() -> GhCaller | None:
    return _gh_api.default_gh_caller()


def find_github_handle(
    name: str,
    employer_name: str | None = None,
    *,
    gh_caller: GhCaller | None = None,
    top_n: int = 3,
) -> str | None:
    """Search GitHub users by name and return a handle if and only if one
    candidate cross-validates against the inputs.

    Args:
        name: Full name to search for. Wrapped in quotes for the GitHub
            search query so partial-word matches don't pollute the top hits.
        employer_name: Optional company hint. When given, profiles whose
            `company` field doesn't tolerantly-match are filtered out.
        gh_caller: Optional injected caller for tests; defaults to gh CLI
            then anonymous HTTP via _gh_api.default_gh_caller().
        top_n: How many top-ranked candidates to fetch and validate.
            Default 3. Larger N means more API budget burned per zero-config
            lookup; smaller N misses people whose name appears as a substring
            of more-popular accounts.

    Returns:
        Handle (str) if exactly one candidate validates. None for: no
        caller available, no name given, search/network error, no hits,
        no candidates matched, or multiple ambiguous matches.
    """
    if not name or not name.strip():
        return None
    caller = gh_caller if gh_caller is not None else _default_gh_caller()
    if caller is None:
        return None

    query = f'"{name.strip()}" in:fullname type:user'
    path = f"/search/users?q={urllib.parse.quote(query)}&per_page={max(1, top_n)}"
    try:
        search_result = caller(path)
    except (subprocess.SubprocessError, urllib.error.URLError,
            json.JSONDecodeError, OSError):
        return None
    if not isinstance(search_result, dict):
        return None
    items = search_result.get("items")
    if not isinstance(items, list) or not items:
        return None

    matches: list[str] = []
    for item in items[:top_n]:
        if not isinstance(item, dict):
            continue
        login = item.get("login")
        if not isinstance(login, str) or not login:
            continue
        # Fetch the profile to get the name/company fields the search
        # response doesn't include
        try:
            profile = caller(f"/users/{login}")
        except (subprocess.SubprocessError, urllib.error.URLError,
                json.JSONDecodeError, OSError):
            continue
        if not isinstance(profile, dict):
            continue

        observed_name = profile.get("name")
        if not name_match(observed_name, name):
            continue

        if employer_name:
            observed_company = profile.get("company")
            if not _employer_match(observed_company, employer_name):
                continue

        matches.append(login)

    # Ambiguous (multiple matches) → abstain rather than guess
    if len(matches) != 1:
        return None
    return matches[0]


def _safe_find_github_handle(
    name: str,
    employer_name: str | None = None,
    *,
    gh_caller: Any = None,
) -> str | None:
    """find_github_handle wrapped in a try/except — when called from inside
    person_resolve, any unexpected error must not break the rest of the
    pipeline. Returns None on anything unexpected, including programmer
    errors elsewhere in the call chain."""
    try:
        return find_github_handle(name, employer_name, gh_caller=gh_caller)
    except Exception:  # noqa: BLE001
        return None
