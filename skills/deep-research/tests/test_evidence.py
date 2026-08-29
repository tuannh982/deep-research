"""Quote normalisation and hashing. `quote_sha256` is a citation's
identity — spec section 2 requires that a source cited by twelve facts is
stored once, which only holds if the same sentence quoted with different
line wrapping hashes the same — and the quote-length bar (MIN_QUOTE_CHARS)
is still enforced at gate 1 and in apply_extract."""
import evidence


# --- normalize ----------------------------------------------------------

def test_a_ligature_folds_the_same_way_on_both_sides():
    """A quote lifted from a PDF often carries ligatures the HTML does
    not. Symmetric NFKC is what makes those match."""
    assert evidence.normalize("ﬁle") == evidence.normalize("file")


def test_normalize_collapses_every_run_of_whitespace():
    assert evidence.normalize("a \t\n\r\n  b") == "a b"


def test_normalize_strips_the_ends():
    assert evidence.normalize("  \n a \n ") == "a"


def test_normalize_is_idempotent():
    once = evidence.normalize("  a\t\tb  ")
    assert evidence.normalize(once) == once


# --- meaningful_length ---------------------------------------------------

def test_meaningful_length_counts_content_characters():
    assert evidence.meaningful_length("42ms at p99") == 9


def test_meaningful_length_folds_a_ligature_before_counting():
    """Both sides of gate 2 are compared in NFKC form, so the count has
    to be of that same string or the bar and the comparison disagree."""
    assert evidence.meaningful_length("ﬁ") == 2          # ligature -> "fi"


def test_meaningful_length_treats_a_nonbreaking_space_as_whitespace():
    assert evidence.meaningful_length("a b") == 2


def test_meaningful_length_does_not_count_zero_width_characters():
    """The deferred zero-width item: NFKC does not fold U+200B away and
    str.isspace() is False for it, so eight of them are eight
    'non-whitespace characters' by any naive count."""
    assert evidence.meaningful_length("​" * 8) == 0


def test_min_quote_chars_is_a_positive_named_constant():
    """The shortest span that can count as evidence. A judgement call, so
    it is named and testable rather than inline."""
    assert isinstance(evidence.MIN_QUOTE_CHARS, int)
    assert evidence.MIN_QUOTE_CHARS > 0


# --- hashing ----------------------------------------------------------

def test_sha256_of_is_the_standard_hash_of_the_utf8_bytes():
    import hashlib
    assert evidence.sha256_of("abc") == hashlib.sha256(b"abc").hexdigest()


def test_sha256_of_is_64_lowercase_hex():
    import re
    assert re.fullmatch(r"[0-9a-f]{64}", evidence.sha256_of("x"))


def test_sha256_matches_the_citation_schema_pattern():
    """quote_sha256 carries this pattern. A hash this module emits that
    the store rejects would fail at write time, three days in."""
    import json
    import re
    from pathlib import Path
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas" / "citation.json")
        .read_text(encoding="utf-8"))
    pattern = schema["properties"]["quote_sha256"]["pattern"]
    assert re.match(pattern, evidence.sha256_of("café — 日本語"))


def test_different_text_hashes_differently():
    assert evidence.sha256_of("a") != evidence.sha256_of("b")
