"""LaTeX escaping and markup parsing. Pure string functions.

Spec section 7 names unescaped `%`, `&`, `_`, `#`, `$`, `~`, `^` and `\\`
in quoted source text as the largest single cause of failed builds, and
rules out asking a model to escape reliably. So every span of
model-produced prose passes through `escape` exactly once, in
apply_synthesize, before it is written to sections/.

Exactly once matters. `escape` is not idempotent and cannot be — a
correct escaper must turn a literal backslash into
`\\textbackslash{}`, so running it over its own output escapes the
backslash it just introduced. `50%` becomes `50\\%` and then
`50\\textbackslash{}\\%`. test_escaping_twice_corrupts_the_text pins
that, so a later change that adds a second call at render time fails a
test instead of producing a subtly wrong PDF.
"""
import re

# `\` must be in this table, and a single-pass regex is what makes that
# safe: re.sub scans the ORIGINAL string, so the backslash inside a
# replacement is never revisited. Escaping character-by-character in a
# loop — the obvious implementation — turns `%` into `\%` and then into
# `\textbackslash{}%` on the backslash pass.
#
# That same property is what lets the Unicode block below emit
# `\ldots{}` and `$-$`: the backslash, braces and dollars they introduce
# are output, never input, so they are not re-escaped. It also relies on
# `escape` passing a CALLABLE to re.sub — a callable's return value is
# used literally, where a string replacement would treat `\l` as a
# backreference escape.
SPECIALS = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "%": r"\%",
    "&": r"\&",
    "_": r"\_",
    "#": r"\#",
    "$": r"\$",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",

    # Unicode punctuation a model emits without thinking. These are NOT
    # mis-rendered by tectonic — they are silently DROPPED, and the
    # surrounding characters close up. Measured against tectonic 0.17.0
    # under this template's own utf8/T1 preamble:
    #
    #     strong—very    ->  strongvery
    #     range 5–10     ->  range 510
    #     “hello” ‘bye’  ->  hello bye
    #     wait…          ->  wait
    #     don’t          ->  dont
    #     a<U+00A0>b     ->  aăb        (not dropped: a WRONG GLYPH)
    #     −5             ->  5
    #
    # The en-dash is why this is not a typography nicety. A synthesizer
    # writing "5–10% of samples" ships "510% of samples": a wrong number
    # in the model-written body, produced after every gate has passed.
    # Gate 5 cannot catch it — it sees a sentence carrying a citation and
    # is satisfied — and nothing downstream looks at prose again. Models
    # write ranges with en-dashes constantly.
    #
    # Every replacement below was compiled against real tectonic before
    # being put here.
    "—": "---",
    "–": "--",
    "“": "``",
    "”": "''",
    "‘": "`",
    "’": "'",
    "…": r"\ldots{}",
    " ": "~",
    "−": "$-$",
}

_SPECIAL = re.compile("|".join(re.escape(character) for character in SPECIALS))

# The two commands a synthesizer is allowed to emit. Everything else it
# writes is prose and gets escaped.
_CITE = re.compile(r"\\cite\{([^{}]*)\}")
_FACTREF = re.compile(r"\\factref\{([^{}]*)\}")
_MARKUP = re.compile(r"\\(?:cite|factref)\{[^{}]*\}")

# Coarse by design: a sentence ends at .!? followed by whitespace. This
# backs a lint, not a proof — its job is to catch a synthesizer stating a
# figure with no provenance, and it is allowed to be approximate about
# where a sentence ends because both the false-positive and the
# false-negative cost one retry, not a wrong number in the report.
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def escape(body):
    """Escape prose while leaving `\\cite{}` and `\\factref{}` intact.

    Gate 5 runs on the UNESCAPED body, because it parses those same
    spans; this runs after the gate has passed.
    """
    parts, last = [], 0
    for match in _MARKUP.finditer(body):
        parts.append(_SPECIAL.sub(lambda m: SPECIALS[m.group()],
                                  body[last:match.start()]))
        parts.append(match.group(0))
        last = match.end()
    parts.append(_SPECIAL.sub(lambda m: SPECIALS[m.group()], body[last:]))
    return "".join(parts)


def _ids(pattern, body):
    found = set()
    for match in pattern.finditer(body):
        for key in match.group(1).split(","):
            key = key.strip()
            if key:
                found.add(key)
    return sorted(found)


def cite_keys(body):
    """Every id inside a `\\cite{}`, sorted and deduplicated."""
    return _ids(_CITE, body)


def fact_refs(body):
    """Every id inside a `\\factref{}`, sorted and deduplicated."""
    return _ids(_FACTREF, body)


def unsourced_numerics(body):
    """Sentences carrying a digit but neither a citation nor a fact id.

    A sentence carrying either a citation or a fact reference is discharged
    before any digit scan, so markup digits never reach it.
    """
    flagged = []
    for sentence in _SENTENCE.split(body):
        if not sentence.strip():
            continue
        if _CITE.search(sentence) or _FACTREF.search(sentence):
            continue
        if any(character.isdigit() for character in sentence):
            flagged.append(sentence.strip())
    return flagged
