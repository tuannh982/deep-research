import json
from pathlib import Path

import pytest

import graph as graph_mod
import latex
import outline
import render
import runconfig


@pytest.fixture
def built(mem, mkcitation, mkhypothesis, tmp_path):
    root = tmp_path / "research"
    (root / "sections").mkdir(parents=True)
    (root / "out").mkdir(parents=True)

    citation = mkcitation(url="https://a-example.com/p",
                          domain="a-example.com",
                          quote="short wavelengths scatter")["id"]
    hypothesis = mkhypothesis(claim="Rayleigh explains it", status="supported",
                              confidence=0.75, verdict="supported")["id"]
    accepted = {
        "question": "why is the sky blue?",
        "sections": [{"id": "S-001", "theme": "T-002",
                      "title": "Optical scattering",
                      "hypotheses": [hypothesis], "facts": []}],
        "orphans": {"hypotheses": [], "facts": []},
        "empty_themes": [],
    }
    (root / "out" / outline.PATH_NAME).write_text(
        json.dumps(accepted), encoding="utf-8")
    (root / "sections" / "S-001.tex").write_text(
        "Short wavelengths scatter \\cite{%s}." % citation, encoding="utf-8")
    (root / "sections" / "S-999.tex").write_text(
        "Taken together the themes agree.", encoding="utf-8")

    cfg = runconfig.default("why is the sky blue?")
    cfg["scope"]["in_scope"] = ["atmospheric optics"]
    cfg["scope"]["out_of_scope"] = ["colour perception in other species"]
    return {"root": root, "graph": graph_mod.Graph(mem), "cfg": cfg,
            "citation": citation, "hypothesis": hypothesis}


def _assemble(built):
    return render.assemble(built["root"], built["graph"], built["cfg"],
                           today="2026-08-22")


def test_assemble_leaves_no_marker_behind(built):
    """A marker that survives ships a literal %%SYNTHESIS%% into the PDF."""
    document = _assemble(built)
    for marker in render.MARKERS:
        assert marker not in document


def test_assemble_includes_the_section_body(built):
    assert "Short wavelengths scatter" in _assemble(built)


def test_assemble_emits_the_section_heading_from_the_outline(built):
    """The heading comes from the validated title, never from the model's
    body — artifact.synthesize forbids a \\section in the body precisely so
    a synthesizer cannot retitle its own section after validation."""
    assert "\\section{Optical scattering}" in _assemble(built)


def test_assemble_places_synthesis_after_the_theme_sections(built):
    document = _assemble(built)
    assert document.index("Short wavelengths scatter") < \
        document.index("Taken together the themes agree")


def test_assemble_includes_the_bibliography(built):
    assert "\\bibitem{%s}" % built["citation"] in _assemble(built)


def test_assemble_includes_the_appendices(built):
    document = _assemble(built)
    assert "\\appendix" in document
    assert "Hypotheses and the evidence for them" in document
    assert "Source inventory" in document


def test_the_title_is_escaped(built):
    built["cfg"]["question"] = "Why do 100% of models fail?"
    assert "100\\%" in _assemble(built)


def test_a_missing_section_file_is_a_render_error(built):
    (built["root"] / "sections" / "S-001.tex").unlink()
    with pytest.raises(render.RenderError, match="S-001"):
        _assemble(built)


def test_a_missing_synthesis_file_is_a_render_error(built):
    (built["root"] / "sections" / "S-999.tex").unlink()
    with pytest.raises(render.RenderError, match="S-999"):
        _assemble(built)


def test_every_missing_section_is_named_at_once(built):
    """Naming one at a time means one loop round-trip per missing file."""
    (built["root"] / "sections" / "S-001.tex").unlink()
    (built["root"] / "sections" / "S-999.tex").unlink()
    with pytest.raises(render.RenderError) as caught:
        _assemble(built)
    assert "S-001" in str(caught.value) and "S-999" in str(caught.value)


def _writer(mem, mktask, section_id, status, attempts=0):
    """A synthesize task owning `section_id`. mktask has no inputs=
    keyword, so the payload is attached with a follow-up update."""
    task = mktask(question=f"write section {section_id}", kind="synthesize",
                  status=status, attempts=attempts)["id"]
    mem.update(task, inputs={"section": {"id": section_id, "title": "t",
                                         "hypotheses": [], "facts": [],
                                         "allowed_cite_keys": []}})
    return task


def test_an_abandoned_writer_yields_a_placeholder_not_a_dead_run(mem, built,
                                                                  mktask):
    """A synthesize task that fails gate 5 three times is abandoned by
    submit._fail. `abandoned` is not in Graph.OPEN_TASK_STATUSES, so it
    never re-enters the frontier: `research next` says nothing to
    dispatch, assemble raised unconditionally, and --tex-only took the
    same path. After days of research the run produced no PDF and no
    report.tex, and the operator's only escape was hand-editing a node
    file — which SKILL.md rule 1 forbids.

    Spec section 7 wants the tex "rather than nothing", and section 4 says
    the loop never blocks on a task it cannot complete."""
    (built["root"] / "sections" / "S-001.tex").unlink()
    _writer(mem, mktask, "S-001", "abandoned", attempts=3)

    document = _assemble(built)
    assert "could not be written" in document
    assert "3" in document and "Appendix C" in document
    # The heading still comes from the validated outline, so the report
    # keeps its shape and the reader sees exactly what is missing.
    assert "\\section{Optical scattering}" in document


def test_an_abandoned_synthesis_writer_also_yields_a_placeholder(mem, built,
                                                                  mktask):
    """S-999 is seeded by the same code path and abandoned by the same
    one. If only the theme sections were covered, one abandoned synthesis
    writer would still make the report permanently unrenderable."""
    (built["root"] / "sections" / "S-999.tex").unlink()
    _writer(mem, mktask, "S-999", "abandoned", attempts=3)
    assert "could not be written" in _assemble(built)


def test_a_writer_still_open_is_a_render_error(mem, built, mktask):
    """The placeholder is for work that can never finish. A writer the
    loop will still dispatch must keep raising, or `research render` would
    quietly ship a stub in place of a section that was about to be
    written."""
    (built["root"] / "sections" / "S-001.tex").unlink()
    _writer(mem, mktask, "S-001", "pending")
    with pytest.raises(render.RenderError, match="S-001"):
        _assemble(built)


def test_the_placeholder_is_not_used_for_a_writer_that_merely_finished(
        mem, built, mktask):
    """The placeholder states, as fact, that the writer was abandoned
    after N attempts. Emitting it for a `done` writer whose file went
    missing would put a false sentence into the report — the exact class
    of defect this wave exists to remove."""
    (built["root"] / "sections" / "S-001.tex").unlink()
    _writer(mem, mktask, "S-001", "done")
    with pytest.raises(render.RenderError, match="S-001"):
        _assemble(built)


def test_a_cite_the_bibliography_dropped_is_a_render_error(mem, built,
                                                            mktask):
    """Gate 5 resolves cite keys against the live graph at SUBMIT time,
    but the graph keeps moving underneath an accepted section: a `recheck`
    or `verify` task that was already outstanding when the outline froze
    is still dispatched — the synthesis freeze is on follow-on scheduling,
    not on the frontier — and applying its artifact can flip a citation to
    `rejected` AFTER a section citing it was accepted. (This docstring
    used to say "submit runs ensure_evidence_tasks on every submit, so
    research continues alongside the writers"; the freeze falsified that,
    and the re-fetch it named no longer exists at all.)

    bibliography() then skips it while the body still says \\cite{C-...}.
    Compiled against real tectonic that exits 0 and writes a PDF carrying
    a `[?]` — while Appendix D prints, for that same citation, "nothing in
    this report rests on it". That sentence is false, and code generated
    it."""
    _writer(mem, mktask, "S-001", "done")
    mem.update(built["citation"], status="rejected")
    built["graph"].invalidate_cache()

    with pytest.raises(render.RenderError) as caught:
        _assemble(built)
    assert "S-001" in str(caught.value)
    assert built["citation"] in str(caught.value)


def test_a_dropped_cite_reopens_the_writer(mem, built, mktask):
    """Raising alone would be the dead end Important 1 fixes. The section
    has to go back to a writer that can drop the claim, and `stale` is
    what puts it back on the frontier."""
    task = _writer(mem, mktask, "S-001", "done")
    mem.update(built["citation"], status="rejected")
    built["graph"].invalidate_cache()

    with pytest.raises(render.RenderError):
        render.assemble(built["root"], built["graph"], built["cfg"],
                        today="2026-08-22", memory=mem)
    assert mem.read(task)["status"] == "stale"


def test_an_unverifiable_cite_is_not_treated_as_dangling(mem, built):
    """gates.CITABLE_STATUSES is ("verified", "unverifiable") — spec
    section 6 keeps an unreadable source in the bibliography, flagged
    rather than dropped. Diffing against the wrong set would reopen a
    writer on every run that ever hit a login wall."""
    mem.update(built["citation"], status="unverifiable")
    built["graph"].invalidate_cache()
    assert "Short wavelengths scatter" in _assemble(built)


def test_a_missing_outline_is_a_render_error(built):
    (built["root"] / "out" / outline.PATH_NAME).unlink()
    with pytest.raises(render.RenderError, match="research synthesize"):
        _assemble(built)


def test_the_introduction_states_the_question_and_the_scope(built):
    text = render.introduction(built["cfg"], {"sections": []})
    assert "why is the sky blue?" in text
    assert "atmospheric optics" in text
    assert "colour perception in other species" in text


def test_the_introduction_does_not_claim_every_citation_was_confirmed(built):
    """It said "a citation to a source whose quoted span was re-fetched and
    confirmed". gates.CITABLE_STATUSES is ("verified", "unverifiable") —
    an unverifiable citation is by definition one whose span was NOT
    confirmed, kept deliberately per spec section 6 and flagged in
    Appendix D. So the Introduction contradicted the same document's own
    Limitations and Appendix D, in the report's own voice, on every run."""
    text = render.introduction(built["cfg"], {"sections": []})
    assert "re-fetched and confirmed" not in text
    assert "Appendix D" in text and "flagged" in text


def test_the_introduction_does_not_promise_facts_in_an_appendix(built):
    """It said a fact identifier "resolv[es] to Appendix A". \\factref
    renders as a superscript F-id and no appendix lists facts — A is
    hypotheses, B assumptions, C open questions, D citations. The
    traceability promise was unredeemable by a reader holding the PDF."""
    text = render.introduction(built["cfg"], {"sections": []})
    assert "Appendix A" not in text


def test_the_introduction_does_not_claim_every_sentence_is_sourced(built):
    """Gate 5 requires a citation only for sentences stating a figure. A
    qualitative claim passes unsourced, so 'every claim carries' is a
    promise the pipeline does not keep — in the one section of the report
    that tells a reader how much to trust the rest."""
    text = render.introduction(built["cfg"], {"sections": []})
    assert "Every claim" not in text
    assert "figure" in text or "number" in text


def test_limitations_reports_unverifiable_sources(mem, built, mkcitation):
    """Spec's open risks: 'a report can contain claims resting on unverified
    quotes'. The Limitations section is where that is said out loud."""
    mkcitation(url="https://c-example.com/p", domain="c-example.com",
               quote="behind a wall here", status="unverifiable")
    text = render.limitations(graph_mod.Graph(mem), built["cfg"],
                              {"sections": [], "empty_themes": []})
    assert "1" in text and "could not be independently" in text


def test_limitations_states_the_thin_count_without_naming_a_score(
    mem, built, mkhypothesis
):
    """The sentence read "N hypothesis(es) remain below the promotion
    threshold of 0.6. They are reported in Appendix A with their scores".
    Appendix A no longer carries scores, and 0.6 means nothing to a reader
    who has been shown no other number — it is a gating constant, and
    naming it here is the last place the report leaked the arithmetic.

    The count itself stays. It is computed from the score, which is fine:
    the score is still doing its internal job."""
    mkhypothesis(claim="thin", confidence=0.2)
    text = render.limitations(graph_mod.Graph(mem), built["cfg"],
                              {"sections": [], "empty_themes": []})
    assert "1" in text
    assert "0.6" not in text
    assert "score" not in text.lower()


def test_limitations_does_not_promise_confidence_figures_the_report_lacks(
    mem, built, mkcitation
):
    """The unverifiable-sources sentence ended "carry less weight than the
    confidence figures alone suggest" — pointing at figures that are no
    longer in the document."""
    mkcitation(url="https://c-example.com/p", domain="c-example.com",
               quote="behind a wall here", status="unverifiable")
    text = render.limitations(graph_mod.Graph(mem), built["cfg"],
                              {"sections": [], "empty_themes": []})
    assert "confidence" not in text.lower()


def test_limitations_says_when_a_promoted_claim_was_never_challenged(
    mem, built, mkhypothesis
):
    """"N claims were challenged and survived" and "N were never
    challenged" are materially different reports, and until the refute
    search existed only the second was ever true. A run can still halt
    with an unchallenged claim — by signal, or on an abandoned refute
    task — and the Limitations section is where that is said out loud
    rather than left for a reader to infer from Appendix A."""
    mkhypothesis(claim="unchallenged", status="supported", confidence=0.6,
                 verdict="supported")
    text = render.limitations(graph_mod.Graph(mem), built["cfg"],
                              {"sections": [], "empty_themes": []})
    assert "never challenged" in text.lower()


def test_limitations_is_quiet_when_every_claim_was_challenged(
    mem, built, mkhypothesis, mktask
):
    """Guards the guard: a sentence that always appears says nothing.

    Challenges the fixture's OWN hypothesis rather than adding a second
    one. The `built` fixture already carries a promoted claim, so a test
    that only challenged a newly-created one would leave that first claim
    unchallenged and see the sentence anyway — passing or failing for a
    reason unrelated to what it is checking."""
    refute = mktask(question="q", kind="search", status="done")["id"]
    mem.update(refute, inputs={"for_hypothesis": built["hypothesis"],
                               "stance": "against"})
    text = render.limitations(graph_mod.Graph(mem), built["cfg"],
                              {"sections": [], "empty_themes": []})
    assert "never challenged" not in text.lower()


def test_limitations_reports_an_abandoned_task(mem, built, mktask):
    task = mktask(question="unanswerable", status="abandoned")["id"]
    mem.update(task, abandoned_reason="3 attempts")
    text = render.limitations(graph_mod.Graph(mem), built["cfg"],
                              {"sections": [], "empty_themes": []})
    assert "abandoned" in text.lower()


def test_limitations_does_not_count_a_writer_as_a_line_of_enquiry(mem, built,
                                                                    mktask):
    """A `synthesize` task writes prose from evidence already gathered; it
    researches nothing. Counting one as a line of enquiry overstated the
    number of real dead ends in the honesty surface the whole system
    exists for.

    Newly reachable: before an abandoned writer was allowed to render at
    all (it raised, so no PDF shipped), this sentence could never appear
    alongside one. Making the report renderable made the miscount
    visible."""
    research = mktask(question="unanswerable", status="abandoned")["id"]
    mem.update(research, abandoned_reason="3 attempts")
    writer = mktask(question="write section S-001: Optics", kind="synthesize",
                    status="abandoned")["id"]
    mem.update(writer, abandoned_reason="3 attempts, all rejected by gate 5")

    text = render.limitations(graph_mod.Graph(mem), built["cfg"],
                              {"sections": [], "empty_themes": []})
    assert "1 line(s) of enquiry were abandoned" in text
    assert "2 line(s)" not in text


def test_limitations_does_not_count_the_outliner_as_a_line_of_enquiry(
        mem, built, mktask):
    """The other half of MACHINERY_KINDS. An `outline` task that fails
    validation three times is abandoned by the same code path, and it
    researches nothing either."""
    outliner = mktask(question="arrange the report outline", kind="outline",
                      status="abandoned")["id"]
    mem.update(outliner, abandoned_reason="3 attempts")
    text = render.limitations(graph_mod.Graph(mem), built["cfg"],
                              {"sections": [], "empty_themes": []})
    assert "line(s) of enquiry were abandoned" not in text


def test_limitations_reports_a_section_that_could_not_be_written(built):
    """Excluding the writer from the research count must not make it
    disappear from the Limitations section altogether. A reader gauging
    how complete this report is has to learn that a whole chapter carries
    a placeholder instead of prose."""
    text = render.limitations(built["graph"], built["cfg"],
                              {"sections": [], "empty_themes": []},
                              placeholders=["S-001"])
    assert "1 section(s) of this report could not be written" in text


def test_limitations_says_nothing_about_writers_when_they_all_succeeded(built):
    """The normal case. A bullet claiming zero unwritten sections would be
    noise on every healthy run."""
    text = render.limitations(built["graph"], built["cfg"],
                              {"sections": [], "empty_themes": []})
    assert "could not be written" not in text


def test_an_abandoned_writer_reaches_the_limitations_section(mem, built,
                                                              mktask):
    """End to end through assemble: the placeholder in the body and the
    Limitations bullet have to agree, because they describe the same
    thing to the same reader."""
    (built["root"] / "sections" / "S-001.tex").unlink()
    _writer(mem, mktask, "S-001", "abandoned", attempts=3)

    document = _assemble(built)
    assert "could not be written; the writer was abandoned" in document
    assert "1 section(s) of this report could not be written" in document


def test_a_section_written_before_its_writer_was_abandoned_is_not_called_unwritten(
        mem, built, mktask):
    """The reason the count is threaded from `assemble` rather than taken
    off the graph. A writer can be re-opened after its body was accepted
    (render.reopen_section marks it stale, it does not delete the .tex)
    and then abandoned on the retries. The section still has real prose,
    so "could not be written" would be false — the exact defect class
    this round exists to remove."""
    _writer(mem, mktask, "S-001", "abandoned", attempts=3)   # file kept

    document = _assemble(built)
    assert "Short wavelengths scatter" in document      # the real body
    assert "could not be written" not in document


def test_limitations_uses_the_latex_em_dash(mem, built, mkcitation):
    """Same defect as Appendix C and D: a literal U+2014 is silently
    dropped by tectonic, so "could not be independently re-checked —
    typically a login wall" typeset as "re-checked typically a login
    wall"."""
    mkcitation(url="https://c-example.com/p", domain="c-example.com",
               quote="behind a wall here", status="unverifiable")
    text = render.limitations(graph_mod.Graph(mem), built["cfg"],
                              {"sections": [], "empty_themes": []},
                              placeholders=["S-001"])
    assert "—" not in text
    assert "---" in text


def test_limitations_is_never_empty(built):
    """An empty Limitations section reads as 'this report has none', which
    is never true and is the most misleading thing it could say."""
    text = render.limitations(built["graph"], built["cfg"],
                              {"sections": [], "empty_themes": []})
    assert text.strip()


# --- the guard's stated reason must be one that still holds -----------

_STALE_PREMISE = "ensure_evidence_tasks on every submit"


def _protocol_section(heading):
    """One section of loop-protocol.md, whitespace collapsed -- the file is
    hard-wrapped, so a phrase to look for otherwise has to be matched
    around whatever column it happened to break at."""
    text = (Path(__file__).resolve().parents[1] / "references"
            / "loop-protocol.md").read_text(encoding="utf-8")
    return " ".join(text.split(heading)[1].split("\n## ")[0].split())


def test_the_dangling_cite_guard_gives_a_reason_that_is_still_true():
    """Both this guard's docstring and loop-protocol.md justified it with
    "submit runs ensure_evidence_tasks on every submit, so research
    carries on alongside the writers". The synthesis freeze falsified
    that: step 4 is skipped for the whole synthesize phase.

    The guarded case is still reachable -- an outstanding `recheck` can
    reject a citation mid-synthesis, pinned by
    tests/test_submit.py::test_an_outstanding_recheck_can_still_reject_a
    _citation_mid_synthesis -- so the danger is the opposite of a dead
    guard. A maintainer who reads the stale premise, checks it, and finds
    it false concludes the case cannot occur and deletes a guard that
    still fires. The reason has to be the true one."""
    doc = render._dangling_cites.__doc__
    assert _STALE_PREMISE not in doc
    assert "recheck" in doc.lower()


def test_loop_protocol_gives_the_same_reason_the_guard_does():
    """The operator-facing half, and it contradicted its own new paragraph
    a few lines above -- the synthesis section already says submit skips
    its follow-on scheduling while the phase is `synthesize`."""
    section = _protocol_section("## A citation rejected after its section "
                                "was accepted")
    assert _STALE_PREMISE not in section
    assert "keeps gathering evidence alongside the writers" not in section
    assert "re-check" in section


def test_the_introduction_scopes_its_sourcing_claim_to_what_gate_5_reads(
        mem, built, mkhypothesis):
    """The fourth iteration of the same defect, and the comment above that
    very sentence says it keeps recurring.

    "Every sentence stating a figure carries either a citation ... or a
    fact identifier" is true of section bodies, which is all gate 5
    (gates.report_section -> latex.unsourced_numerics) ever reads. It is
    not true of the report as a whole: Limitations emits "N hypothesis(es)
    remain below the promotion threshold of 0.6" with neither, and so do
    the appendices. Low harm -- those are self-evidently statements about
    the run, not about the world -- but the Introduction is the paragraph
    that tells a reader how much to trust the rest, so it has to say which
    part of the document the guarantee covers.

    The second half of this test is the point: it shows the exemption is a
    real one, not a rhetorical hedge, by running gate 5's own numeric
    check over the generated Limitations text and finding a figure it
    would reject."""
    mkhypothesis(claim="thin", confidence=0.1)
    text = render.introduction(built["cfg"], {"sections": []})
    assert "themed sections" in text
    assert "Limitations" in text and "appendices" in text

    limits = render.limitations(graph_mod.Graph(mem), built["cfg"],
                                {"sections": [], "empty_themes": []})
    assert latex.unsourced_numerics(limits), (
        "Limitations no longer states an unsourced figure, so the "
        "Introduction's exemption may be describing nothing")
