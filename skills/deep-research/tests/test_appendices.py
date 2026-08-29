from pathlib import Path

import pytest

import appendices
import graph as graph_mod

EMPTY_OUTLINE = {"question": "q", "sections": [],
                 "orphans": {"hypotheses": [], "facts": []},
                 "empty_themes": []}


def test_bibliography_lists_a_citable_citation(mem, mkcitation):
    citation = mkcitation(url="https://a-example.com/p",
                          domain="a-example.com",
                          quote="a verified span here")["id"]
    output = appendices.bibliography(graph_mod.Graph(mem))
    assert "\\bibitem{%s}" % citation in output
    assert "a-example.com" in output


def test_the_bibliography_states_the_publication_date(mem, mkcitation):
    """The retrieval date was the only date on a bibliography entry, and
    it is the least interesting one: apply_recheck writes `fetched_at`,
    so it records when WE re-read the page. A reader could not tell a
    2011 source from a 2025 one."""
    citation = mkcitation(url="https://a-example.com/p",
                          domain="a-example.com",
                          quote="a verified span here")["id"]
    mem.update(citation, published_at="2019-03-04")
    output = appendices.bibliography(graph_mod.Graph(mem))
    assert "2019-03-04" in output


def test_the_bibliography_says_undated_when_there_is_none(mem, mkcitation):
    """Stated, not omitted. A missing date reads as an oversight in the
    rendering; the report should say the source does not carry one — and
    a source with no date is a fact about the source."""
    mkcitation(url="https://a-example.com/p", domain="a-example.com",
               quote="a verified span here")
    output = appendices.bibliography(graph_mod.Graph(mem))
    assert "undated" in output.lower()


def test_appendix_d_states_the_publication_date(mem, mkcitation):
    """Appendix D is the source inventory — every citation the run
    touched, including the rejected and unreadable ones. Whoever is
    auditing a source there needs its date as much as the bibliography's
    reader does."""
    citation = mkcitation(url="https://a-example.com/p",
                          domain="a-example.com",
                          quote="a verified span here")["id"]
    mem.update(citation, published_at="2019")
    output = appendices.appendix_d(graph_mod.Graph(mem))
    assert "2019" in output


def test_bibliography_excludes_a_rejected_citation(mem, mkcitation):
    """A rejected citation's quote is not on the page. Listing it in the
    references would put a source the report cannot stand behind in front
    of the reader as though it could.

    Also asserts the verified citation's \\bibitem IS present, so this
    test cannot pass against a bibliography() that returns "" — only
    against one that actually discriminates by status."""
    verified = mkcitation(url="https://a-example.com/p",
                          domain="a-example.com",
                          quote="a verified span here")["id"]
    rejected = mkcitation(url="https://b-example.com/p",
                          domain="b-example.com",
                          quote="a rejected span here",
                          status="rejected")["id"]
    output = appendices.bibliography(graph_mod.Graph(mem))
    assert rejected not in output
    assert "\\bibitem{%s}" % verified in output


def test_bibliography_escapes_a_title(mem, mkcitation):
    citation = mkcitation(url="https://a-example.com/p",
                          domain="a-example.com", quote="a span here")["id"]
    mem.update(citation, title="Profits & Losses: 100% of the story")
    output = appendices.bibliography(graph_mod.Graph(mem))
    assert "100\\%" in output and "\\&" in output


def test_a_url_with_braces_falls_back_to_escaped_monospace(mem, mkcitation):
    """\\url{} cannot carry an unbalanced brace — it breaks the build. A URL
    is attacker-influenced input in the sense that matters here: it comes
    from a page we did not write."""
    citation = mkcitation(url="https://a-example.com/a{b",
                          domain="a-example.com", quote="a span here")["id"]
    output = appendices.bibliography(graph_mod.Graph(mem))
    assert "\\url{https://a-example.com/a{b}" not in output
    assert "\\{b" in output


def test_a_url_with_a_backslash_falls_back_to_escaped_monospace(mem,
                                                                 mkcitation):
    """`_url`'s fallback checks for a brace OR a backslash; only the
    brace path was covered before this test. A literal backslash inside
    \\url{} is passed straight to the LaTeX engine, which reads it as
    the start of a control sequence and breaks the build."""
    citation = mkcitation(url="https://a-example.com/a\\b",
                          domain="a-example.com", quote="a span here")["id"]
    output = appendices.bibliography(graph_mod.Graph(mem))
    assert "\\url{https://a-example.com/a\\b}" not in output
    assert "\\texttt{" in output
    assert "\\textbackslash{}" in output


def test_appendix_a_reports_a_hypothesis_and_what_the_check_found(
    mem, mkhypothesis
):
    """Renamed from test_appendix_a_reports_a_hypothesis_with_its_confidence,
    which asserted "0.75" appeared. It did, and that was the defect: the
    score is a gating threshold, not a probability, and it was printed
    bare next to a claim with nothing in the document defining it."""
    hypothesis = mkhypothesis(claim="Rayleigh explains it", status="supported",
                              confidence=0.75, verdict="supported")["id"]
    output = appendices.appendix_a(graph_mod.Graph(mem))
    assert hypothesis in output
    assert "Rayleigh explains it" in output
    assert "Supported" in output


def test_appendix_a_does_not_print_a_confidence_score(mem, mkhypothesis):
    """base x spread x weight saturates at 0.96 and puts the modal
    promoted claim -- 3 citations across 2 domains, verdict supported --
    at exactly 0.60. Printed bare beside a claim, a reader reads that as
    "60% likely". It is a promotion threshold and a stop-predicate input,
    and it stays on the node for both; it does not belong in front of a
    reader without the formula beside it."""
    mkhypothesis(claim="Rayleigh explains it", status="supported",
                 confidence=0.75, verdict="supported")
    output = appendices.appendix_a(graph_mod.Graph(mem))
    assert "0.75" not in output
    assert "confidence" not in output.lower()


def test_appendix_a_names_the_evidence_on_both_sides(mem, mkhypothesis,
                                                     mkcitation):
    """The auditability fix. The entry named no citation at all, so a
    reader who wanted to check a claim had nowhere to start, and a
    contested claim was visually identical to an undisputed one but for
    a single word."""
    for_it = mkcitation(url="https://a-example.com/p", domain="a-example.com",
                        quote="a supporting span here")["id"]
    against = mkcitation(url="https://b-example.com/p", domain="b-example.com",
                         quote="a countering span here")["id"]
    mkhypothesis(claim="Rayleigh explains it", status="contested",
                 supporting=[for_it], counter=[against],
                 confidence=0.6, verdict="supported")
    output = appendices.appendix_a(graph_mod.Graph(mem))
    assert for_it in output and against in output
    assert "For:" in output and "Against:" in output


def test_appendix_a_labels_an_empty_side_rather_than_omitting_it(
    mem, mkhypothesis, mkcitation
):
    """The absence of counter-evidence is itself information about a
    claim. A missing "Against:" label reads as a rendering fault, and
    leaves the reader unable to tell "nothing argues against this" from
    "we did not say".

    The label got MORE specific once refute searches existed: a bare
    "none" could not distinguish "nobody looked" from "we looked and
    found nothing", and both are now reachable. This asserts the label is
    present and says which; the two cases have their own tests."""
    for_it = mkcitation(url="https://a-example.com/p", domain="a-example.com",
                        quote="a supporting span here")["id"]
    mkhypothesis(claim="Rayleigh explains it", status="supported",
                 supporting=[for_it], confidence=0.6, verdict="supported")
    output = appendices.appendix_a(graph_mod.Graph(mem))
    assert "Against: not searched for" in output


def test_appendix_a_carries_the_verifier_s_reasoning(mem, mkhypothesis):
    """The argument is the thing a reader auditing a claim actually
    needs. It exists on the node only since the verdict_reasoning field
    landed; before that it reached inbox/applied/ and stopped there."""
    hypothesis = mkhypothesis(claim="Rayleigh explains it", status="supported",
                              confidence=0.6, verdict="supported")["id"]
    mem.update(hypothesis,
               verdict_reasoning="C-001 states the wavelength dependence.")
    output = appendices.appendix_a(graph_mod.Graph(mem))
    assert "C-001 states the wavelength dependence." in output


def test_appendix_a_escapes_the_reasoning(mem, mkhypothesis):
    """Model-written prose about a page we did not author, going straight
    into LaTeX. Everything else in this file is escaped; this arrived
    later and would be the one hole."""
    hypothesis = mkhypothesis(claim="c", status="supported", confidence=0.6,
                              verdict="supported")["id"]
    mem.update(hypothesis, verdict_reasoning="covers 100% of cases & more")
    output = appendices.appendix_a(graph_mod.Graph(mem))
    assert "100\\%" in output and "\\&" in output


def test_appendix_a_says_a_claim_survived_a_search_for_its_opposite(
    mem, mkhypothesis, mkcitation, mktask
):
    """"Against: none" reads identically whether nobody looked or nobody
    found anything, and those are not the same fact about a claim. The
    second is a result; the first is a gap. Until the refute search
    existed only the first was ever true, so the conflation cost nothing
    — now it would be the most misleading line in the document."""
    for_it = mkcitation(url="https://a-example.com/p", domain="a-example.com",
                        quote="a supporting span here")["id"]
    hypothesis = mkhypothesis(claim="Rayleigh explains it", status="supported",
                              supporting=[for_it], confidence=0.6,
                              verdict="supported")["id"]
    refute = mktask(question="Find evidence that would show this claim is "
                             "false: Rayleigh explains it",
                    kind="search", status="done")["id"]
    mem.update(refute, inputs={"for_hypothesis": hypothesis,
                               "stance": "against"})
    output = appendices.appendix_a(graph_mod.Graph(mem))
    assert "searched for, none found" in output


def test_appendix_a_says_when_a_claim_was_never_challenged(
    mem, mkhypothesis, mkcitation
):
    """Reachable: a run halted by signal before the challenge ran, or an
    abandoned refute task. The reader has to be able to tell."""
    for_it = mkcitation(url="https://a-example.com/p", domain="a-example.com",
                        quote="a supporting span here")["id"]
    mkhypothesis(claim="never challenged", status="supported",
                 supporting=[for_it], confidence=0.6, verdict="supported")
    output = appendices.appendix_a(graph_mod.Graph(mem))
    assert "not searched for" in output


def test_appendix_a_explains_an_open_claim_the_check_agreed_with(
    mem, mkhypothesis, mkcitation
):
    """Found by reading a built PDF, not by a unit test. An entry read
    "Open: the adversarial check found the quotes established it." —
    which is a flat contradiction to anyone who has not read
    apply._verified_status. The claim is Open because one citation scores
    0.17 against a 0.6 promotion bar: the verifier agreed, and the
    evidence still is not enough. Both halves are true and the entry has
    to say so, or the appendix looks broken at exactly the moment a
    reader starts to trust it."""
    for_it = mkcitation(url="https://a-example.com/p", domain="a-example.com",
                        quote="a supporting span here")["id"]
    mkhypothesis(claim="thin but agreed with", status="proposed",
                 supporting=[for_it], confidence=0.17, verdict="supported")
    output = appendices.appendix_a(graph_mod.Graph(mem))
    assert "has not been promoted" in output


def test_appendix_a_does_not_add_that_clause_to_a_promoted_claim(
    mem, mkhypothesis
):
    """The concession only makes sense where the two halves disagree."""
    mkhypothesis(claim="promoted", status="supported", confidence=0.6,
                 verdict="supported")
    output = appendices.appendix_a(graph_mod.Graph(mem))
    assert "has not been promoted" not in output


def test_appendix_a_distinguishes_unchecked_from_checked_and_found_wanting(
    mem, mkhypothesis
):
    """verdict None is the normal state of a claim the loop has not
    reached yet, not a failure. Rendering it the same as `unsupported`
    would report work that has not happened as work that came back
    negative."""
    unchecked = mkhypothesis(claim="not yet looked at")["id"]
    output = appendices.appendix_a(graph_mod.Graph(mem))
    assert unchecked in output
    assert "no adversarial check has run" in output


def test_appendix_a_includes_a_refuted_hypothesis(mem, mkhypothesis):
    """Excluded from the body, reported here. That is the difference between
    not narrating a refuted claim as a finding and losing it."""
    refuted = mkhypothesis(claim="a wrong idea", status="refuted")["id"]
    output = appendices.appendix_a(graph_mod.Graph(mem))
    # "Refuted", not "refuted": the status is rendered as a word to a
    # reader now rather than echoed as the enum token it is on the node.
    assert refuted in output and "Refuted" in output


def test_appendix_b_reports_a_refuted_assumption(mem, mkassumption):
    assumption = mkassumption(statement="the sensor is calibrated",
                              status="refuted", refuted_by="H-004")["id"]
    output = appendices.appendix_b(graph_mod.Graph(mem))
    assert assumption in output and "H-004" in output


def test_appendix_b_ignores_an_open_assumption(mem, mkassumption):
    """Also asserts a refuted assumption from the same graph IS present,
    so this test cannot pass against an appendix_b() that returns
    nothing at all — only against one that actually filters by status."""
    still_open = mkassumption(statement="unresolved", status="open")["id"]
    refuted = mkassumption(statement="the sensor is calibrated",
                           status="refuted", refuted_by="H-004")["id"]
    output = appendices.appendix_b(graph_mod.Graph(mem))
    assert still_open not in output
    assert refuted in output


def test_appendix_c_reports_an_abandoned_task(mem, mktask):
    task = mktask(question="a question nothing could answer",
                  status="abandoned")["id"]
    mem.update(task, abandoned_reason="3 attempts, all rejected")
    output = appendices.appendix_c(graph_mod.Graph(mem), EMPTY_OUTLINE)
    assert task in output and "3 attempts" in output


def test_appendix_c_reports_an_undispatchable_task(mem, mktask):
    """graph.undispatchable() is the second of appendix C's four sources
    and, unlike the other three, was previously untested here. A task
    depending on an `abandoned` one can never be dispatched: `abandoned`
    is a terminal state outside SATISFIABLE_DEP_STATUSES, so the
    dependency never settles and the dependent never reaches the
    frontier. Verifying `graph.undispatchable()` actually returns the
    task (not just checking appendix_c's output) is what stops this test
    from passing vacuously if the fixture happened not to trigger the
    condition."""
    dead_end = mktask(question="a prerequisite nothing could satisfy",
                      status="abandoned")["id"]
    stuck = mktask(question="a question stuck behind a dead end",
                   depends_on=[dead_end])["id"]
    graph = graph_mod.Graph(mem)
    assert stuck in graph.undispatchable()
    output = appendices.appendix_c(graph, EMPTY_OUTLINE)
    assert stuck in output
    assert "Never dispatchable" in output


def test_appendix_c_reports_an_orphaned_finding(mem):
    outline = {**EMPTY_OUTLINE,
               "orphans": {"hypotheses": ["H-007"], "facts": []}}
    output = appendices.appendix_c(graph_mod.Graph(mem), outline)
    assert "H-007" in output


def test_appendix_c_reports_a_theme_that_produced_nothing(mem, mktask):
    theme = mktask(question="a line of enquiry that went nowhere",
                   depth=1)["id"]
    outline = {**EMPTY_OUTLINE, "empty_themes": [theme]}
    output = appendices.appendix_c(graph_mod.Graph(mem), outline)
    assert "went nowhere" in output


def test_appendix_c_separates_a_question_from_its_verdict(mem, mktask):
    """A task question is not guaranteed to end in punctuation. An
    abandoned section writer's question is "write section S-001: <title>",
    so the entry ran straight into the italic verdict and read as one
    sentence: "...scatters sunlight Abandoned: 3 attempts."."""
    task = mktask(question="write section S-001: Optics", kind="synthesize",
                  status="abandoned")["id"]
    mem.update(task, abandoned_reason="3 attempts")
    output = appendices.appendix_c(graph_mod.Graph(mem), EMPTY_OUTLINE)
    assert "Optics. \\emph{Abandoned" in output


def test_appendix_c_does_not_double_punctuate_a_real_question(mem, mktask):
    """The common case is a decomposer's question, which already ends in a
    question mark. Appending a full stop to it would be worse than the
    problem being fixed."""
    task = mktask(question="why is the sky blue?", status="abandoned")["id"]
    mem.update(task, abandoned_reason="3 attempts")
    output = appendices.appendix_c(graph_mod.Graph(mem), EMPTY_OUTLINE)
    assert "blue? \\emph{Abandoned" in output
    assert "blue?." not in output


def test_appendix_c_uses_the_latex_em_dash(mem, mktask):
    """tectonic SILENTLY DROPS a literal U+2014. Verified against 0.17.0
    with the template's own utf8/T1 preamble: `AAA --- BBB` typesets an
    em-dash, `CCC — DDD` typesets "CCC DDD". So every one of these
    generated sentences lost its dash and read as a run-on."""
    dead_end = mktask(question="a prerequisite nothing could satisfy",
                      status="abandoned")["id"]
    mktask(question="a question stuck behind a dead end",
           depends_on=[dead_end])
    output = appendices.appendix_c(graph_mod.Graph(mem), EMPTY_OUTLINE)
    assert "Never dispatchable ---" in output
    assert "—" not in output


def test_appendix_d_uses_the_latex_em_dash(mem, mkcitation):
    """Appendix D is the honesty surface, and this is where the dropped
    dash read worst: "rejected the quote was not found on the page"."""
    mkcitation(url="https://b-example.com/p", domain="b-example.com",
               quote="a rejected span", status="rejected")
    mkcitation(url="https://c-example.com/p", domain="c-example.com",
               quote="behind a wall here", status="unverifiable")
    output = appendices.appendix_d(graph_mod.Graph(mem))
    assert "—" not in output
    assert "---" in output


def test_appendix_d_flags_an_unverifiable_source(mem, mkcitation):
    """Spec section 6: flagged rather than silently trusted. This is the
    surface where that promise is kept."""
    walled = mkcitation(url="https://c-example.com/p", domain="c-example.com",
                        quote="behind a wall here",
                        status="unverifiable")["id"]
    output = appendices.appendix_d(graph_mod.Graph(mem))
    assert walled in output
    assert "not independently verified" in output


def test_appendix_d_includes_a_rejected_citation(mem, mkcitation):
    """Appendix D is the honesty surface and reports every source the run
    touched, including the ones it threw out. The bibliography is the set
    the report stands behind; these two must not be unified."""
    rejected = mkcitation(url="https://b-example.com/p",
                          domain="b-example.com", quote="a rejected span",
                          status="rejected")["id"]
    output = appendices.appendix_d(graph_mod.Graph(mem))
    assert rejected in output


@pytest.mark.parametrize("status,expected,forbidden", [
    # Gate 2 is a `rechecker` subagent re-reading the page with WebFetch,
    # never having been shown the claim the quote supports. "Independent"
    # is a claim about that isolation, and it is the strongest thing this
    # report says about any source.
    ("verified", "quote confirmed by an independent re-check",
     ("re-fetch", "http", "byte-for-byte")),
    # Spec section 6: flagged rather than silently trusted. It is the
    # ABSENCE of verification, not disproof -- the note must not read as
    # either a pass or a rejection.
    ("unverifiable",
     "\\textbf{not independently verified} --- the re-check agent could "
     "not read the page",
     ("rejected", "confirmed")),
    # True by construction, not by hope: `rejected` is not in
    # gates.CITABLE_STATUSES, and render._dangling_cites refuses to build
    # a report whose body still cites one.
    ("rejected",
     "\\textbf{rejected} --- the quote was not found on the page; nothing "
     "in this report rests on it",
     ("confirmed", "could not read")),
    # No verdict has landed. Saying anything more would be the exact
    # overstatement this appendix exists to prevent.
    ("pending", "\\textbf{not yet checked}", ("confirmed", "rejected")),
])
def test_appendix_d_says_what_actually_happened_to_a_source(
    mem, mkcitation, status, expected, forbidden
):
    """The single highest-stakes honesty surface in the system: the line
    that tells a report's reader what happened to a source. All four notes
    are pinned here because prose no gate reads is exactly where this
    defect keeps recurring -- twice already, once as a mechanism that no
    longer exists and once as a dropped em-dash that turned "rejected ---
    the quote was not found" into "rejected the quote was not found"."""
    citation = mkcitation(url="https://b-example.com/p",
                          domain="b-example.com", quote="a quoted span here",
                          status=status)["id"]
    output = appendices.appendix_d(graph_mod.Graph(mem))
    assert citation in output
    assert expected in output
    # The note only, not the whole entry: the entry also carries the
    # source's own URL, and "http" appearing in `https://...` says nothing
    # about what the run claims it did.
    note = output.split("Retrieved ")[1].split(". ", 1)[1].lower()
    for phrase in forbidden:
        assert phrase not in note.replace("\\textbf", "")


def test_appendix_d_claims_no_mechanism_this_codebase_no_longer_has():
    """Gate 2 stopped being an httpx re-download in this branch. A note
    describing a client that does not exist is a false statement about how
    a source was checked, and Appendix D is the last place that may
    happen."""
    source = appendices.appendix_d.__doc__ + "".join(
        line for line in
        (Path(appendices.__file__).read_text(encoding="utf-8")
         .split("def appendix_d")[1].split("\ndef ")[0]).splitlines())
    for gone in ("httpx", "re-fetch", "refetch", "page_sha256",
                 "http_status", "status code"):
        assert gone not in source.lower()


def test_an_empty_appendix_says_so_rather_than_rendering_blank(mem):
    """An empty \\begin{description} is a LaTeX error, and a silently blank
    appendix reads as a rendering bug rather than as a true 'none'."""
    output = appendices.appendix_b(graph_mod.Graph(mem))
    assert "None" in output
    assert "\\begin{description}\n\\end{description}" not in output


def _search(mem, mktask, question, queries, stance=None, status="done"):
    task = mktask(question=question, kind="search", status=status)["id"]
    inputs = {"stance": stance} if stance else {}
    mem.update(task, queries=list(queries), inputs=inputs)
    return task


def test_appendix_e_lists_the_queries_of_each_search(mem, mktask):
    """Nothing recorded what a run actually searched for, so no part of
    its literature search could be re-run or assessed for coverage."""
    _search(mem, mktask, "how does scattering work?",
            ["rayleigh scattering", "why is the sky blue"])
    output = appendices.appendix_e(graph_mod.Graph(mem))
    assert "rayleigh scattering" in output
    assert "why is the sky blue" in output


def test_appendix_e_says_the_queries_are_self_reported(mem, mktask):
    """Nothing in this process observes the WebSearch call — the searcher
    tells us what it says it sent. An appendix whose entire purpose is
    reproducibility must not overstate what it knows, or it is worse than
    no appendix."""
    _search(mem, mktask, "q", ["a query"])
    output = appendices.appendix_e(graph_mod.Graph(mem))
    assert "as reported" in output.lower()


def test_appendix_e_includes_a_search_that_found_nothing(mem, mktask):
    """The entry a reader most needs: it is what separates "the question
    is exhausted" from "we asked badly"."""
    _search(mem, mktask, "a question with no answers", ["a fruitless query"])
    output = appendices.appendix_e(graph_mod.Graph(mem))
    assert "a fruitless query" in output


def test_appendix_e_marks_a_refute_search_as_one(mem, mktask):
    """Searching for a claim's disproof is a different act from searching
    for its support, and a reader assessing coverage needs to see which
    was which."""
    _search(mem, mktask, "show this is false: X", ["counter-evidence for X"],
            stance="against")
    output = appendices.appendix_e(graph_mod.Graph(mem))
    assert "disproof" in output.lower() or "against" in output.lower()


def test_appendix_e_escapes_a_query(mem, mktask):
    """A query is a model-written string going straight into LaTeX."""
    _search(mem, mktask, "q", ["100% of cases & more_things"])
    output = appendices.appendix_e(graph_mod.Graph(mem))
    assert "100\\%" in output and "\\&" in output


def test_appendix_e_is_not_empty_looking_when_no_search_ran(mem):
    """An empty description environment is a LaTeX error, and a blank
    appendix reads as a rendering bug rather than a true "none"."""
    output = appendices.appendix_e(graph_mod.Graph(mem))
    assert appendices.EMPTY in output


def test_render_all_emits_the_five_appendices_in_order(mem):
    """Renamed from ..._the_four_...: Appendix E was added and the old
    name pinned the count."""
    output = appendices.render_all(graph_mod.Graph(mem), EMPTY_OUTLINE)
    positions = [output.index(title) for title in (
        "Hypotheses and the evidence for them", "Refuted assumptions",
        "Open questions", "Source inventory", "Search queries")]
    assert positions == sorted(positions)


def test_appendix_a_marks_a_claim_with_no_primary_source(
    mem, mkhypothesis, mkcitation
):
    """Gate 3 counts distinct registrable domains, and two of them can
    still be one source — a syndicated release, or two posts citing one
    paper. That is not detectable cheaply, so the report discloses what
    it does know: whether anything carrying this claim presented its own
    work."""
    ids = []
    for index in range(2):
        citation = mkcitation(url=f"https://n{index}-example.com/p",
                              domain=f"n{index}-example.com",
                              quote=f"a relayed span {index}")["id"]
        mem.update(citation, source_type="secondary")
        ids.append(citation)
    mkhypothesis(claim="all relayed", status="supported", supporting=ids,
                 confidence=0.67, verdict="supported")
    output = appendices.appendix_a(graph_mod.Graph(mem))
    assert "no primary source" in output.lower()


def test_appendix_a_does_not_mark_a_claim_that_has_one(
    mem, mkhypothesis, mkcitation
):
    first = mkcitation(url="https://a-example.com/p", domain="a-example.com",
                       quote="an original span here")["id"]
    mem.update(first, source_type="primary")
    second = mkcitation(url="https://b-example.com/p", domain="b-example.com",
                        quote="a relayed span here")["id"]
    mem.update(second, source_type="secondary")
    mkhypothesis(claim="has a primary", status="supported",
                 supporting=[first, second], confidence=0.67,
                 verdict="supported")
    output = appendices.appendix_a(graph_mod.Graph(mem))
    assert "no primary source" not in output.lower()


def test_unknown_is_not_silently_counted_as_secondary(
    mem, mkhypothesis, mkcitation
):
    """`unknown` means the extractor could not tell. Rolling it into
    `secondary` would state something the run does not know — the
    wording has to be "no primary source identified", not "all
    secondary"."""
    citation = mkcitation(url="https://u-example.com/p",
                          domain="u-example.com",
                          quote="a span of unclear origin")["id"]
    mem.update(citation, source_type="unknown")
    mkhypothesis(claim="unclear", status="supported", supporting=[citation],
                 confidence=0.67, verdict="supported")
    output = appendices.appendix_a(graph_mod.Graph(mem)).lower()
    assert "no primary source" in output
    assert "all secondary" not in output
