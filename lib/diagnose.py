"""lib/diagnose.py — capability probe.

Subagent F1 raised this from P3 to P1: a resolver silently returning
empty because `gh` isn't authed is a silent killer for the
categorical-improvement claim. The diagnose probe surfaces every
capability the resolvers depend on so the user sees the gap before
running a real lookup.

Output is a structured list of Capability records the CLI / renderer
can present as a table. No formatting decisions here — that's the
renderer's job.

Status semantics:
  - "ok"        : capability is fully functional
  - "degraded"  : works in a reduced mode (e.g. anon HTTP at 60 req/hr
                  instead of authed gh at 5000)
  - "missing"   : capability is not available at all; affected resolvers
                  will return status="unavailable"

Impact strings are concrete: tell the user WHICH resolver this affects.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


Status = Literal["ok", "degraded", "missing"]


@dataclass(frozen=True)
class Capability:
    name: str          # short identifier; renderer uses for sort/grouping
    status: Status
    detail: str        # human-readable observation
    impact: str        # which resolvers / features are affected


# ---- probes ---------------------------------------------------------------


def _probe_gh_cli() -> Capability:
    if shutil.which("gh") is None:
        return Capability(
            name="gh_cli",
            status="missing",
            detail="`gh` CLI not installed",
            impact=(
                "git_emails, gh_profile, person_resolve fall back to "
                "anonymous HTTP at 60 req/hr (enough for one lookup, "
                "tight on retries)"
            ),
        )
    try:
        r = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        return Capability(
            name="gh_cli",
            status="degraded",
            detail=f"`gh` exists but couldn't run: {type(e).__name__}",
            impact="falling back to anonymous HTTP (60 req/hr)",
        )
    if r.returncode == 0:
        return Capability(
            name="gh_cli",
            status="ok",
            detail="`gh` installed and authenticated (5000 req/hr)",
            impact="git_emails, gh_profile, person_resolve at full capacity",
        )
    return Capability(
        name="gh_cli",
        status="degraded",
        detail="`gh` installed but not authenticated — run `gh auth login`",
        impact="falling back to anonymous HTTP (60 req/hr)",
    )


def _probe_anon_github() -> Capability:
    """Last-resort fallback when gh isn't usable."""
    try:
        req = urllib.request.Request(
            "https://api.github.com/zen",
            headers={"User-Agent": "snoop-skill-diagnose"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status == 200:
                return Capability(
                    name="anon_github",
                    status="ok",
                    detail="api.github.com reachable anonymously (60 req/hr ceiling)",
                    impact="GitHub-dependent resolvers can fall back if gh fails",
                )
    except (urllib.error.URLError, OSError) as e:
        return Capability(
            name="anon_github",
            status="missing",
            detail=f"api.github.com unreachable: {type(e).__name__}: {e}",
            impact=(
                "git_emails / gh_profile / person_resolve will fail if "
                "gh CLI also unavailable"
            ),
        )
    return Capability(
        name="anon_github",
        status="degraded",
        detail="api.github.com reachable but returned non-200",
        impact="anon fallback may be flaky",
    )


def _probe_dnspython() -> Capability:
    try:
        import dns.resolver  # type: ignore  # noqa: F401
    except ImportError:
        return Capability(
            name="dnspython",
            status="missing",
            detail="`dnspython` package not installed",
            impact=(
                "verify_smtp cannot resolve MX records — SMTP probing "
                "fully disabled (pip install --user dnspython to fix)"
            ),
        )
    return Capability(
        name="dnspython",
        status="ok",
        detail="`dnspython` installed",
        impact="verify_smtp can resolve MX records",
    )


def _probe_idna() -> Capability:
    try:
        import idna  # type: ignore  # noqa: F401
    except ImportError:
        return Capability(
            name="idna",
            status="degraded",
            detail="`idna` package not installed — stdlib idna fallback in use",
            impact=(
                "normalize_domain uses Python's older IDNA 2003 encoding; "
                "edge-case non-ASCII domains may encode differently than "
                "RFC 5891 strict idna. pip install idna to fix."
            ),
        )
    return Capability(
        name="idna",
        status="ok",
        detail="`idna` package installed (RFC 5891 strict encoding)",
        impact="non-ASCII domains encoded correctly",
    )


def _probe_whois_binary() -> Capability:
    if shutil.which("whois") is None:
        return Capability(
            name="whois",
            status="missing",
            detail="`whois` shell command not installed",
            impact="whois_emails resolver unavailable (P2 source)",
        )
    return Capability(
        name="whois",
        status="ok",
        detail="`whois` command available",
        impact="whois_emails resolver functional (when wired up)",
    )


def _probe_google_account() -> Capability:
    """Check whether the --allow-google-account path is operable.

    Doesn't make a real People API call (that would burn a probe slot in
    the user's daily budget and risks Google flagging the diagnose runs).
    Just confirms cookie acquisition can find a SAPISID-family cookie.
    """
    try:
        from . import chrome_cookies
    except ImportError:
        return Capability(
            name="google_account",
            status="missing",
            detail="lib/chrome_cookies.py not importable",
            impact="--allow-google-account path cannot read browser cookies",
        )

    if not chrome_cookies.list_supported_browsers():
        return Capability(
            name="google_account",
            status="missing",
            detail=f"chrome_cookies: platform {sys.platform!r} not supported "
                   f"(macOS/Linux only in v1)",
            impact="--allow-google-account unavailable on this platform",
        )

    try:
        cookies = chrome_cookies.get_google_cookies()
    except Exception as e:  # noqa: BLE001
        return Capability(
            name="google_account",
            status="degraded",
            detail=f"cookie read errored: {type(e).__name__}: {e}",
            impact="--allow-google-account will fail until the read succeeds",
        )

    if not cookies:
        return Capability(
            name="google_account",
            status="missing",
            detail=(
                "no Google cookies found in any installed Chromium browser "
                "(Chrome, Brave, Edge, Arc, Vivaldi). Sign into Google in "
                "Chrome and retry."
            ),
            impact="--allow-google-account inactive — falls back to other resolvers",
        )

    # Check that a SAPISID-family cookie is present (the load-bearing one).
    sapisid_names = ("SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID")
    has_sapisid = any(cookies.get(n) for n in sapisid_names)
    if not has_sapisid:
        return Capability(
            name="google_account",
            status="degraded",
            detail=(
                f"Google cookies found ({len(cookies)} total) but no SAPISID-"
                f"family cookie among {sapisid_names!r}. Session may be partial; "
                f"sign out and back into Google."
            ),
            impact="--allow-google-account will fail SAPISIDHASH auth",
        )

    return Capability(
        name="google_account",
        status="ok",
        detail=f"Google cookies present ({len(cookies)} cookies); SAPISID auth ready",
        impact="--allow-google-account ready for use",
    )


def _probe_profile_search() -> Capability:
    """The profile's free-text body-of-work search is fed by the HOST MODEL's
    WebSearch results (plan.work_search_results), not a bundled provider. A
    standalone CLI run has no host model, so it uses anchored sources only.
    This is a by-design boundary, not a broken dependency."""
    return Capability(
        name="profile_search",
        status="degraded",
        detail=("free-text body-of-work search runs from host-model WebSearch "
                "results (plan.work_search_results)"),
        impact=(
            "with a host model: talks/podcasts/papers (cross-link-gated) appear. "
            "Standalone CLI: anchored sources only (repos, profile-linked feeds). "
            "Pass --no-search to skip entirely."
        ),
    )


def _probe_snoop_state_dir() -> Capability:
    snoop_dir = Path.home() / ".snoop"
    try:
        snoop_dir.mkdir(parents=True, exist_ok=True)
        # Check we can write a probe file with 0600 perms
        probe = snoop_dir / ".diagnose-probe"
        probe.write_text("ok")
        os.chmod(probe, 0o600)
        mode = os.stat(probe).st_mode & 0o777
        probe.unlink()
        if mode != 0o600:
            return Capability(
                name="snoop_state_dir",
                status="degraded",
                detail=f"{snoop_dir} exists but chmod 600 didn't stick (got {oct(mode)})",
                impact="ProbeBudget and store may write world-readable files",
            )
        return Capability(
            name="snoop_state_dir",
            status="ok",
            detail=f"{snoop_dir} writable with 0600 perms",
            impact="ProbeBudget persistence and (future) store cache OK",
        )
    except OSError as e:
        return Capability(
            name="snoop_state_dir",
            status="missing",
            detail=f"cannot write to {snoop_dir}: {e}",
            impact="ProbeBudget cannot persist between invocations",
        )


# ---- public API -----------------------------------------------------------


def diagnose() -> list[Capability]:
    """Run all capability probes and return the report.

    Order matters for the renderer: most-load-bearing first so degraded
    capacity is visible at the top of the report.
    """
    return [
        _probe_gh_cli(),
        _probe_anon_github(),
        _probe_dnspython(),
        _probe_idna(),
        _probe_whois_binary(),
        _probe_google_account(),
        _probe_profile_search(),
        _probe_snoop_state_dir(),
    ]


def format_report(capabilities: list[Capability]) -> str:
    """Plain-text capability report for the CLI. Not the renderer's
    final output — that's per-lookup. This is a one-shot system check."""
    if not capabilities:
        return "snoop diagnose: no capabilities probed\n"

    lines = ["snoop capability probe", "=" * 22, ""]
    status_glyph = {"ok": "[OK] ", "degraded": "[!! ]", "missing": "[XX ]"}
    for cap in capabilities:
        glyph = status_glyph.get(cap.status, "[? ]")
        lines.append(f"{glyph} {cap.name}: {cap.detail}")
        lines.append(f"      impact: {cap.impact}")
        lines.append("")

    return "\n".join(lines)
