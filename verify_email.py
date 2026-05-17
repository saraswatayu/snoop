#!/usr/bin/env python3
"""
Email verifier -- single OR batch.

SINGLE mode (one positional email): tests one address, prints one JSON
object, exit code mirrors the verdict.

BATCH mode (multiple emails, or --file): tests a ranked list in order.
Groups by domain, does the MX lookup + catch-all sentinel probe ONCE per
domain, reuses ONE SMTP connection per domain, and stops early as soon as
an address is `verified`. Prints one JSON object summarising the run.
This collapses the whole "loop one candidate at a time" into a single
call -- the caller does not loop.

It never sends mail; it only performs SMTP RCPT probes.
Use responsibly and only for legitimate outreach / verification.

Usage:
    python3 verify_email.py "jane.doe@acme.com"
    python3 verify_email.py a@acme.com b@acme.com c@acme.dev
    python3 verify_email.py --file candidates.txt          # one per line
    printf 'a@acme.com\\nb@acme.com\\n' | python3 verify_email.py --file -
    # options: --from you@dom.com  --timeout 8

Exit codes:
    single : 0 verified  1 invalid  2 catch_all  3 inconclusive
             4 bad_syntax 5 no_mx
    batch  : 0 a verified hit was found, else 3
Logs go to stderr; the JSON result is the only thing on stdout.
"""

import argparse
import json
import random
import re
import smtplib
import socket
import string
import sys

try:
    import dns.resolver
except ImportError:
    print("Missing dependency 'dnspython'. Install: pip install dnspython",
          file=sys.stderr)
    sys.exit(3)

SYNTAX_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

VERDICT_EXIT = {
    "verified": 0, "invalid": 1, "catch_all": 2,
    "inconclusive": 3, "bad_syntax": 4, "no_mx": 5,
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def detect_provider(mx_host: str) -> str:
    h = mx_host.lower()
    if "google" in h or "googlemail" in h or "aspmx.l.google.com" in h:
        return "google"
    if "outlook" in h or "microsoft" in h or "protection.outlook.com" in h:
        return "microsoft"
    return "other"


def get_mx(domain: str):
    """Return (mx_host, None) or (None, error_string)."""
    try:
        records = dns.resolver.resolve(domain, "MX")
        if not records:
            return None, "no MX records"
        best = min(records, key=lambda r: r.preference)
        return str(best.exchange).rstrip("."), None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def random_localpart(n: int = 16) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def classify(target_code, catch_all: bool, provider: str):
    """Map an SMTP response code to a verdict + human detail."""
    if catch_all:
        return "catch_all", (
            "Domain accepts mail for non-existent addresses (catch-all); "
            "RCPT cannot confirm this specific mailbox."
        )
    if target_code == 250:
        return "verified", "RCPT accepted on a non-catch-all domain."
    if target_code in (550, 551, 553, 554):
        return "invalid", f"RCPT rejected (SMTP {target_code})."
    note = f"RCPT inconclusive (SMTP {target_code})."
    if provider in ("google", "microsoft"):
        note += f" {provider.title()}-hosted domains commonly block RCPT."
    return "inconclusive", note


class DomainProbe:
    """One MX lookup + one catch-all sentinel + one reused SMTP connection."""

    def __init__(self, domain: str, mail_from: str, timeout: int):
        self.domain = domain
        self.mail_from = mail_from
        self.timeout = timeout
        self.mx = None
        self.provider = None
        self.catch_all = None
        self.error = None
        self._server = None

    def _open(self):
        self._server = smtplib.SMTP(timeout=self.timeout)
        self._server.connect(self.mx)
        self._server.ehlo_or_helo_if_needed()
        self._server.mail(self.mail_from)

    def _connect(self):
        self.mx, err = get_mx(self.domain)
        if self.mx is None:
            self.error = f"No usable MX for {self.domain}: {err}"
            return False
        self.provider = detect_provider(self.mx)
        log(f"[{self.domain}] MX {self.mx} (provider={self.provider})")
        try:
            self._open()
        except (smtplib.SMTPException, socket.error, OSError) as e:
            self.error = f"SMTP connect failed: {e}"
            return False
        # Catch-all sentinel: probe one random non-existent localpart once.
        code = self._rcpt(f"{random_localpart()}@{self.domain}")
        self.catch_all = (code == 250)
        log(f"[{self.domain}] catch_all={self.catch_all}")
        return True

    def _rcpt(self, addr: str):
        try:
            code, _ = self._server.rcpt(addr)
            return code
        except smtplib.SMTPServerDisconnected:
            try:  # reconnect once, retry this address
                self._open()
                code, _ = self._server.rcpt(addr)
                return code
            except Exception:  # noqa: BLE001
                return None
        except Exception:  # noqa: BLE001
            return None

    def test(self, email: str):
        if self._server is None and self.error is None:
            self._connect()
        if self.error:
            return {"email": email, "verdict": "no_mx", "mx": self.mx,
                    "provider": self.provider, "catch_all": None,
                    "smtp_code": None, "detail": self.error}
        code = self._rcpt(email)
        verdict, detail = classify(code, self.catch_all, self.provider)
        return {"email": email, "verdict": verdict, "mx": self.mx,
                "provider": self.provider, "catch_all": self.catch_all,
                "smtp_code": code, "detail": detail}

    def close(self):
        if self._server is not None:
            try:
                self._server.quit()
            except Exception:  # noqa: BLE001
                pass
            self._server = None


def run_single(email: str, mail_from: str, timeout: int) -> int:
    result = {"email": email, "verdict": None, "mx": None, "provider": None,
              "catch_all": None, "smtp_code": None, "detail": None}
    if not SYNTAX_RE.match(email):
        result.update(verdict="bad_syntax", detail="Fails email syntax check.")
        print(json.dumps(result))
        return VERDICT_EXIT["bad_syntax"]
    domain = email.rsplit("@", 1)[1]
    probe = DomainProbe(domain, mail_from, timeout)
    r = probe.test(email)
    probe.close()
    print(json.dumps(r))
    return VERDICT_EXIT.get(r["verdict"], 3)


def run_batch(emails, mail_from: str, timeout: int) -> int:
    """Test a ranked list. Stop globally on the first `verified` hit.
    A catch-all / no-MX domain is marked dead and its remaining
    candidates are skipped (probing them is pointless)."""
    probes = {}
    tested = []
    hit = None
    dead_domains = set()
    for email in emails:
        email = email.strip()
        if not email:
            continue
        if not SYNTAX_RE.match(email):
            tested.append({"email": email, "verdict": "bad_syntax",
                           "detail": "Fails syntax check."})
            continue
        domain = email.rsplit("@", 1)[1]
        if domain in dead_domains:
            tested.append({"email": email, "verdict": "skipped",
                           "detail": f"{domain} already unverifiable; "
                                     "skipped to save probes."})
            continue
        if domain not in probes:
            probes[domain] = DomainProbe(domain, mail_from, timeout)
        r = probes[domain].test(email)
        tested.append(r)
        if r["verdict"] in ("catch_all", "no_mx"):
            dead_domains.add(domain)
        if r["verdict"] == "verified":
            hit = r
            break
    for p in probes.values():
        p.close()

    if hit:
        result = "verified"
    elif any(t["verdict"] == "catch_all" for t in tested):
        result = "catch_all"
    elif any(t["verdict"] == "inconclusive" for t in tested):
        result = "inconclusive"
    else:
        result = "exhausted"

    out = {
        "result": result,
        "hit": hit,
        "tested": tested,
        "summary": (f"{len(tested)} candidate(s) tested; result={result}"
                    + (f"; hit={hit['email']}" if hit else "")),
    }
    print(json.dumps(out, indent=2))
    return 0 if hit else 3


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify email address(es).")
    ap.add_argument("emails", nargs="*", help="One = single mode; many = batch.")
    ap.add_argument("--file", help="Read candidates one-per-line ('-' = stdin).")
    ap.add_argument("--from", dest="mail_from", default="verify@example.com")
    ap.add_argument("--timeout", type=int, default=10)
    args = ap.parse_args()

    emails = list(args.emails)
    if args.file:
        src = sys.stdin if args.file == "-" else open(args.file)
        emails += [ln.strip() for ln in src if ln.strip()
                   and not ln.lstrip().startswith("#")]

    if not emails:
        ap.error("provide at least one email, or --file")

    if len(emails) == 1 and not args.file:
        return run_single(emails[0], args.mail_from, args.timeout)
    return run_batch(emails, args.mail_from, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
