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


# Transparent, hand-weighted. NOT a calibrated probability (no labelled
# data) -- a defensible confidence score. Every number here is explainable.
_VERDICT_BASE = {
    "verified": 0.85,
    "catch_all": 0.45,
    "inconclusive": 0.35,
    "invalid": 0.02,
    "no_mx": 0.0,
    "bad_syntax": 0.0,
}


def score_confidence(verdict, catch_all, provider, smtp_code,
                     pattern_matches: int = 0):
    """Return (score 0-1, one-line evidence string) from named signals.

    pattern_matches = how many known company addresses corroborate this
    address's name->localpart format (0 if no pattern evidence).
    """
    base = _VERDICT_BASE.get(verdict, 0.3)
    facts = []

    if verdict == "verified":
        base += 0.05  # clean RCPT accept on a non-catch-all domain
        facts.append("RCPT accepted, non-catch-all")
    elif verdict == "catch_all":
        facts.append("domain is catch-all (mailbox itself unconfirmable)")
    elif verdict == "inconclusive":
        if provider in ("google", "microsoft"):
            facts.append(f"{provider.title()} blocks RCPT verification")
        else:
            facts.append("RCPT inconclusive")
    elif verdict == "invalid":
        facts.append("RCPT rejected")
    elif verdict == "no_mx":
        facts.append("no usable MX")
    elif verdict == "bad_syntax":
        facts.append("invalid syntax")

    if pattern_matches > 0 and verdict not in ("invalid", "no_mx",
                                               "bad_syntax"):
        bonus = min(0.04 * pattern_matches, 0.12)
        base += bonus
        facts.append(f"matches company pattern from {pattern_matches} "
                     f"known address{'es' if pattern_matches > 1 else ''}")

    score = round(max(0.0, min(1.0, base)), 2)
    return score, "; ".join(facts)


# ---- #2: infer a company's name->localpart format from known addresses ----
# Simple and explicit: a fixed set of common templates. If known company
# addresses agree on one, predict the target's address with it and corroborate
# the matching candidate. No provenance plumbing, no probabilistic modelling.

def _norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def _split_name(name: str):
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    if len(parts) < 2:
        return None
    return _norm(parts[0]), _norm(parts[-1])


def _templates(first: str, last: str):
    f, l = first[:1], last[:1]
    return {
        "first.last": f"{first}.{last}",
        "firstlast": f"{first}{last}",
        "flast": f"{f}{last}",
        "firstl": f"{first}{l}",
        "f.last": f"{f}.{last}",
        "first_last": f"{first}_{last}",
        "first-last": f"{first}-{last}",
        "last.first": f"{last}.{first}",
        "lastfirst": f"{last}{first}",
        "lastf": f"{last}{f}",
        "first": f"{first}",
        "fl": f"{f}{l}",
    }


def infer_patterns(localpart: str, first: str, last: str):
    """Template names whose output equals this localpart (may be >1)."""
    if not first or not last:
        return []
    lp = localpart.lower()
    return [name for name, val in _templates(first, last).items() if val == lp]


def predict(template: str, first: str, last: str) -> str:
    return _templates(first, last).get(template, "")


def rank_candidates(emails, known, target_name):
    """Reorder `emails` so addresses matching the company's known pattern
    come first; return (ordered_emails, {email: corroborating_known_count}).

    known: list of (email, "First Last" | None). Only knowns on the SAME
    domain as a candidate, with a parseable name, vote. Tolerant: any bad
    input just yields no reordering.
    """
    tn = _split_name(target_name or "")
    if not tn:
        return list(emails), {}
    tf, tl = tn

    votes = {}  # domain -> {template: count}
    for kemail, kname in known or []:
        kn = _split_name(kname or "")
        if not kn or "@" not in (kemail or ""):
            continue
        klp, kdom = kemail.rsplit("@", 1)
        for t in infer_patterns(klp, *kn):
            votes.setdefault(kdom.lower(), {})
            votes[kdom.lower()][t] = votes[kdom.lower()].get(t, 0) + 1

    match, predicted = {}, []
    for dom in {e.rsplit("@", 1)[1].lower() for e in emails if "@" in e}:
        for t, n in votes.get(dom, {}).items():
            pred = f"{predict(t, tf, tl)}@{dom}"
            predicted.append((n, pred))
            match[pred] = max(match.get(pred, 0), n)

    order, seen = [], set()
    for _, pred in sorted(predicted, key=lambda x: -x[0]):
        if pred not in seen:
            order.append(pred)
            seen.add(pred)
    for e in emails:
        if e not in seen:
            order.append(e)
            seen.add(e)
    return order, match


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

    def test(self, email: str, pattern_matches: int = 0):
        if self._server is None and self.error is None:
            self._connect()
        if self.error:
            score, evidence = score_confidence("no_mx", None,
                                               self.provider, None)
            return {"email": email, "verdict": "no_mx", "mx": self.mx,
                    "provider": self.provider, "catch_all": None,
                    "smtp_code": None, "detail": self.error,
                    "score": score, "evidence": evidence}
        code = self._rcpt(email)
        verdict, detail = classify(code, self.catch_all, self.provider)
        score, evidence = score_confidence(verdict, self.catch_all,
                                           self.provider, code,
                                           pattern_matches)
        return {"email": email, "verdict": verdict, "mx": self.mx,
                "provider": self.provider, "catch_all": self.catch_all,
                "smtp_code": code, "detail": detail,
                "score": score, "evidence": evidence}

    def close(self):
        if self._server is not None:
            try:
                self._server.quit()
            except Exception:  # noqa: BLE001
                pass
            self._server = None


def run_single(email: str, mail_from: str, timeout: int,
               known=None, target=None) -> int:
    result = {"email": email, "verdict": None, "mx": None, "provider": None,
              "catch_all": None, "smtp_code": None, "detail": None,
              "score": None, "evidence": None}
    if not SYNTAX_RE.match(email):
        s, ev = score_confidence("bad_syntax", None, None, None)
        result.update(verdict="bad_syntax", detail="Fails email syntax check.",
                      score=s, evidence=ev)
        print(json.dumps(result))
        return VERDICT_EXIT["bad_syntax"]
    _, match = rank_candidates([email], known, target)
    domain = email.rsplit("@", 1)[1]
    probe = DomainProbe(domain, mail_from, timeout)
    r = probe.test(email, match.get(email, 0))
    probe.close()
    print(json.dumps(r))
    return VERDICT_EXIT.get(r["verdict"], 3)


def run_batch(emails, mail_from: str, timeout: int,
              known=None, target=None) -> int:
    """Test a ranked list. Stop globally on the first `verified` hit.
    A catch-all / no-MX domain is marked dead and its remaining
    candidates are skipped (probing them is pointless)."""
    emails, match = rank_candidates(emails, known, target)
    probes = {}
    tested = []
    hit = None
    dead_domains = set()
    for email in emails:
        email = email.strip()
        if not email:
            continue
        if not SYNTAX_RE.match(email):
            s, ev = score_confidence("bad_syntax", None, None, None)
            tested.append({"email": email, "verdict": "bad_syntax",
                           "detail": "Fails syntax check.",
                           "score": s, "evidence": ev})
            continue
        domain = email.rsplit("@", 1)[1]
        if domain in dead_domains:
            tested.append({"email": email, "verdict": "skipped",
                           "detail": f"{domain} already unverifiable; "
                                     "skipped to save probes.",
                           "score": None, "evidence": None})
            continue
        if domain not in probes:
            probes[domain] = DomainProbe(domain, mail_from, timeout)
        r = probes[domain].test(email, match.get(email, 0))
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
    ap.add_argument("--known", action="append", metavar="EMAIL[=First Last]",
                    help="A known address at the company (repeatable). With "
                         "a name, used to infer the company's email format.")
    ap.add_argument("--for", dest="target", metavar="First Last",
                    help="The person the candidates are for (enables #2 "
                         "pattern inference from --known).")
    args = ap.parse_args()

    known = []
    for k in args.known or []:
        kemail, _, kname = k.partition("=")
        known.append((kemail.strip(), kname.strip() or None))

    emails = list(args.emails)
    if args.file:
        src = sys.stdin if args.file == "-" else open(args.file)
        emails += [ln.strip() for ln in src if ln.strip()
                   and not ln.lstrip().startswith("#")]

    if not emails:
        ap.error("provide at least one email, or --file")

    if len(emails) == 1 and not args.file:
        return run_single(emails[0], args.mail_from, args.timeout,
                          known, args.target)
    return run_batch(emails, args.mail_from, args.timeout,
                     known, args.target)


if __name__ == "__main__":
    sys.exit(main())
