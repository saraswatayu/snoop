"""lib/verify_smtp.py — SMTP RCPT probing for candidate addresses.

The SMTP RCPT handshake is one verification signal the host model reasons over;
it never sends mail. Two structural choices:

1. **Skip personal-provider domains entirely.** Probing @gmail.com,
   @yahoo.com, @icloud.com, @outlook.com, @hotmail.com, @protonmail.com,
   @proton.me, etc. is useless — mass-market providers either block
   RCPT, return greylist 451 to non-recognized senders, or just
   blackhole the connection — AND it tips spam filters. Those candidates
   carry smtp_verdict="unprobed" and the host model treats SMTP as
   uninformative there.

2. **Inconclusive carries zero information.** RCPT inconclusive on
   Google/M365 (the dominant business inbox in 2026) tells us NOTHING
   about whether the mailbox exists. The observation reports it honestly
   as `smtp=inconclusive`; mx_provider is exposed so the host model can
   explain it ("SMTP inconclusive (M365 blocks RCPT)") and lean on the
   Google account probe instead.

3. **EmailCandidate is the unit, not raw strings.** verify_candidates
   mutates EmailCandidate objects in place, setting smtp_verdict and
   mx_provider; the host model reads those verdicts off the bundle.

The catch-all sentinel + connection-reuse logic in the `DomainProbe`
class is the original mechanism, preserved unchanged — it is correct
and well-tested.

Per-domain daily budget (optional) caps probes to avoid spammy patterns
that get the user's MX denylisted. State persists in JSON under
LAST30DAYS_BUDGET_FILE or ~/.snoop/probe-budget.json.
"""

from __future__ import annotations

import json
import os
import random
import smtplib
import socket
import string
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Literal

from .schema import EmailCandidate
# SYNTAX_RE re-exported from lib.normalize (the single home for the address
# validator) so existing callers/tests can keep importing verify_smtp.SYNTAX_RE.
from .normalize import SYNTAX_RE, is_personal_provider  # noqa: F401
from .fetch import is_public_host, resolve_public_ip

# Bound the MX DNS lookup so a blackholing nameserver can't stall a probe past
# the caller's timeout budget (the smtplib socket below is already bounded, but
# resolution was not).
_DNS_TIMEOUT_SEC = 5.0


def detect_provider(mx_host: str) -> str:
    """Identify the MX provider so the renderer can explain inconclusive."""
    h = (mx_host or "").lower()
    if "google" in h or "googlemail" in h or "aspmx.l.google.com" in h:
        return "google"
    if "outlook" in h or "microsoft" in h or "protection.outlook.com" in h:
        return "microsoft"
    if "yahoodns" in h or "yahoo" in h:
        return "yahoo"
    if "zoho" in h:
        return "zoho"
    if "fastmail" in h:
        return "fastmail"
    if "proton" in h:
        return "proton"
    return "other"


def get_mx(domain: str, *,
           lifetime: float = _DNS_TIMEOUT_SEC) -> tuple[str | None, str | None]:
    """Return (mx_host, None) on success or (None, error_string).

    `lifetime` bounds the total DNS time so a slow/blackholing nameserver
    can't stall the probe indefinitely."""
    try:
        import dns.resolver  # type: ignore
    except ImportError:
        return None, "dnspython not installed (pip install dnspython)"
    try:
        records = dns.resolver.resolve(domain, "MX", lifetime=lifetime)
        if not records:
            return None, "no MX records"
        best = min(records, key=lambda r: r.preference)
        return str(best.exchange).rstrip("."), None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def is_google_hosted(domain: str) -> bool:
    """True when `domain`'s MX is Google Workspace (or the literal google.com).

    Used by snoop.py to auto-add Workspace-hosted domains to the
    --allow-google-account probe set, so the user doesn't need to remember
    --google-workspace-domain for every YC startup on Gmail. Returns False
    on DNS failure or non-Google MX; the caller treats that as "not
    Workspace, don't probe." One DNS lookup per unique candidate domain
    per invocation; no caching beyond what the resolver provides.
    """
    if domain == "google.com":
        return True
    mx_host, err = get_mx(domain)
    if err or not mx_host:
        return False
    return detect_provider(mx_host) == "google"


def _random_localpart(n: int = 16) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


# ---- per-domain daily budget ------------------------------------------------


@dataclass
class ProbeBudget:
    """Tracks per-domain probe counts for the current UTC date. Persisted
    to JSON so multiple invocations on the same day share the count."""
    per_domain: int = 5
    state_path: Path | None = None
    _counts: dict[str, int] | None = None
    _date: str | None = None

    def _load(self) -> None:
        self._counts = {}
        self._date = date.today().isoformat()
        if self.state_path is None:
            return
        try:
            with self.state_path.open("r") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and raw.get("date") == self._date:
                counts = raw.get("counts")
                if isinstance(counts, dict):
                    self._counts = {
                        k: int(v) for k, v in counts.items()
                        if isinstance(k, str) and isinstance(v, int)
                    }
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            pass  # Treat unreadable state as empty

    def _save(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with self.state_path.open("w") as f:
                json.dump({"date": self._date, "counts": self._counts or {}}, f)
            os.chmod(self.state_path, 0o600)
        except OSError:
            pass

    def allow(self, domain: str) -> bool:
        if self._counts is None:
            self._load()
        assert self._counts is not None
        return self._counts.get(domain, 0) < self.per_domain

    def record(self, domain: str) -> None:
        if self._counts is None:
            self._load()
        assert self._counts is not None
        self._counts[domain] = self._counts.get(domain, 0) + 1
        self._save()


# ---- DomainProbe (preserved from legacy with minor refactor) ----------------


class DomainProbe:
    """One MX lookup + one catch-all sentinel probe + one reused SMTP
    connection per domain."""

    def __init__(self, domain: str, mail_from: str, timeout: int):
        self.domain = domain
        self.mail_from = mail_from
        self.timeout = timeout
        self.mx: str | None = None
        self._mx_ip: str | None = None  # the validated public IP we pin the connect to
        self.provider: str | None = None
        self.catch_all: bool | None = None
        self.error: str | None = None
        self._server: smtplib.SMTP | None = None

    def _open(self) -> None:
        self._server = smtplib.SMTP(timeout=self.timeout)
        assert self.mx is not None
        assert self._mx_ip is not None  # pinned by _connect before _open runs
        # Connect to the validated IP, not the name: smtplib.connect would
        # re-resolve a hostname and a rebinding MX could flip it to a private
        # address between the is_public_host check and here. EHLO sends the LOCAL
        # hostname (no server-name dependency), and the probe is plain SMTP (no
        # TLS / cert), so connecting by IP is correct.
        self._server.connect(self._mx_ip)
        self._server.ehlo_or_helo_if_needed()
        self._server.mail(self.mail_from)

    def _connect(self) -> bool:
        self.mx, err = get_mx(self.domain)
        if self.mx is None:
            self.error = f"no usable MX for {self.domain}: {err}"
            return False
        self.provider = detect_provider(self.mx)
        # SSRF guard: the MX host comes from the candidate domain's own DNS
        # (target-influenced). Refuse to open a socket to a private / loopback /
        # link-local / reserved address — that would be an internal port-probe,
        # not a deliverability check. lib.fetch hardens HTTP the same way; SMTP
        # must not be a hole around it.
        if not is_public_host(self.mx):
            self.error = f"MX host not a public address: {self.mx}"
            return False
        # Pin the validated public IP and connect to THAT, not the name. Without
        # this, smtplib.connect(self.mx) re-resolves the hostname, so a rebinding
        # MX could answer public for is_public_host and private for the connect
        # (the HTTP path pins the same way in lib.fetch._pinned_https_open).
        self._mx_ip = resolve_public_ip(self.mx)
        if self._mx_ip is None:
            self.error = f"MX host did not resolve to a public address: {self.mx}"
            return False
        try:
            self._open()
        except (smtplib.SMTPException, socket.error, OSError) as e:
            self.error = f"SMTP connect failed: {e}"
            return False
        sentinel = f"{_random_localpart()}@{self.domain}"
        code = self._rcpt(sentinel)
        self.catch_all = (code == 250)
        return True

    def _rcpt(self, addr: str) -> int | None:
        if self._server is None:
            return None
        try:
            code, _ = self._server.rcpt(addr)
            return code
        except smtplib.SMTPServerDisconnected:
            try:
                self._open()
                if self._server is None:
                    return None
                code, _ = self._server.rcpt(addr)
                return code
            except Exception:  # noqa: BLE001
                return None
        except Exception:  # noqa: BLE001
            return None

    def test(self, email: str) -> tuple[
        Literal["verified", "catch_all", "inconclusive", "invalid", "unprobed"],
        str | None,
        int | None,
    ]:
        """Returns (smtp_verdict, mx_provider, smtp_code)."""
        if self._server is None and self.error is None:
            self._connect()
        if self.error or self._server is None:
            return "unprobed", self.provider, None
        # Catch-all was established by the sentinel during _connect(); a
        # real-address RCPT here would (a) waste a round-trip, (b) log the
        # target address in the recipient MX's mail.log, and (c) tell us
        # nothing new — the domain accepts anything.
        if self.catch_all:
            return "catch_all", self.provider, None
        code = self._rcpt(email)
        if code == 250:
            return "verified", self.provider, code
        if code in (550, 551, 553, 554):
            return "invalid", self.provider, code
        return "inconclusive", self.provider, code

    def close(self) -> None:
        if self._server is not None:
            try:
                self._server.quit()
            except Exception:  # noqa: BLE001
                pass
            self._server = None


# ---- pipeline API -----------------------------------------------------------


_DEFAULT_BUDGET_PATH = Path.home() / ".snoop" / "probe-budget.json"


def verify_candidates(
    candidates: Iterable[EmailCandidate],
    *,
    mail_from: str = "verify@example.com",
    timeout: int = 10,
    skip_personal_providers: bool = True,
    budget: ProbeBudget | None = None,
    _probe_factory: type[DomainProbe] = DomainProbe,
) -> list[EmailCandidate]:
    """Probe SMTP for each candidate, mutating in place to set smtp_verdict
    and mx_provider.

    Args:
        candidates: Iterable of EmailCandidate. Mutated in place.
        mail_from: MAIL FROM address used in the SMTP envelope.
        timeout: Per-domain SMTP socket timeout in seconds.
        skip_personal_providers: If True (default), addresses on Gmail/
            iCloud/Yahoo/M365-consumer/etc. are left with smtp_verdict=
            "unprobed" (the host model treats SMTP as uninformative there).
            Set False to force-probe — useful only for self-hosted-provider
            edge cases.
        budget: Optional per-domain daily probe budget. If provided and
            exhausted for a domain, candidates on that domain are left
            "unprobed."
        _probe_factory: Test seam — pass a fake DomainProbe class.

    Returns:
        The same candidates list, now with smtp_verdict and mx_provider
        populated for everything that was probed.
    """
    probes: dict[str, DomainProbe] = {}
    # When a domain dies (catch-all sentinel returned 250, or MX/SMTP setup
    # failed), record WHY so subsequent candidates on the same domain inherit
    # the right verdict. Without this, the first probed candidate gets
    # "catch_all" and the rest end up "unprobed" — which is technically true
    # ("we did not probe them") but misleading: the user reads two different
    # deliverable scores for addresses on the same catch-all domain.
    dead_domains: dict[str, tuple[Literal["catch_all", "unprobed"], str | None]] = {}
    candidates = list(candidates)

    # Group by domain; iterate stably so the catch-all sentinel runs ONCE
    # per domain across the batch.
    for c in candidates:
        if not c.address:
            # Empty address — cannot evaluate. unprobed (not invalid: invalid
            # implies measured-and-rejected).
            c.smtp_verdict = "unprobed"
            continue
        if "@" not in c.address or not SYNTAX_RE.match(c.address):
            c.smtp_verdict = "invalid"
            continue
        domain = c.address.rsplit("@", 1)[1].lower()

        # Skip personal providers — the scorer downstream knows what to
        # do with an unprobed personal-provider address.
        if skip_personal_providers and is_personal_provider(domain):
            c.smtp_verdict = "unprobed"
            c.is_personal_provider = True
            continue

        if domain in dead_domains:
            inherited_verdict, inherited_provider = dead_domains[domain]
            c.smtp_verdict = inherited_verdict
            c.mx_provider = inherited_provider
            continue

        if budget is not None and not budget.allow(domain):
            c.smtp_verdict = "unprobed"
            continue

        if domain not in probes:
            probes[domain] = _probe_factory(domain, mail_from, timeout)

        verdict, provider, _code = probes[domain].test(c.address)
        c.smtp_verdict = verdict
        c.mx_provider = provider

        if budget is not None:
            budget.record(domain)

        # A catch-all domain or a no-MX/connect-failed domain renders future
        # probes pointless. Record WHY so the next candidate on this domain
        # inherits the right verdict (catch_all propagates; no-MX → unprobed).
        if verdict == "catch_all":
            dead_domains[domain] = ("catch_all", provider)
        elif probes[domain].error is not None:
            dead_domains[domain] = ("unprobed", provider)
        # A verified hit doesn't dead-domain the connection — we may have
        # more candidates on the same domain to probe.

    for p in probes.values():
        p.close()
    return candidates


def default_budget(per_domain: int = 5) -> ProbeBudget:
    """Build a default daily ProbeBudget that persists under ~/.snoop/."""
    return ProbeBudget(per_domain=per_domain, state_path=_DEFAULT_BUDGET_PATH)
