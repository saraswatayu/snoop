"""Tests for lib/normalize.py.

Covers the named failure modes from Subagent F2:
- Accented characters (Étienne, Müller, Łódź)
- Eastern name order (Wang Xiaoming)
- IDN domains (münchen.de)
- Multi-particle surnames (van Halen, de la Cruz)
- Suffixes and prefixes (Jr, Sr, PhD, Dr.)
"""

from __future__ import annotations

from lib.normalize import (
    EMAIL_RE,
    PERSONAL_PROVIDER_DOMAINS,
    SYNTAX_RE,
    domain_is_noise,
    employer_match,
    fold_ascii,
    fold_to_letters,
    is_personal_provider,
    localpart_templates,
    name_match,
    name_variants,
    normalize_domain,
    normalize_email,
    parse_name,
)


# ---- shared email primitives ------------------------------------------------


def test_syntax_re_is_anchored_email_re():
    # SYNTAX_RE is the whole-string validator; EMAIL_RE matches anywhere.
    assert SYNTAX_RE.match("jane@acme.io")
    assert not SYNTAX_RE.match("see jane@acme.io now")  # anchored: no surrounding text
    assert EMAIL_RE.search("see jane@acme.io now")      # finds it inline


def test_email_re_quantifiers_are_bounded():
    # The localpart/domain runs are bounded to RFC 5321 limits so finditer over a
    # large no-'@' body stays linear instead of O(n^2) (the ReDoS guard). A
    # 64-char localpart is the RFC max and must still match; 65 must not.
    assert SYNTAX_RE.match("a" * 64 + "@b.co")
    assert SYNTAX_RE.match("a" * 65 + "@b.co") is None
    # A pathological no-'@' blob must not blow up: with an unbounded `+` this scan
    # was O(n^2) (~40s at 200KB); bounded it returns instantly with no matches.
    assert list(EMAIL_RE.finditer("a." * 100_000)) == []


def test_domain_is_noise_covers_reserved_and_subdomains():
    assert domain_is_noise("example.com")
    assert domain_is_noise("mail.example.org")   # subdomain of a reserved domain
    assert domain_is_noise("localhost")
    assert not domain_is_noise("acme.io")
    # sensor-specific extras compose without editing the shared set
    assert domain_is_noise("users.noreply.github.com",
                           extra=("users.noreply.github.com",))
    assert not domain_is_noise("users.noreply.github.com")


# ---- fold_ascii / fold_to_letters --------------------------------------------


def test_fold_ascii_strips_diacritics():
    assert fold_ascii("Étienne Dupont") == "etienne dupont"
    assert fold_ascii("Müller") == "muller"
    assert fold_ascii("Łódź") == "lodz"
    assert fold_ascii("São Paulo") == "sao paulo"


def test_fold_ascii_eszett_to_ss():
    assert fold_ascii("straße") == "strasse"
    assert fold_ascii("Weiß") == "weiss"


def test_fold_ascii_handles_turkish_dotless_i():
    assert fold_ascii("Istanbul") == "istanbul"
    assert fold_ascii("İstanbul") == "istanbul"  # dotted-capital-I → i
    assert fold_ascii("ışık") == "isik"


def test_fold_ascii_empty():
    assert fold_ascii("") == ""
    assert fold_ascii("   ") == "   "  # whitespace is preserved


def test_fold_to_letters_drops_non_letters():
    assert fold_to_letters("Peter Steinberger") == "petersteinberger"
    assert fold_to_letters("O'Brien") == "obrien"
    assert fold_to_letters("Smith-Jones") == "smithjones"
    assert fold_to_letters("Étienne!") == "etienne"


# ---- parse_name --------------------------------------------------------------


def test_parse_name_simple_two_token():
    p = parse_name("Peter Steinberger")
    assert p is not None
    assert p.first == "Peter"
    assert p.last == "Steinberger"
    assert p.middle == ""


def test_parse_name_with_middle_initial():
    p = parse_name("Peter L. Steinberger")
    assert p is not None
    assert p.first == "Peter"
    assert p.last == "Steinberger"
    assert p.middle == "L"


def test_parse_name_drops_suffix():
    for suffix in ("Jr.", "Sr.", "II", "III", "PhD"):
        p = parse_name(f"Peter Steinberger {suffix}")
        assert p is not None
        assert p.last == "Steinberger", f"suffix {suffix} not dropped"


def test_parse_name_drops_prefix():
    p = parse_name("Dr. Peter Steinberger")
    assert p is not None
    assert p.first == "Peter"
    assert p.last == "Steinberger"


def test_parse_name_multi_particle_surname():
    p = parse_name("Peter van der Berg")
    assert p is not None
    assert p.first == "Peter"
    assert p.last == "van der Berg"
    assert p.middle == ""


def test_parse_name_too_short_returns_none():
    assert parse_name("Madonna") is None
    assert parse_name("") is None
    assert parse_name("   ") is None


def test_parse_name_drops_combined_prefix_and_suffix():
    p = parse_name("Dr. Peter Steinberger Jr.")
    assert p is not None
    assert p.first == "Peter"
    assert p.last == "Steinberger"


# ---- name_variants -----------------------------------------------------------


def test_name_variants_emits_eastern_order_for_two_token_input():
    """Wang Xiaoming might be Lastname-First or Firstname-Last; emit both."""
    variants = name_variants("Wang Xiaoming")
    pairs = {(v.first.lower(), v.last.lower()) for v in variants}
    assert ("wang", "xiaoming") in pairs
    assert ("xiaoming", "wang") in pairs


def test_name_variants_does_not_reverse_three_token_names():
    """Peter Lloyd Steinberger should NOT generate (Steinberger, Peter Lloyd) —
    Eastern-order ambiguity only applies to 2-token inputs."""
    variants = name_variants("Peter L Steinberger")
    pairs = {(v.first.lower(), v.last.lower()) for v in variants}
    # First token is always first; last token is always last.
    assert ("peter", "steinberger") in pairs
    assert ("steinberger", "peter") not in pairs


def test_name_variants_emits_particle_stripped_form():
    """For 'Peter van Halen', emit both 'van halen' and 'halen' as variants."""
    variants = name_variants("Peter van Halen")
    last_names = {v.last.lower() for v in variants}
    assert "van halen" in last_names
    assert "halen" in last_names


def test_name_variants_emits_joined_particle_form():
    """For 'Peter van der Berg', emit 'vanderberg' as a variant."""
    variants = name_variants("Peter van der Berg")
    last_names_folded = {fold_to_letters(v.last) for v in variants}
    assert "vanderberg" in last_names_folded


def test_name_variants_emits_german_transliteration():
    """For 'Andreas Schröder', emit BOTH 'schroder' AND 'schroeder' as variants."""
    variants = name_variants("Andreas Schröder")
    last_names_folded = {fold_to_letters(v.last) for v in variants}
    assert "schroder" in last_names_folded   # strict NFKD fold
    assert "schroeder" in last_names_folded  # ö → oe


def test_name_variants_returns_empty_for_invalid():
    assert name_variants("") == []
    assert name_variants("Cher") == []


def test_name_variants_deduplicates_on_folded_form():
    """Variants identical after fold_to_letters shouldn't duplicate."""
    variants = name_variants("PETER STEINBERGER")  # case difference only
    # Should produce 1 canonical + 1 reversed = 2 unique
    folded = {(fold_to_letters(v.first), fold_to_letters(v.last)) for v in variants}
    assert len(folded) == len(variants)


# ---- localpart_templates -----------------------------------------------------


def test_localpart_templates_covers_legacy_set():
    """The template names + outputs must match verify_email.py's legacy
    `_templates` so pattern_gen extraction preserves behavior."""
    templates = localpart_templates("Peter", "Steinberger")
    assert templates["first.last"] == "peter.steinberger"
    assert templates["firstlast"] == "petersteinberger"
    assert templates["flast"] == "psteinberger"
    assert templates["firstl"] == "peters"
    assert templates["f.last"] == "p.steinberger"
    assert templates["first_last"] == "peter_steinberger"
    assert templates["first-last"] == "peter-steinberger"
    assert templates["last.first"] == "steinberger.peter"
    assert templates["lastfirst"] == "steinbergerpeter"
    assert templates["lastf"] == "steinbergerp"
    assert templates["first"] == "peter"
    assert templates["fl"] == "ps"


def test_localpart_templates_handles_accents():
    """'Étienne Müller' should produce 'etienne.muller', not 'étienne.müller'."""
    templates = localpart_templates("Étienne", "Müller")
    assert templates["first.last"] == "etienne.muller"


def test_localpart_templates_empty_input():
    assert localpart_templates("", "Smith") == {}
    assert localpart_templates("John", "") == {}


# ---- normalize_domain --------------------------------------------------------


def test_normalize_domain_lowercases_ascii():
    assert normalize_domain("Example.COM") == "example.com"
    assert normalize_domain("OPENAI.COM") == "openai.com"


def test_normalize_domain_strips_trailing_dot():
    assert normalize_domain("example.com.") == "example.com"


def test_normalize_domain_idna_encodes_non_ascii():
    """München.de must become punycode for MX lookup."""
    result = normalize_domain("münchen.de")
    assert result.startswith("xn--")
    assert result.endswith(".de")


def test_normalize_domain_handles_empty():
    assert normalize_domain("") == ""
    assert normalize_domain("   ") == ""


# ---- normalize_email ---------------------------------------------------------


def test_normalize_email_lowercases_localpart():
    assert normalize_email("Pete.Steinberger@OpenAI.com") == "pete.steinberger@openai.com"


def test_normalize_email_idna_encodes_domain():
    result = normalize_email("peter@münchen.de")
    assert "@xn--" in result


def test_normalize_email_handles_malformed():
    assert normalize_email("not-an-email") == "not-an-email"
    assert normalize_email("") == ""


# ---- is_personal_provider ----------------------------------------------------


def test_is_personal_provider_recognizes_majors():
    assert is_personal_provider("gmail.com")
    assert is_personal_provider("yahoo.com")
    assert is_personal_provider("icloud.com")
    assert is_personal_provider("protonmail.com")
    assert is_personal_provider("outlook.com")


def test_is_personal_provider_case_insensitive():
    assert is_personal_provider("Gmail.COM")


def test_is_personal_provider_rejects_corporate():
    assert not is_personal_provider("openai.com")
    assert not is_personal_provider("acme.dev")


def test_personal_provider_domains_is_frozenset():
    assert isinstance(PERSONAL_PROVIDER_DOMAINS, frozenset)
    assert "gmail.com" in PERSONAL_PROVIDER_DOMAINS


# ---- employer_match (extracted from gh_search / person_resolve) --------------


def test_employer_match_tolerates_suffix_and_handle():
    assert employer_match("OpenAI, Inc.", "OpenAI")
    assert employer_match("@openai", "OpenAI")
    assert employer_match("OpenAI", "OpenAI, LLC")


def test_employer_match_allows_single_token_target_in_richer_observed():
    """A single believed employer token legitimately matches a richer observed
    company string (confirming 'Apple'/'Google' via a fuller profile). Company
    names collide far less than given names, and this anchor can't bind a
    candidate alone, so the lenient subset stays here (unlike name_match)."""
    assert employer_match("Apple Inc, Cupertino", "Apple")
    assert employer_match("OpenAI", "OpenAI")
    assert employer_match("Acme Robotics Inc", "Acme Robotics")
    # genuinely different tokens still don't match
    assert not employer_match("Applesauce Corp", "Apple")


# ---- name_match -------------------------------------------------------------


def test_name_match_first_last_and_middle_initial():
    assert name_match("Peter Steinberger", "Peter Steinberger")
    assert name_match("John A. Smith", "John Smith")
    assert name_match("John Smith", "John A. Smith")


def test_name_match_rejects_bare_first_name():
    """SECURITY: a lone given name must not subset-match a full name and bind a
    namesake's display-name anchor ('John' must not match 'John Smith')."""
    assert not name_match("John", "John Smith")
    assert not name_match("John Smith", "John")


def test_employer_match_rejects_substring_false_positives():
    # the exact false positives the token-set design defends against
    assert not employer_match("Applesauce", "Apple")
    assert not employer_match("OpenAI", "A")


def test_employer_match_handles_empty():
    assert not employer_match(None, "OpenAI")
    assert not employer_match("", "OpenAI")
    assert not employer_match("OpenAI", "")
