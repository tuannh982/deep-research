import pytest

import latex


def test_an_en_dash_range_survives_as_a_range():
    """The reason this table carries Unicode punctuation at all.

    tectonic 0.17.0 does not mis-render a raw U+2013 under the template's
    own utf8/T1 preamble — it DROPS it, silently. Measured: a body
    containing `range 5–10` typesets as `range 510`. A synthesizer writing
    "5–10% of samples" therefore ships "510% of samples", a wrong number
    in model-written prose with no gate anywhere that would catch it —
    gate 5 sees a sentence carrying a citation and passes it.

    Models emit en-dashes in ranges constantly, so this is the ordinary
    case, not an exotic one."""
    assert latex.escape("5–10% of samples") == "5--10\\% of samples"


@pytest.mark.parametrize("raw, escaped, name", [
    ("—", "---", "em dash"),
    ("–", "--", "en dash"),
    ("“", "``", "left double quote"),
    ("”", "''", "right double quote"),
    ("‘", "`", "left single quote"),
    ("’", "'", "right single quote / apostrophe"),
    ("…", "\\ldots{}", "ellipsis"),
    (" ", "~", "non-breaking space"),
    ("−", "$-$", "minus sign"),
])
def test_escape_converts_punctuation_tectonic_cannot_typeset(raw, escaped,
                                                              name):
    """Each of these is dropped or mangled by tectonic under utf8/T1.

    Every replacement was compiled against real tectonic 0.17.0 before
    being put in the table; `a\\u00a0b` is the one that is not merely
    dropped — it typesets as `aăb`, a visible wrong glyph."""
    assert latex.escape(f"x{raw}y") == f"x{escaped}y", name


def test_the_typographic_apostrophe_is_converted():
    """"don’t" is the single most common instance of this in model prose,
    and it shipped as "dont"."""
    assert latex.escape("don’t") == "don't"


def test_escape_covers_every_latex_special():
    assert latex.escape("100% pure_text with $ & # ~ ^ \\ {braces}") == (
        "100\\% pure\\_text with \\$ \\& \\# \\textasciitilde{} "
        "\\textasciicircum{} \\textbackslash{} \\{braces\\}"
    )


def test_escape_leaves_a_cite_command_intact():
    body = "Costs fell 40% in 2024 \\cite{C-001,C-002}."
    assert latex.escape(body) == "Costs fell 40\\% in 2024 \\cite{C-001,C-002}."


def test_escape_leaves_a_factref_command_intact():
    body = "Margins held \\factref{F-007}."
    assert latex.escape(body) == "Margins held \\factref{F-007}."


def test_unicode_punctuation_is_converted_around_intact_markup():
    """The composition point of the two mechanisms in `escape`, which are
    independent of each other and could each break the other silently.

    The SPECIALS table decides what prose becomes; `_MARKUP` decides which
    spans are exempt from it. Nothing else pins them together: the other
    cite tests use only ASCII `%`, so a regex change that stopped
    excluding `\\cite{}` would still pass them as long as the ids happened
    to contain no `%`. Conversely a `_MARKUP` pattern that swallowed
    neighbouring text would silently stop converting the punctuation
    beside it, and the Unicode tests all use bodies with no markup at all.

    Punctuation sits on BOTH sides of both commands. The comma-separated
    key list is deliberate — the comma and the hyphens inside `C-001` are
    the characters a careless widening of the pattern damages first, and
    a damaged key is a dangling `\\cite` in the PDF and a wrong Appendix D.
    """
    body = ("Scattering—strong at the blue end \\cite{C-001,C-002} covers "
            "5–10% of cases \\factref{F-007}, and Rayleigh called it "
            "“molecular”.")
    assert latex.escape(body) == (
        "Scattering---strong at the blue end \\cite{C-001,C-002} covers "
        "5--10\\% of cases \\factref{F-007}, and Rayleigh called it "
        "``molecular''."
    )
    # Byte-identical survival, asserted separately so a failure says which
    # of the two mechanisms broke rather than just showing a long diff.
    escaped = latex.escape(body)
    assert "\\cite{C-001,C-002}" in escaped
    assert "\\factref{F-007}" in escaped
    assert latex.cite_keys(escaped) == ["C-001", "C-002"]
    assert latex.fact_refs(escaped) == ["F-007"]


def test_escaping_twice_corrupts_the_text():
    """Pinning the constraint, not the behaviour. Escape runs exactly once,
    in apply_synthesize, before the body reaches sections/. If a later task
    ever adds a second call at render time, this is what it produces."""
    assert latex.escape("50%") == "50\\%"
    assert latex.escape(latex.escape("50%")) == "50\\textbackslash{}\\%"


def test_cite_keys_splits_a_comma_list():
    assert latex.cite_keys("a \\cite{C-002,C-001} b") == ["C-001", "C-002"]


def test_cite_keys_are_sorted_and_deduplicated():
    body = "\\cite{C-009} then \\cite{C-002,C-009}"
    assert latex.cite_keys(body) == ["C-002", "C-009"]


def test_cite_keys_of_prose_with_no_citations_is_empty():
    assert latex.cite_keys("no citations at all here") == []


def test_fact_refs_are_parsed_separately_from_cite_keys():
    body = "\\cite{C-001} and \\factref{F-004}"
    assert latex.cite_keys(body) == ["C-001"]
    assert latex.fact_refs(body) == ["F-004"]


def test_unsourced_numerics_flags_a_bare_number():
    body = "The rate rose to 12 percent. Nothing else changed."
    assert latex.unsourced_numerics(body) == ["The rate rose to 12 percent."]


def test_a_cited_sentence_may_carry_numbers():
    assert latex.unsourced_numerics("See \\cite{C-003} for the 2019 figure.") == []


def test_a_factref_also_discharges_a_numeric_claim():
    assert latex.unsourced_numerics("It grew 12% \\factref{F-001}.") == []


def test_only_the_unsourced_sentence_is_flagged():
    """Ensure that when a body has both sourced and unsourced numeric sentences,
    only the unsourced one is returned. This would fail if unsourced_numerics
    returned all sentences or no sentences."""
    body = "The rate rose to 12 percent. See \\cite{C-001} for the 2019 figure."
    assert latex.unsourced_numerics(body) == ["The rate rose to 12 percent."]


def test_prose_with_no_numbers_is_never_flagged():
    assert latex.unsourced_numerics("Margins held steady through the year.") == []
