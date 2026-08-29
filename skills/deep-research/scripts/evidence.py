"""Quote normalisation and hashing.

A `recheck` subagent re-reads a citation's page and judges the quote
itself (agents/rechecker.md); this module no longer fetches or parses
anything. What survives is the comparison both sides of that judgement
still have to agree on: normalize() puts a quote and a page fragment into
the same canonical form, sha256_of() is what makes `quote_sha256` a
citation's stable identity, and meaningful_length()/MIN_QUOTE_CHARS is the
evidence bar enforced at gate 1 (schemas/artifact.extract.json,
schemas/citation.json) and again in apply_extract.
"""
import hashlib
import re
import unicodedata

# The shortest span that can count as evidence, measured in characters
# that actually carry content (see meaningful_length).
#
# A citation is only evidence if its quote is specific enough that
# finding it on the page means something: three extract artifacts
# quoting "a" on three registrable domains reached `supported / 0.6` with
# every code gate satisfied, which left the LLM verifier as the only
# thing between a degenerate quote and promotion — inverting this
# system's stated principle that schemas and code do the enforcing.
#
# 8 is chosen against the two ends. Below it sit the spans that match
# essentially any English page and assert nothing: "a", "42", "the",
# "is not". At it and above, a match is a real coincidence rather than an
# inevitability. And it stays under the shortest quotation a real
# extractor has cause to lift — "42ms at p99" is 9 by this measure — so a
# genuinely terse number-plus-unit span survives with a character to
# spare.
MIN_QUOTE_CHARS = 8

_WHITESPACE = re.compile(r"\s+")

# Categories that occupy no visible width: Cf is the format class
# (ZERO WIDTH SPACE U+200B, ZWNJ, ZWJ, WORD JOINER, SOFT HYPHEN, BOM),
# Cc the C0/C1 controls, Zl/Zp the line and paragraph separators. NFKC
# does not fold any of them away and `str.isspace()` is False for every
# Cf character, so without this a quote of eight zero-width spaces is
# eight "non-whitespace characters" — the deferred zero-width item,
# closed here rather than in a second place that could disagree.
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Cc", "Zl", "Zp"})


def normalize(text):
    """The canonical form a quote and the page text a re-check agent reads
    are both put into before either is compared against the other.

    NFKC then whitespace collapse. NFKC is deliberately aggressive: it
    folds the non-breaking space to a space and the `fi` ligature to two
    letters, so a quote lifted from a PDF matches the same words on the
    rendered page. That symmetry is what makes `quote_sha256` a stable
    identity for the same span however it was line-wrapped.
    """
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def meaningful_length(text):
    """How many characters of real content a quote carries.

    NFKC first, for the same reason normalize() applies it: the count has
    to be of the same string a comparison would use. Whitespace and
    zero-width characters are then dropped — see _INVISIBLE_CATEGORIES.

    Compared against MIN_QUOTE_CHARS at gate 1
    (schemas/artifact.extract.json, schemas/citation.json) and again in
    apply_extract, which is the enforcement point now that gate 2 itself
    is a subagent's judgement rather than code; this is the definition
    both share.
    """
    return sum(
        1 for character in unicodedata.normalize("NFKC", text)
        if not character.isspace()
        and unicodedata.category(character) not in _INVISIBLE_CATEGORIES
    )


def sha256_of(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
