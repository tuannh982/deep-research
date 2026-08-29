"""The only test in this suite that runs a TeX engine.

Everything else replaces `render._tectonic_run` — `tests/conftest.py`'s
autouse `no_tectonic` fixture turns a real invocation into a failing test,
and `test_end_to_end.py` swaps in `stubs.tectonic_stub()`, which writes a
canned MINIMAL_PDF and returns success. So of 1354 tests, the assembled
LaTeX was compiled by nothing.

That is not a hypothetical gap. `appendices.py` and `render.py` each carry
a comment recording a tectonic-specific defect found by hand rather than by
a test: a literal U+2014 is silently DROPPED even under the template's
utf8/T1 preamble, and `\\url{}` breaks the build on an unbalanced brace.
Plan 6's F5 was a third — an Appendix A entry reading "Open: the
adversarial check found the quotes established it", a flat contradiction
that twenty new unit tests passed straight through, because each asserted
on a fragment and none compiled the document or read the sentence.

A stub cannot see any of that. This test compiles for real.

Skipped, never failed, where `tectonic` is absent: it is an optional tool,
and a suite that goes red for a missing optional tool trains people to stop
reading it.
"""
import json
import shutil

import pytest

import graph as graph_mod
import outline
import render
import runconfig

pytestmark = [
    pytest.mark.real_tectonic,
    pytest.mark.skipif(
        shutil.which("tectonic") is None,
        reason="tectonic is not on PATH; the LaTeX build is not exercised"),
]


@pytest.fixture
def awkward(mem, mkcitation, mkfact, mkhypothesis, mktask, tmp_path):
    """A report carrying, on purpose, every shape known to have broken a
    build or misread on the page.

    Not a minimal fixture. The point of compiling for real is to compile
    the things a stub hid, so each element here is one of them.
    """
    root = tmp_path / "research"
    (root / "sections").mkdir(parents=True)
    (root / "out").mkdir(parents=True)

    supporting = mkcitation(url="https://a-example.com/p",
                            domain="a-example.com",
                            quote="short wavelengths scatter more")["id"]
    # A brace in the URL: `\url{}` cannot carry one and an unbalanced brace
    # breaks the build outright. appendices._url falls back to escaped
    # monospace, and this is what proves the fallback compiles.
    braced = mkcitation(url="https://b-example.com/a{b",
                        domain="b-example.com",
                        quote="a span from a braced url")["id"]
    # `unverifiable` reaches the bibliography AND Appendix D with a
    # different note, and its note contains an `---` that must not be a
    # literal em-dash.
    walled = mkcitation(url="https://c-example.com/p", domain="c-example.com",
                        quote="a span behind a login wall",
                        status="unverifiable")["id"]
    against = mkcitation(url="https://d-example.com/p", domain="d-example.com",
                         quote="a span arguing the other way")["id"]
    mkfact(statement="blue scatters more", citations=[supporting])
    mkfact(statement="the counter finding", citations=[against])

    contested = mkhypothesis(claim="Rayleigh explains 100% of it & more",
                             status="contested", confidence=0.6,
                             supporting=[supporting], counter=[against],
                             verdict="supported")["id"]
    # Verifier prose is model-written text about a page we did not author,
    # going straight into LaTeX. Every TeX special character it can carry
    # is in here.
    mem.update(contested, verdict_reasoning=(
        "C-001 covers ~95% of cases & the remainder is #undefined; "
        "see foo_bar $x^2$ {braced} \\backslash."))
    thin = mkhypothesis(claim="A claim the check agreed with but thin",
                        status="proposed", confidence=0.17,
                        supporting=[supporting], verdict="supported")["id"]
    mkhypothesis(claim="A claim nobody reached", status="proposed")

    # Appendix E is LaTeX built from model-written search strings, and a
    # query is about the most special-character-dense thing a model
    # produces. Nothing else in the suite compiles this appendix.
    searched = mktask(question="how does scattering work?", kind="search",
                      status="done")["id"]
    mem.update(searched, queries=["100% & rayleigh_scattering",
                                  "sky {blue} $why$ #1 \\ ~tilde^"])
    refuted = mktask(question="show this is false: the effect holds",
                     kind="search", status="done")["id"]
    mem.update(refuted, queries=["evidence against the effect"],
               inputs={"stance": "against", "for_hypothesis": "H-001"})

    accepted = {
        "question": "why is the sky blue?",
        "sections": [{"id": "S-001", "theme": "T-002",
                      "title": "Optical scattering & 100% of it",
                      "hypotheses": [contested, thin], "facts": []}],
        "orphans": {"hypotheses": [], "facts": []},
        "empty_themes": [],
    }
    (root / "out" / outline.PATH_NAME).write_text(
        json.dumps(accepted), encoding="utf-8")
    (root / "sections" / "S-001.tex").write_text(
        "Short wavelengths scatter far more than long ones \\cite{%s}, and "
        "one source could not be re-read \\cite{%s}." % (supporting, walled),
        encoding="utf-8")
    (root / "sections" / "S-999.tex").write_text(
        "Taken together the themes agree.", encoding="utf-8")

    cfg = runconfig.default("why is the sky blue? 100% of it")
    cfg["scope"]["in_scope"] = ["atmospheric optics"]
    cfg["scope"]["out_of_scope"] = ["colour perception in other species"]
    return {"root": root, "graph": graph_mod.Graph(mem), "cfg": cfg,
            "braced": braced, "walled": walled}


def test_the_assembled_report_compiles(awkward):
    """The load-bearing test in this file. Everything else here inspects
    the output of a build this one proves happens at all."""
    result = render.build(awkward["root"], awkward["graph"], awkward["cfg"],
                          today="2026-08-25")
    pdf = awkward["root"] / "out" / "report.pdf"
    assert result.ok, getattr(result, "error", None)
    assert pdf.is_file()
    # stubs.MINIMAL_PDF is a few hundred bytes. A real report is tens of
    # kilobytes, so this distinguishes "tectonic ran" from "something
    # wrote a file called report.pdf".
    assert pdf.stat().st_size > 5000, pdf.stat().st_size


def test_the_build_reports_no_undefined_control_sequence(awkward):
    """A command the template never defined is an `Undefined control
    sequence`, and tectonic can still exit 0 having typeset the error
    into the page — so the build succeeding proves nothing here.

    NOT primarily about model prose: `latex.escape` turns a backslash
    into `\\textbackslash{}`, so an extractor or a synthesizer cannot
    emit a control sequence at all. What is unguarded is the LaTeX this
    codebase writes BY HAND — every string in appendices.py and
    render.py, and templates/report.tex — which is escaped by nobody and
    is where `\\factref` had to be defined in the preamble or every
    numeric section failed. Verified by mutation: emitting one undefined
    command from appendix_a fails this test.
    """
    render.build(awkward["root"], awkward["graph"], awkward["cfg"],
                 today="2026-08-25")
    logs = list((awkward["root"] / "out").glob("*.log"))
    assert logs, "tectonic --keep-logs produced no log to inspect"
    text = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in logs)
    assert "Undefined control sequence" not in text


def test_no_literal_em_dash_reaches_the_source(awkward):
    """tectonic silently DROPS U+2014 even under the template's utf8/T1
    preamble — measured against 0.17.0, and recorded in appendices.py and
    render.py where each was fixed. Silently: the build succeeds and the
    sentence ships with a word missing, which is why this asserts on the
    source rather than on the return code.

    Again not about model prose — `latex.escape` already turns an em-dash
    into `---`, so the extractor and the synthesizer are covered. The
    exposure is the hand-written strings in appendices.py, render.py and
    the template, and that is exactly where both recorded instances of
    this bug were. Verified by mutation: restoring the literal em-dash to
    appendix D's `unverifiable` note fails this test.
    """
    render.build(awkward["root"], awkward["graph"], awkward["cfg"],
                 today="2026-08-25")
    source = (awkward["root"] / "out" / "report.tex").read_text(
        encoding="utf-8")
    assert "—" not in source
