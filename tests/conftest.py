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

# Make the skill root importable as both `lib.x` and bare top-level.
SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT))
