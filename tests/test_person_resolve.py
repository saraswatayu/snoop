"""Tests for lib/person_resolve.py.

The narrow v1 contract:
- Validate identity anchors from --person-plan (just GitHub in v1)
- Surface plan-vs-observed deltas as Person.notes
- Compute 3-state ambiguity based on ≥2-anchor binding rule
- Don't fabricate handles when none are given
- Defense against host hallucination: handle-exists alone doesn't bind
"""

from __future__ import annotations

from lib.person_resolve import resolve_person


def make_gh(routes):
    """Stub gh_caller. routes maps path-prefix → response or Exception.
    Returns None to simulate 404."""
    def caller(path):
        for key, resp in routes.items():
            if path.startswith(key):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return None  # default: 404
    return caller


# ---- no plan / no handle ----------------------------------------------------


def test_resolve_with_no_plan_returns_basic_person():
    p = resolve_person("Peter Steinberger")
    assert p.name == "Peter Steinberger"
    assert p.handles == {}
    assert p.personal_domains == []
    assert p.employer is None
    assert p.ambiguity == "insufficient_identity_evidence"
    assert any("no identity anchors" in n for n in p.notes)


def test_resolve_generates_name_variants():
    """name_variants must populate even with no plan, for downstream
    pattern_gen / resolver consumption."""
    p = resolve_person("Peter Steinberger")
    assert "Peter Steinberger" in p.name_variants or len(p.name_variants) >= 1


def test_resolve_with_plan_no_handles_abstains_but_keeps_employer():
    """Caller gave employer + personal_domains but no handle.
    The pipeline can still run; resolver flags insufficient evidence."""
    plan = {
        "employer": {"name": "OpenAI", "domains": ["openai.com"]},
        "personal_domains": ["steipete.com"],
    }
    p = resolve_person("Peter Steinberger", plan=plan, gh_caller=make_gh({}))
    assert p.employer is not None and p.employer.name == "OpenAI"
    assert p.employer.domains == ["openai.com"]
    assert p.personal_domains == ["steipete.com"]
    assert p.ambiguity == "insufficient_identity_evidence"
    assert any("no github handle" in n for n in p.notes)


# ---- github handle validation: happy paths ----------------------------------


def test_resolve_binds_when_name_employer_and_blog_all_match():
    """Three independent anchors corroborate the handle → bound."""
    plan = {
        "handles": {"github": "steipete"},
        "employer": {"name": "OpenAI", "domains": ["openai.com"]},
        "personal_domains": ["steipete.com"],
    }
    gh = make_gh({
        "/users/steipete": {
            "login": "steipete",
            "name": "Peter Steinberger",
            "company": "@OpenAI",
            "blog": "https://steipete.com",
        }
    })
    p = resolve_person("Peter Steinberger", plan=plan, gh_caller=gh)
    assert p.ambiguity == "single_plausible_match"
    anchor_types = {a[0] for a in p.bound_anchors}
    assert "github_name_match" in anchor_types
    assert "github_employer_match" in anchor_types
    assert "github_personal_domain_match" in anchor_types
    assert "github_handle_exists" in anchor_types
    # No deltas → no warnings
    assert all("differs" not in n and "do not match" not in n for n in p.notes)


def test_resolve_binds_with_just_name_and_employer_match():
    """Two anchors is enough — name + employer match."""
    plan = {
        "handles": {"github": "steipete"},
        "employer": {"name": "OpenAI", "domains": ["openai.com"]},
    }
    gh = make_gh({
        "/users/steipete": {
            "login": "steipete",
            "name": "Peter Steinberger",
            "company": "OpenAI",
        }
    })
    p = resolve_person("Peter Steinberger", plan=plan, gh_caller=gh)
    assert p.ambiguity == "single_plausible_match"


def test_resolve_handles_company_name_with_corporate_suffix():
    """OpenAI vs OpenAI, Inc. should still match."""
    plan = {
        "handles": {"github": "steipete"},
        "employer": {"name": "OpenAI", "domains": ["openai.com"]},
    }
    gh = make_gh({
        "/users/steipete": {
            "login": "steipete",
            "name": "Peter Steinberger",
            "company": "OpenAI, Inc.",
        }
    })
    p = resolve_person("Peter Steinberger", plan=plan, gh_caller=gh)
    assert p.ambiguity == "single_plausible_match"


# ---- github handle validation: failure / mismatch --------------------------


def test_resolve_flags_404_handle_as_invalid():
    """Plan claims handle, GitHub returns 404 — drop the handle and note it."""
    plan = {
        "handles": {"github": "doesnotexist123abc"},
        "employer": {"name": "Acme", "domains": ["acme.com"]},
    }
    gh = make_gh({})  # everything 404s
    p = resolve_person("Some Person", plan=plan, gh_caller=gh)
    assert "github" not in p.handles
    assert any("404" in n for n in p.notes)
    assert p.ambiguity == "insufficient_identity_evidence"


def test_resolve_surfaces_name_mismatch_as_warning():
    """Plan: 'Peter Steinberger', GitHub handle: 'steipete' but profile
    name is 'Someone Else' — flag the delta, don't bind name anchor."""
    plan = {
        "handles": {"github": "steipete"},
        "employer": {"name": "OpenAI", "domains": ["openai.com"]},
    }
    gh = make_gh({
        "/users/steipete": {
            "login": "steipete",
            "name": "Someone Else",
            "company": "OpenAI",
        }
    })
    p = resolve_person("Peter Steinberger", plan=plan, gh_caller=gh)
    anchor_types = {a[0] for a in p.bound_anchors}
    assert "github_name_match" not in anchor_types
    assert "github_employer_match" in anchor_types
    assert any("do not match" in n.lower() for n in p.notes)
    # Only 1 validating anchor → insufficient
    assert p.ambiguity == "insufficient_identity_evidence"


def test_resolve_surfaces_employer_mismatch_as_warning():
    """Plan: OpenAI. GitHub: Anthropic. Surface the delta."""
    plan = {
        "handles": {"github": "steipete"},
        "employer": {"name": "OpenAI", "domains": ["openai.com"]},
    }
    gh = make_gh({
        "/users/steipete": {
            "login": "steipete",
            "name": "Peter Steinberger",
            "company": "Anthropic",
        }
    })
    p = resolve_person("Peter Steinberger", plan=plan, gh_caller=gh)
    anchor_types = {a[0] for a in p.bound_anchors}
    assert "github_name_match" in anchor_types
    assert "github_employer_match" not in anchor_types
    assert any("Anthropic" in n for n in p.notes)
    # Only 1 validating anchor → insufficient
    assert p.ambiguity == "insufficient_identity_evidence"


def test_resolve_handle_exists_but_nothing_matches_is_insufficient():
    """Codex c5 evidence laundering defense: a handle that 200s but
    matches NONE of name/employer/domain must NOT bind. The handle
    might be a hallucinated string from the host model."""
    plan = {
        "handles": {"github": "innocentbystander"},
        "employer": {"name": "OpenAI", "domains": ["openai.com"]},
    }
    gh = make_gh({
        "/users/innocentbystander": {
            "login": "innocentbystander",
            "name": "Unrelated Person",
            "company": "Some Other Co",
        }
    })
    p = resolve_person("Peter Steinberger", plan=plan, gh_caller=gh)
    assert p.ambiguity == "insufficient_identity_evidence"
    # The handle is still recorded, but bound_anchors has only the trivial
    # "handle exists" entry, no validating anchors.
    validating = [a for a in p.bound_anchors if a[0] != "github_handle_exists"]
    assert validating == []
    assert any("nothing in the profile matches" in n for n in p.notes)


def test_resolve_handles_gh_api_error_gracefully():
    """If gh api blows up, don't crash — note the failure and continue."""
    import subprocess
    def bombs(path):
        raise subprocess.SubprocessError("simulated")
    plan = {"handles": {"github": "steipete"}}
    p = resolve_person("Peter Steinberger", plan=plan, gh_caller=bombs)
    assert any("could not validate" in n for n in p.notes)


# ---- ambiguity_candidates from plan -----------------------------------------


def test_resolve_respects_caller_multiple_matches():
    """If the host model says 'there are multiple Matt Smiths,' surface
    that even when individual anchors might otherwise bind."""
    plan = {
        "handles": {"github": "msmith1"},
        "employer": {"name": "Acme", "domains": ["acme.com"]},
        "ambiguity_candidates": [
            {"name": "Matt L. Smith"},
            {"name": "Matt R. Smith"},
        ],
    }
    gh = make_gh({
        "/users/msmith1": {
            "login": "msmith1",
            "name": "Matt Smith",
            "company": "Acme",
        }
    })
    p = resolve_person("Matt Smith", plan=plan, gh_caller=gh)
    assert p.ambiguity == "multiple_plausible_matches"


# ---- input sanitization -----------------------------------------------------


def test_resolve_drops_empty_handles():
    plan = {"handles": {"github": "", "x": "   ", "hn": "real"}}
    p = resolve_person("X", plan=plan, gh_caller=make_gh({}))
    assert "github" not in p.handles
    assert "x" not in p.handles
    assert p.handles.get("hn") == "real"


def test_resolve_normalizes_personal_domains():
    """Domains should be lowercased + IDNA-encoded via normalize_domain."""
    plan = {"personal_domains": ["Steipete.COM", "münchen.de", "   "]}
    p = resolve_person("X", plan=plan, gh_caller=make_gh({}))
    assert "steipete.com" in p.personal_domains
    assert any(d.startswith("xn--") for d in p.personal_domains)
    assert "" not in p.personal_domains
    assert "   " not in p.personal_domains


def test_resolve_keeps_former_employers():
    plan = {
        "former_employers": [
            {"name": "PSPDFKit", "domains": ["pspdfkit.com"], "until": "2023"},
        ],
    }
    p = resolve_person("Peter Steinberger", plan=plan, gh_caller=make_gh({}))
    assert len(p.former_employers) == 1
    assert p.former_employers[0].name == "PSPDFKit"
    assert p.former_employers[0].until == "2023"


def test_employer_match_rejects_partial_word_substring():
    """Defense against substring false-positives. plan='Apple' vs profile
    company='Applesauce Corp' previously bound github_employer_match by
    substring containment ('apple' in 'applesauce' = True). With one other
    correct anchor, that flipped ambiguity to single_plausible_match
    incorrectly."""
    from lib.person_resolve import _employer_match
    assert not _employer_match("Applesauce Corp", "Apple")
    assert not _employer_match("Apple", "Applesauce Corp")
    # Real positives still work
    assert _employer_match("Apple Inc, Cupertino", "Apple")
    assert _employer_match("@OpenAI", "OpenAI")
    assert _employer_match("OpenAI, Inc.", "OpenAI")


def test_employer_match_rejects_one_letter_observation():
    """A single-letter observed company shouldn't false-match a longer
    target. 'A' in 'OpenAI' substring-matched previously."""
    from lib.person_resolve import _employer_match
    assert not _employer_match("A", "OpenAI")
    assert not _employer_match("OpenAI", "A")


def test_resolve_passes_channel_hints_through():
    plan = {"channel_hints": {"x_dms_open": True, "prefers": "x"}}
    p = resolve_person("X", plan=plan, gh_caller=make_gh({}))
    assert p.channel_hints == {"x_dms_open": True, "prefers": "x"}


# ---- gh_search fallback (zero-config CLI path) ------------------------------


def test_resolve_invokes_gh_search_when_no_handle_in_plan():
    """No github handle in the plan → person_resolve calls gh_search and
    threads the discovered handle through the normal validation path."""
    plan = {"employer": {"name": "Formation Bio", "domains": ["formation.bio"]}}
    # Caller serves both the search query and the profile fetch (validation
    # in gh_search) AND the subsequent fetch person_resolve does for anchors.
    routes = {
        "/search/users": {
            "items": [{"login": "danielneil"}],
        },
        "/users/danielneil": {
            "login": "danielneil",
            "name": "Daniel Neil",
            "company": "Formation Bio",
        },
    }
    p = resolve_person("Daniel Neil", plan=plan, gh_caller=make_gh(routes))
    assert p.handles.get("github") == "danielneil"
    assert any("user search" in n for n in p.notes)
    # Anchor binding still applies — name and employer both matched, so this
    # should resolve to single_plausible_match.
    assert p.ambiguity == "single_plausible_match"


def test_resolve_skips_gh_search_when_handle_already_in_plan():
    """Don't call search when the plan already names a handle — search
    would re-fetch the same profile for no benefit."""
    plan = {
        "handles": {"github": "steipete"},
        "employer": {"name": "OpenAI", "domains": ["openai.com"]},
    }
    routes = {
        "/users/steipete": {
            "login": "steipete",
            "name": "Peter Steinberger",
            "company": "OpenAI",
        },
        # If gh_search were invoked, it would try /search/users — this raises
        # to prove it wasn't.
        "/search/users": AssertionError("gh_search should not have been called"),
    }
    p = resolve_person("Peter Steinberger", plan=plan, gh_caller=make_gh(routes))
    assert p.handles.get("github") == "steipete"


def test_resolve_gh_search_failure_doesnt_break_pipeline():
    """gh_search erroring out (network, rate limit, etc.) must not affect
    the rest of person_resolve. The person comes back with no handle,
    pipeline continues with pattern_gen / personal_site."""
    import subprocess

    def caller(path):
        if path.startswith("/search/users"):
            raise subprocess.SubprocessError("rate limited")
        return None

    plan = {"employer": {"name": "Acme", "domains": ["acme.com"]}}
    p = resolve_person("Someone Random", plan=plan, gh_caller=caller)
    assert "github" not in p.handles
    # Ambiguity reflects the no-handle reality
    assert p.ambiguity == "insufficient_identity_evidence"


def test_resolve_gh_search_skipped_when_search_finds_ambiguous():
    """gh_search returns None on ambiguity → person_resolve gets no handle,
    falls through to no-handle ambiguity state."""
    routes = {
        "/search/users": {
            "items": [{"login": "first"}, {"login": "second"}],
        },
        # Both candidates pass name+employer match — search abstains
        "/users/first": {"name": "John Smith", "company": "Acme"},
        "/users/second": {"name": "John Smith", "company": "Acme"},
    }
    plan = {"employer": {"name": "Acme", "domains": ["acme.com"]}}
    p = resolve_person("John Smith", plan=plan, gh_caller=make_gh(routes))
    assert "github" not in p.handles
