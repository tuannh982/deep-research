"""The bibliography and Appendices A-D, emitted straight from the graph.

Spec section 7: "Appendices A-D and the bibliography bypass the LLM
entirely - render.py emits them directly from the graph. There is nothing
in them to hallucinate."

Every appendix is a `description` list rather than a `tabular`. A table of
claims needs p{} columns sized by hand and overflows the moment a claim
runs long; a description list wraps for free and needs no package beyond
the base class.
"""
import gates
import ids as ids_mod
import latex

# An empty `description` environment is a LaTeX error, and a silently
# blank appendix reads as a rendering bug rather than as a true "none".
EMPTY = "\\emph{None.}"


def _url(raw):
    """`\\url{}` when it is safe, escaped monospace when it is not.

    A URL comes from a page we did not write. `\\url` cannot carry a brace
    or a backslash - an unbalanced one breaks the build - so anything
    holding one is escaped and set in monospace instead. Losing the
    hyperlink on a handful of odd URLs beats losing the PDF.
    """
    if any(character in raw for character in "{}\\"):
        return "\\texttt{" + latex.escape(raw) + "}"
    return "\\url{" + raw + "}"


def _published(citation):
    """"Published 2019-03-04" — or "undated", said out loud.

    Stated rather than omitted when absent. A missing clause reads as a
    rendering oversight, and "this source carries no date" is itself a
    fact a reader assessing it needs. Partial values are printed as
    given: the citation schema accepts 2019 and 2019-03 because many
    pages offer no more, and padding one out here would invent precision
    the source never had.
    """
    stated = (citation.get("published_at") or "").strip()
    return f"Published {latex.escape(stated)}" if stated else "Undated"


def _description(items):
    """items is [(term, body)]. Both are already-escaped LaTeX."""
    if not items:
        return EMPTY
    lines = ["\\begin{description}"]
    lines += [f"\\item[{term}] {body}" for term, body in items]
    lines.append("\\end{description}")
    return "\n".join(lines)


def _sorted_nodes(graph, node_type):
    return sorted(graph.readable(node_type),
                  key=lambda pair: ids_mod.numeric(pair[0]))


def _citable(graph):
    """(id, citation) for every citation the report may stand behind.

    Sorted numerically, because `_sorted_nodes` is.
    """
    return [(citation_id, citation)
            for citation_id, citation in _sorted_nodes(graph, "citation")
            if citation["status"] in gates.CITABLE_STATUSES]


def bibliography_ids(graph):
    """Exactly the ids `bibliography` emits a \\bibitem for.

    Shares `_citable` with it rather than restating the filter: render's
    dangling-\\cite check diffs a section body against this list, and a
    second copy of the status rule could drift from the one that decides
    what is really in the document — which is the whole failure it exists
    to catch.
    """
    return [citation_id for citation_id, _ in _citable(graph)]


def bibliography(graph):
    """Every citation the report is allowed to stand behind.

    Deliberately not every citation the run touched - that is Appendix D.
    A `rejected` citation failed gate 2, meaning its quote is not on the
    page, and listing it here would put a source the report cannot stand
    behind in front of the reader as though it could.
    """
    entries = []
    for citation_id, citation in _citable(graph):
        title = latex.escape(citation["title"] or citation["domain"])
        fetched = latex.escape((citation["fetched_at"] or "undated")[:10])
        entries.append(
            f"\\bibitem{{{citation_id}}} \\emph{{{title}}}. "
            f"{latex.escape(citation['domain'])}. {_url(citation['url'])}. "
            # Publication before retrieval: `fetched_at` is written by
            # apply_recheck and records when WE re-read the page, which
            # is the less interesting of the two and was the only one
            # here.
            f"{_published(citation)}. Retrieved {fetched}."
        )
    if not entries:
        return ""
    # The `{99}` argument only sets the label width; it is not a cap.
    return ("\\begin{thebibliography}{99}\n" + "\n".join(entries)
            + "\n\\end{thebibliography}")


# How a claim's standing is put to a reader. `status` is computed by
# code from the evidence as it stands now (apply._verified_status writes
# the promotions, Graph.recompute_confidence re-evaluates `contested` on
# every submit), so it is the honest headline.
STATUS_WORD = {
    "supported": "Supported",
    "contested": "Contested",
    "refuted": "Refuted",
    "proposed": "Open",
}

# What the adversarial check concluded, in words rather than as a token.
# `None` is deliberately NOT folded in with `unsupported`: a claim the
# loop has not reached yet is not a claim that was checked and came back
# negative, and rendering them alike reports work that never happened as
# work that failed.
VERDICT_PHRASE = {
    "supported": "the adversarial check found the quotes established it",
    "unsupported": "the adversarial check found the quotes did not "
                   "establish it",
    "contradicted": "the adversarial check found the quotes argued "
                    "against it",
    None: "no adversarial check has run on it yet",
}


def _side(label, citation_ids):
    """"For: C-001, C-004." — and "Against: none." when a side is empty.

    Printed rather than omitted. Whether anything argues against a claim
    is information about that claim, and a missing label leaves a reader
    unable to tell "nothing does" from "we did not say" — as well as
    reading like a rendering fault.
    """
    listed = (", ".join(latex.escape(c) for c in sorted(citation_ids,
                                                        key=ids_mod.numeric))
              if citation_ids else "none")
    return f"{label}: {listed}."


def _has_primary(graph, hypothesis):
    """True if any citation supporting this claim presents its own work.

    Reads `source_type`, which the extractor reports and nothing gates
    on. Absent counts as not-primary: a citation written before the
    field existed says nothing either way, and claiming a primary source
    the run never identified would be the one thing this disclosure
    exists to avoid.
    """
    supporting = set(hypothesis["supporting"])
    for citation_id, citation in graph.readable("citation"):
        if citation_id in supporting and citation.get("source_type") == "primary":
            return True
    return False


def appendix_a(graph):
    """Every hypothesis, what the adversarial check made of it, and the
    evidence on both sides. Including refuted ones.

    A refuted hypothesis is kept out of the body prose and reported here.
    That is the difference between declining to narrate a refuted claim as
    a finding and losing it.

    The confidence score is deliberately absent. It is
    `base * spread * weight` from confidence.py — a promotion threshold
    and an input to the `min_hypothesis_confidence` stop predicate, both
    of which still read it off the node. It is not a probability: it
    saturates at 0.96, and the modal promoted claim (3 verified citations
    across 2 registrable domains, verdict `supported`) scores exactly
    0.60. Printed bare beside a claim, with nothing in the document
    defining it, a reader reads 0.60 as "60% likely". The verdict, the
    verifier's own reasoning and the citation ids say what that number
    was standing in for, and can be checked.
    """
    items = []
    for hypothesis_id, hypothesis in _sorted_nodes(graph, "hypothesis"):
        status = STATUS_WORD.get(hypothesis["status"], hypothesis["status"])
        verdict = VERDICT_PHRASE.get(
            hypothesis["verdict"], "the adversarial check's result is not "
                                   "recorded")
        # No \hfill. It right-aligned the status to the margin, which read
        # well when the status was the last thing in the entry and badly
        # once the reasoning and the two evidence lists followed it — a
        # word stranded at the right edge with a paragraph under it.
        # `---`, never a literal em-dash: tectonic silently drops U+2014
        # even under the template's utf8/T1 preamble.
        # "Open: the adversarial check found the quotes established it"
        # is a flat contradiction to anyone who has not read
        # apply._verified_status. Both halves are true — the verifier
        # agreed, and the claim still did not clear the bar, because a
        # supported verdict promotes only when the score also reaches
        # promotion_threshold (and a provenance cascade can demote one
        # afterwards). Left unexplained the appendix looks broken at the
        # moment a reader starts checking it. Deliberately does not name
        # WHICH of the two reasons: this function has no cfg, and
        # guessing the gate would be worse than conceding the outcome.
        unpromoted = (hypothesis["status"] == "proposed"
                      and hypothesis["verdict"] == "supported")
        concession = (", but the claim has not been promoted on the "
                      "evidence gathered so far") if unpromoted else ""
        body = [
            f"{latex.escape(hypothesis['claim'])} --- "
            f"\\emph{{{latex.escape(status)}}}: {latex.escape(verdict)}"
            f"{concession}."
        ]
        # Absent on a hypothesis written before the field existed, and on
        # any claim no verdict has landed for. Omitted rather than
        # rendered as an empty quotation.
        reasoning = hypothesis.get("verdict_reasoning")
        if reasoning:
            body.append(f"\\emph{{Verifier:}} ``{latex.escape(reasoning)}''")
        # "Against: none" read identically whether nobody looked or
        # nobody found anything. Those are not the same fact about a
        # claim — the second is a result, the first is a gap — and until
        # refute searches existed only the first was ever true, so the
        # conflation cost nothing. Now it would be the most misleading
        # line in the document.
        if hypothesis["counter"]:
            against = _side("Against", hypothesis["counter"])
        elif graph.was_challenged(hypothesis_id):
            against = "Against: searched for, none found."
        else:
            against = "Against: not searched for."
        body.append(_side("For", hypothesis["supporting"]) + " " + against)
        # Gate 3 counts distinct registrable domains, and two of them can
        # still be one source — a syndicated release, or two posts citing
        # one paper. Detecting that needs origin identification, which is
        # fuzzy matching over model-reported attributions and wrong too
        # often for promotion to rest on; and gating on `primary` fails
        # the other way, because a question whose honest literature is
        # all secondary would never promote anything. So this discloses
        # what the run does know and leaves the judgement to the reader.
        #
        # "no primary source identified", never "all secondary":
        # `unknown` means the extractor could not tell, and folding it
        # into `secondary` would state something the run does not know.
        if hypothesis["supporting"] and not _has_primary(graph, hypothesis):
            body.append("\\emph{No primary source identified: every source "
                        "carrying this claim relays work done elsewhere, or "
                        "does not say.}")
        items.append((latex.escape(hypothesis_id), " ".join(body)))
    return ("\\section{Hypotheses and the evidence for them}\n"
            + _description(items))


def appendix_b(graph):
    """Refuted assumptions, and what each one took down with it."""
    items = []
    for assumption_id, assumption in _sorted_nodes(graph, "assumption"):
        if assumption["status"] != "refuted":
            continue
        refuted_by = assumption["refuted_by"] or "an unrecorded finding"
        blocks = assumption["blocks"]
        took_down = (", ".join(latex.escape(b) for b in blocks)
                     if blocks else "nothing else")
        items.append((
            latex.escape(assumption_id),
            f"{latex.escape(assumption['statement'])} Refuted by "
            f"{latex.escape(refuted_by)}; work resting on it: {took_down}."
        ))
    return "\\section{Refuted assumptions}\n" + _description(items)


def _stopped(text):
    """A task question, terminated so the verdict after it is a new sentence.

    A `question` is only a question by convention. The ones a decomposer
    writes end in "?", but apply_outline names a section writer's task
    "write section S-001: <title>" and ensure_hypothesize_tasks writes
    "Form candidate claims from the N facts gathered under: ...". Both run
    straight into the italic verdict that follows, so the entry read as
    one sentence: "...scatters sunlight Abandoned: 3 attempts."
    """
    stripped = (text or "").strip()
    if not stripped or stripped[-1] in ".?!:;":
        return stripped
    return stripped + "."


def appendix_c(graph, accepted):
    """Open questions: what the run could not resolve.

    Four sources, because there are four ways a line of enquiry ends
    without an answer, and a report that shows only one of them overstates
    how complete it is.
    """
    items = []
    for task_id, task in _sorted_nodes(graph, "task"):
        if task["status"] != "abandoned":
            continue
        reason = task.get("abandoned_reason") or "no reason recorded"
        items.append((latex.escape(task_id),
                      f"{latex.escape(_stopped(task['question']))} "
                      f"\\emph{{Abandoned: {latex.escape(reason)}.}}"))

    for task_id in graph.undispatchable():
        question = graph.tasks.get(task_id, {}).get("question", "")
        items.append((latex.escape(task_id),
                      f"{latex.escape(_stopped(question))} "
                      "\\emph{Never dispatchable --- its dependencies could "
                      "not be satisfied.}"))

    orphans = (accepted.get("orphans") or {})
    # Compound key, not plain ids_mod.numeric: this list concatenates
    # hypothesis and fact ids, and a purely numeric key would interleave
    # F-002 and H-002 arbitrarily. Prefix first groups by type, then the
    # integer orders numerically within each group (H-999 before H-1000).
    stranded = sorted(orphans.get("hypotheses", []) + orphans.get("facts", []),
                      key=lambda node_id: (node_id.split("-")[0],
                                            ids_mod.numeric(node_id)))
    if stranded:
        items.append((
            "Unplaced findings",
            "These could not be attributed to any theme, because the task "
            "that raised them is missing or unreadable: "
            + ", ".join(latex.escape(node_id) for node_id in stranded) + "."))

    for theme in accepted.get("empty_themes") or []:
        question = graph.tasks.get(theme, {}).get("question", "")
        if not question:
            continue
        items.append((latex.escape(theme),
                      f"{latex.escape(_stopped(question))} \\emph{{This line "
                      "of enquiry produced no findings.}"))

    return "\\section{Open questions}\n" + _description(items)


def appendix_d(graph):
    """Source inventory - every citation the run touched, flagged.

    Spec section 6's promise that an unreadable source is "flagged rather
    than silently trusted" is kept here, and spec's open risks section
    names this appendix as the mitigation for gate 2's coverage gap. So it
    reports rejected and pending citations too, which the bibliography
    does not.
    """
    items = []
    for citation_id, citation in _sorted_nodes(graph, "citation"):
        status = citation["status"]
        if status == "verified":
            note = "quote confirmed by an independent re-check"
        elif status == "unverifiable":
            # `---`, never a literal em-dash. tectonic silently DROPS a
            # U+2014 even under the template's utf8/T1 preamble — measured
            # against 0.17.0 — so this sentence shipped as "not
            # independently verified the re-check agent could not read".
            note = ("\\textbf{not independently verified} --- the re-check "
                    "agent could not read the page")
        elif status == "rejected":
            note = ("\\textbf{rejected} --- the quote was not found on the "
                    "page; nothing in this report rests on it")
        else:
            note = "\\textbf{not yet checked}"
        fetched = latex.escape((citation["fetched_at"] or "undated")[:10])
        items.append((
            latex.escape(citation_id),
            f"{latex.escape(citation['domain'])}. {_url(citation['url'])}. "
            f"{_published(citation)}. Retrieved {fetched}. {note}."
        ))
    return "\\section{Source inventory}\n" + _description(items)


def appendix_e(graph):
    """Search queries - what this run actually looked for.

    The reproducibility record. Nothing recorded a run's queries before
    this, so no part of its literature search could be re-run or judged
    for breadth - and the saturation halt was uninterpretable with it,
    because six dry tasks might mean an exhausted question or might mean
    a monoculture of queries, and there was no way to tell.

    AS REPORTED, said out loud in the appendix itself. Nothing in this
    process observes the WebSearch call; the searcher tells us what it
    says it sent. An appendix whose whole purpose is letting someone
    else repeat the work would be worse than useless if it overstated
    that.

    A refute search is marked as one. Searching for a claim's disproof
    is a different act from searching for its support, and a reader
    assessing coverage has to see which was which.
    """
    items = []
    for task_id, task in _sorted_nodes(graph, "task"):
        queries = task.get("queries")
        if not queries:
            continue
        against = ((task.get("inputs") or {}).get("stance")) == "against"
        # A marker, not a prefix. ensure_refute_tasks writes the question
        # as "Find evidence that would show this claim is false: ...", so
        # leading with "searching for the disproof of" produced "the
        # disproof of: ... show this claim is false" — the same thing
        # said twice in one line.
        marker = " \\emph{(searching for disproof)}" if against else ""
        listed = "; ".join(f"``{latex.escape(query)}''" for query in queries)
        items.append((
            latex.escape(task_id),
            f"{latex.escape(_stopped(task['question']))}{marker} "
            f"\\emph{{Queries:}} {listed}"
        ))
    return ("\\section{Search queries}\n"
            "These are the queries each search reports having issued, as "
            "reported by the searcher --- nothing in this run observes the "
            "search itself.\n\n" + _description(items))


def render_all(graph, accepted):
    """A, B, C, D, E in order, as one LaTeX string."""
    return "\n\n".join([
        appendix_a(graph),
        appendix_b(graph),
        appendix_c(graph, accepted),
        appendix_d(graph),
        appendix_e(graph),
    ])
