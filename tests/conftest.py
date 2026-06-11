"""Shared pytest fixtures.

Tests are split into two tiers:

- Default (no marker): deterministic, no network. Run in CI, fast.
- `@pytest.mark.network`: hits real services (gh api, WHOIS, HTTP fetches).
  Run with `pytest -m network` locally for integration sanity.

VCR-style recording for HTTP fetches lives next to each resolver's tests:
`tests/fixtures/{resolver}/{fixture_name}.yaml` recorded with `vcrpy` or
hand-authored.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the skill root importable as both `lib.x` and bare top-level.
SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT))


@pytest.fixture(autouse=True)
def _stub_unconditional_network(monkeypatch):
    """Keep the suite network-free. The PGP corroboration (E3) runs in main() on
    every discovered address — it's not one of the mockable fan-out resolvers — so
    default-stub it to an empty result. PGP-specific tests re-monkeypatch
    snoop.fetch_pgp_emails (or call lib.pgp_keyserver directly with a fake fetch)."""
    import snoop
    from lib.schema import ResolverResult
    monkeypatch.setattr(
        snoop, "fetch_pgp_emails",
        lambda emails, **kw: ResolverResult(resolver="pgp", candidates=[], status="empty"),
        raising=False,
    )
