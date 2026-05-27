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
    fold_ascii,
    fold_to_letters,
    localpart_templates,
    name_variants,
    normalize_domain,
    normalize_email,
    parse_name,
)


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
