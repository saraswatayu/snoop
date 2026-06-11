"""Name and domain normalization for /snoop.

Subagent finding F2 (post-review): without an explicit normalization layer,
pattern matching breaks on accented characters ("Étienne Dupont" never
matches "etienne.dupont@..."), Eastern name order produces wrong-direction
pattern guesses, and non-ASCII domains end up with broken MX lookups.

This module is the single funnel. Every downstream module consumes
normalized inputs.

Design choices:

- ASCII-fold via NFKD + diacritic stripping. We pick the dumb folding ("é"→"e",
  "ß"→"ss" via canonical decomposition, "ø"→"o") because the corporate email
  patterns this tool reverse-engineers ARE the dumb folding. If a German user
  has "schroeder" in their email, pattern_gen needs to try both "schroeder"
  AND "schroder" — we generate variants for both directions.

- Name parsing is permissive but predictable. Suffixes (Jr/Sr/III/PhD) and
  middle initials are dropped. Multi-particle surnames ("van der Berg") are
  joined AND space-stripped as separate variants. We do NOT do a hard
  is-this-Eastern-order detection — for any name that has exactly 2 tokens we
  ALSO emit the reversed pairing as a variant, and let the resolver/scorer
  let evidence pick the winner.

- IDNA: domains pass through `idna.encode` if installed, otherwise
  `str.encode('idna')`. Non-ASCII characters in the localpart are NOT handled
  (would require SMTPUTF8 negotiation — out of scope for v1).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


# ---- provider classification -------------------------------------------------

# Personal-provider domains — addresses on these are NEVER work addresses, and
# SMTP RCPT probing them is both useless (they block/throttle RCPT) and a
# spam-filter risk. A sensor-side fact about the domain, not a judgment about
# the person, so it lives in the normalization funnel.
PERSONAL_PROVIDER_DOMAINS = frozenset({
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.co.uk", "ymail.com",
    "icloud.com", "me.com", "mac.com",
    "outlook.com", "hotmail.com", "live.com",
    "protonmail.com", "proton.me", "pm.me",
    "fastmail.com", "fastmail.fm",
    "aol.com", "duck.com", "hey.com",
})


def is_personal_provider(domain: str) -> bool:
    return domain.lower() in PERSONAL_PROVIDER_DOMAINS


# ---- ASCII-folding -----------------------------------------------------------


_GERMAN_FOLDS = {
    "ß": "ss",
    "ä": "ae",  # generated as a separate variant; main fold drops the umlaut
    "ö": "oe",
    "ü": "ue",
    "Ä": "ae",
    "Ö": "oe",
    "Ü": "ue",
}

# Strict NFKD fold: strips ALL combining marks. "ß" doesn't decompose in NFKD,
# so it requires the explicit mapping below.
_EXPLICIT_FOLDS_STRICT = {
    "ß": "ss",
    "Ł": "L",
    "ł": "l",
    "Ø": "O",
    "ø": "o",
    "Æ": "AE",
    "æ": "ae",
    "Œ": "OE",
    "œ": "oe",
    "Đ": "D",
    "đ": "d",
    "Ð": "D",
    "ð": "d",
    "Þ": "Th",
    "þ": "th",
    "ı": "i",
}


def fold_ascii(s: str) -> str:
    """Fold to lowercase ASCII via NFKD + combining-mark strip + explicit overrides.

    Examples:
        "Étienne" → "etienne"
        "Müller" → "muller"
        "Łódź" → "lodz"
        "Peter Steinberger" → "peter steinberger"
    """
    if not s:
        return ""
    out = []
    for ch in s:
        ch = _EXPLICIT_FOLDS_STRICT.get(ch, ch)
        # NFKD decomposition; drop combining marks (Mn category).
        decomposed = unicodedata.normalize("NFKD", ch)
        for d in decomposed:
            if unicodedata.category(d) == "Mn":
                continue
            out.append(d)
    return "".join(out).lower()


def fold_to_letters(s: str) -> str:
    """Aggressive fold: ASCII letters only (drops digits, punctuation, spaces).
    Used to derive `localpart`-shaped strings."""
    return re.sub(r"[^a-z]", "", fold_ascii(s))


# ---- Name parsing ------------------------------------------------------------


_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "phd", "md", "esq", "mba"}
_PREFIXES = {"dr", "mr", "mrs", "ms", "mx", "prof", "sir", "dame"}
_PARTICLES = {
    "van", "von", "de", "der", "del", "della", "di", "da", "do", "dos",
    "la", "le", "el", "al", "bin", "ibn", "ben", "bat", "abu",
    "ó", "mac", "mc", "fitz", "san", "santa",
}


@dataclass(frozen=True)
class NameParts:
    """Permissive name decomposition. `first` and `last` are always present
    when parse_name succeeds; `middle` is whatever's between them; `raw`
    preserves the original string."""
    first: str
    last: str
    middle: str
    raw: str


def parse_name(name: str) -> NameParts | None:
    """Split a human name into (first, middle, last) using common conventions.

    Heuristic, not perfect. Returns None if the input has fewer than 2 tokens.

    Examples:
        "Peter Steinberger" → ("peter", "", "steinberger")
        "Peter L. Steinberger" → ("peter", "l", "steinberger")
        "Peter Steinberger Jr." → ("peter", "", "steinberger")
        "Dr. Peter Steinberger" → ("peter", "", "steinberger")
        "Peter van Halen" → ("peter", "", "van halen")
        "Wang Xiaoming" → ("wang", "", "xiaoming") — see name_variants for
            handling of ambiguous Eastern-order inputs
    """
    if not name or not name.strip():
        return None

    raw = name.strip()
    # Tokenize on whitespace, drop trailing periods, drop empties.
    tokens = [t.rstrip(".") for t in re.split(r"\s+", raw) if t.strip()]
    if len(tokens) < 2:
        return None

    # Drop honorific prefixes ("Dr.", "Prof.", etc.)
    while tokens and fold_ascii(tokens[0]) in _PREFIXES:
        tokens = tokens[1:]
    # Drop generational/title suffixes ("Jr.", "PhD", "III")
    while tokens and fold_ascii(tokens[-1]) in _SUFFIXES:
        tokens = tokens[:-1]

    if len(tokens) < 2:
        return None

    first = tokens[0]
    last_tokens: list[str] = [tokens[-1]]
    # Walk left while we hit lower-case particles (van, de, etc.) — those
    # belong to the surname, not the middle.
    i = len(tokens) - 2
    while i >= 1 and fold_ascii(tokens[i]) in _PARTICLES:
        last_tokens.insert(0, tokens[i])
        i -= 1
    last = " ".join(last_tokens)
    middle = " ".join(tokens[1:i + 1]) if i >= 1 else ""

    return NameParts(first=first, last=last, middle=middle, raw=raw)


# ---- Name variants ----------------------------------------------------------


def name_variants(name: str) -> list[NameParts]:
    """Generate all plausible (first, last) pairings for downstream matching.

    Always includes:
      - ASCII-folded forms of the parsed name
      - Eastern-order reversal when input has exactly 2 tokens
      - Particle-stripped surname (drops "van", "de", etc.)
      - Joined-particle surname ("vanhalen" alongside "van halen")
      - German-style transliterations (ä→ae) as separate forms

    Returns a list of NameParts — duplicates removed (by folded form).
    The first item is always the parsed-as-is form (best canonical guess).
    """
    parsed = parse_name(name)
    if parsed is None:
        return []

    seen: set[tuple[str, str]] = set()
    out: list[NameParts] = []

    def push(first: str, last: str, middle: str = "") -> None:
        key = (fold_to_letters(first), fold_to_letters(last))
        if not key[0] or not key[1] or key in seen:
            return
        seen.add(key)
        out.append(NameParts(first=first, last=last, middle=middle, raw=parsed.raw))

    # 1. Original parse (Western order)
    push(parsed.first, parsed.last, parsed.middle)

    # 2. Eastern order reversal: when the raw input has exactly 2 tokens,
    #    Wang Xiaoming might actually be (Xiaoming, Wang). Emit both pairings.
    tokens = [t for t in re.split(r"\s+", parsed.raw.strip()) if t]
    if len(tokens) == 2:
        push(tokens[1], tokens[0])

    # 3. Particle-stripped surname (drop leading lower-case particles)
    last_tokens = parsed.last.split()
    if len(last_tokens) > 1:
        stripped = [t for t in last_tokens if fold_ascii(t) not in _PARTICLES]
        if stripped:
            push(parsed.first, " ".join(stripped), parsed.middle)

    # 4. Joined-particle surname ("van halen" → "vanhalen")
    if " " in parsed.last:
        push(parsed.first, parsed.last.replace(" ", ""), parsed.middle)

    # 5. German-style transliterations: produce ä→ae, ö→oe, ü→ue variants
    #    alongside the strict NFKD fold.
    def german_fold(s: str) -> str:
        out_chars = []
        for ch in s:
            out_chars.append(_GERMAN_FOLDS.get(ch, ch))
        return fold_ascii("".join(out_chars))
    g_first = german_fold(parsed.first)
    g_last = german_fold(parsed.last)
    if (g_first, g_last) != (fold_ascii(parsed.first), fold_ascii(parsed.last)):
        push(g_first, g_last, parsed.middle)

    return out


# ---- Localpart templates ----------------------------------------------------


def localpart_templates(first: str, last: str) -> dict[str, str]:
    """Apply the common name-to-localpart templates to a folded (first, last) pair.

    Returns a dict of {template_name: localpart}. All localparts are already
    fold_to_letters-clean (lowercase ASCII letters only). The template set
    covers the common corporate formats (first.last, flast, first, etc.).
    """
    f = fold_to_letters(first)
    l = fold_to_letters(last)
    if not f or not l:
        return {}
    fi, li = f[:1], l[:1]
    return {
        "first.last":   f"{f}.{l}",
        "firstlast":    f"{f}{l}",
        "flast":        f"{fi}{l}",
        "firstl":       f"{f}{li}",
        "f.last":       f"{fi}.{l}",
        "first_last":   f"{f}_{l}",
        "first-last":   f"{f}-{l}",
        "last.first":   f"{l}.{f}",
        "lastfirst":    f"{l}{f}",
        "lastf":        f"{l}{fi}",
        "first":        f,
        "fl":           f"{fi}{li}",
    }


# ---- Domain normalization ---------------------------------------------------


def normalize_domain(domain: str) -> str:
    """Lowercase + IDNA-encode a domain. Non-ASCII domains become punycode.

    Examples:
        "Example.COM" → "example.com"
        "münchen.de" → "xn--mnchen-3ya.de"
        "" → ""
    """
    if not domain:
        return ""
    d = domain.strip().lower().rstrip(".")
    if not d:
        return ""
    # Pure-ASCII domains skip IDNA encode (cheap path)
    try:
        d.encode("ascii")
        return d
    except UnicodeEncodeError:
        pass
    # Try the strict idna lib if present (better RFC 5891 compliance), else
    # fall back to stdlib (which uses IDNA 2003).
    try:
        import idna  # type: ignore
        return idna.encode(d).decode("ascii")
    except ImportError:
        pass
    try:
        return d.encode("idna").decode("ascii")
    except UnicodeError:
        # Domain isn't IDNA-encodable (e.g. contains a label with disallowed
        # chars). Return the lowercased original; downstream resolvers will
        # likely fail on it but at least we don't crash.
        return d


def normalize_email(email: str) -> str:
    """Lowercase localpart, IDNA-encode domain. Strips surrounding whitespace.

    Does NOT validate; SYNTAX_RE in verify_smtp is the validator. This is
    purely canonicalization.
    """
    if not email or "@" not in email:
        return (email or "").strip().lower()
    local, _, domain = email.strip().rpartition("@")
    return f"{local.lower()}@{normalize_domain(domain)}"


def name_match(observed_name: str | None, target_name: str) -> bool:
    """Loose equality on names — folded ASCII, drop punctuation, compare
    set-of-tokens to handle "John A. Smith" vs "John Smith".
    """
    if not observed_name or not target_name:
        return False
    obs = parse_name(observed_name)
    tgt = parse_name(target_name)
    if obs and tgt:
        # First+last fold equality (Eastern-order reversal counted as match)
        if fold_ascii(obs.first) == fold_ascii(tgt.first) and \
           fold_ascii(obs.last) == fold_ascii(tgt.last):
            return True
        if fold_ascii(obs.first) == fold_ascii(tgt.last) and \
           fold_ascii(obs.last) == fold_ascii(tgt.first):
            return True
    # Token-set fallback
    obs_tokens = set(fold_ascii(observed_name).split())
    tgt_tokens = set(fold_ascii(target_name).split())
    return bool(obs_tokens) and bool(tgt_tokens) and (
        obs_tokens.issubset(tgt_tokens) or tgt_tokens.issubset(obs_tokens)
    )


# Common org-name suffix tokens, stripped before company matching so
# "OpenAI" matches "OpenAI, Inc.".
ORG_SUFFIX_TOKENS = frozenset({
    "inc", "llc", "ltd", "corp", "co", "company",
    "gmbh", "ag", "sa", "bv", "kg", "ltda", "srl", "spa",
    "lp", "llp", "plc",
})


def employer_match(observed_company: str | None, target_employer_name: str) -> bool:
    """Tolerant company-name match. Companies have variants ('OpenAI' vs
    '@openai' vs 'OpenAI, Inc.'). Compare as token sets after stripping common
    org suffixes — one must be a subset of the other.

    Token-set was chosen over substring containment because `tgt in obs or obs
    in tgt` false-positives in two real ways:
      - plan='Apple' vs observed='Applesauce' → 'apple' in 'applesauce' = True
      - plan='A' vs observed='OpenAI'         → 'a' in 'openai' = True
    Both would falsely bind the github_employer_match anchor (which, with one
    other correct anchor, flips ambiguity to single_plausible_match)."""
    if not observed_company or not target_employer_name:
        return False

    def _tokens(s: str) -> set[str]:
        folded = fold_ascii(s).lstrip("@")
        raw = [t.strip(",.()[]{}\"'") for t in folded.split()]
        return {t for t in raw if t and t not in ORG_SUFFIX_TOKENS}

    obs = _tokens(observed_company)
    tgt = _tokens(target_employer_name)
    if not obs or not tgt:
        return False
    return obs.issubset(tgt) or tgt.issubset(obs)
