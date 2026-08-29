"""Assemble report.tex from the graph, the outline and the section bodies.

Spec section 7. Everything here is code: the bibliography, all four
appendices, the Introduction and the Limitations. The only model-written
text in the document is the theme section bodies and the Synthesis, and
both of those were escaped by apply_synthesize before they reached
sections/ and are inserted verbatim here.

Substitution is str.replace on %%MARKER%% tokens, never str.format: a
LaTeX preamble is full of braces and every one would need doubling.
"""
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import appendices
import atomicio
import latex
import memory as memory_mod
import outline as outline_mod
import runconfig
import workspace
from graph import Graph

MARKERS = ("%%TITLE%%", "%%DATE%%", "%%INTRODUCTION%%", "%%SECTIONS%%",
           "%%SYNTHESIS%%", "%%LIMITATIONS%%", "%%BIBLIOGRAPHY%%",
           "%%APPENDICES%%")


class RenderError(ValueError):
    """The report cannot be assembled from what is on disk."""


def template_path():
    return workspace.skill_dir() / "templates" / "report.tex"


def load_outline(root):
    path = Path(root) / "out" / outline_mod.PATH_NAME
    if not path.is_file():
        raise RenderError(
            f"no accepted outline at {path}; run `research synthesize` and "
            "complete the loop before rendering"
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RenderError(f"{path} is not readable JSON: {error}") from None


def _body(root, section_id):
    path = Path(root) / "sections" / f"{section_id}.tex"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip()


def _bullets(heading, items):
    if not items:
        return ""
    lines = [heading, "\\begin{itemize}"]
    lines += [f"\\item {item}" for item in items]
    lines.append("\\end{itemize}")
    return "\n".join(lines)


def introduction(cfg, accepted):
    """The question and the agreed scope. Nothing to hallucinate, so no
    model is asked for it."""
    parts = [
        "This report answers the question: \\emph{"
        + latex.escape(cfg["question"]) + "}"
    ]
    scope = cfg.get("scope") or {}
    for key, heading in (("in_scope", "Within scope:"),
                         ("out_of_scope", "Out of scope:"),
                         ("success_criteria", "Success criteria:")):
        block = _bullets(heading, [latex.escape(item)
                                   for item in scope.get(key) or []])
        if block:
            parts.append(block)
    count = len(accepted.get("sections") or [])
    # This sentence has now overstated the run three times.
    #
    # It claimed every citation's "quoted span was re-fetched and
    # confirmed". gates.CITABLE_STATUSES is ("verified", "unverifiable"),
    # and an `unverifiable` citation is precisely one whose span could NOT
    # be confirmed — kept deliberately, per spec section 6, and flagged in
    # Appendix D. So the Introduction contradicted this same document's
    # Limitations and Appendix D on every run that ever met a login wall.
    #
    # And it promised a fact identifier "resolving to Appendix A". A
    # \factref renders as a superscript F-id, and no appendix lists facts:
    # A is hypotheses, B assumptions, C open questions, D citations. The
    # promise was unredeemable by anyone holding the PDF. Reworded rather
    # than answered with a fifth appendix — the id stays traceable in the
    # graph, which is where a fact's provenance actually lives.
    #
    # And it claimed "every claim carries" a citation or a fact id. Gate 5
    # (latex.unsourced_numerics) only requires one for a sentence stating a
    # figure — a purely qualitative sentence passes ungated. "Every claim"
    # promised sourcing the pipeline never enforced. The recurrence across
    # three fixes is the point: this paragraph is where the report tells a
    # reader how much to trust the rest of it, so a claim here that outruns
    # what a gate actually checks is the same defect the gates themselves
    # exist to catch, just relocated to the one paragraph no gate reads.
    #
    # And, fourth: it claimed the guarantee for the whole document. Gate 5
    # is `gates.report_section`, and `assemble` only ever runs it over a
    # section BODY. Limitations is generated here, ungated, and emits "N
    # hypothesis(es) remain below the promotion threshold of 0.6" with
    # neither a \cite nor a \factref; the appendices do the same. The harm
    # is small — those are self-evidently statements about this run, not
    # about the world — but an unqualified guarantee that the report
    # itself breaks four paragraphs later is exactly the pattern above.
    # Scoped to what gate 5 actually reads, with the exemption named
    # rather than left for the reader to notice.
    parts.append(
        f"The findings are organised into {count} "
        + ("theme" if count == 1 else "themes")
        + ", followed by a cross-cutting synthesis. In those themed "
        "sections and the synthesis, every sentence stating a figure "
        "carries either a citation to a source listed in Appendix D, "
        "where any source that could not be independently re-read is "
        "flagged, or a fact identifier naming the extracted fact it rests "
        "on; a gate checks this before a section is accepted. Sentences "
        "that state no figure are the report's own connective prose. The "
        "Limitations section and the appendices are not gated that way "
        "and carry uncited figures: they count this run's own graph --- "
        "how many claims fell short, how many sources could not be "
        "re-read --- rather than stating anything about the world."
    )
    return "\n\n".join(parts)


def limitations(graph, cfg, accepted, *, placeholders=()):
    """What this run could not establish. Generated, never written.

    A model asked to characterise its own run's weaknesses is a model asked
    to soften them, and spec's open-risks section names exactly the things
    that must be said out loud here.

    `placeholders` is the section ids `assemble` typeset a placeholder for.
    Threaded in rather than re-derived from the graph because "abandoned
    writer" and "unwritten section" are NOT the same set: `reopen_section`
    marks a writer stale without deleting the .tex it already produced, so
    a writer abandoned on the retries can leave a section with perfectly
    good prose in it. Counting those would put another false sentence in
    the one part of the report whose job is not to overstate.
    """
    items = []

    unreadable = sorted(
        citation_id for citation_id, citation in graph.readable("citation")
        if citation["status"] == "unverifiable")
    if unreadable:
        items.append(
            f"{len(unreadable)} cited source(s) could not be independently "
            # `---`, never a literal em-dash: tectonic silently drops a
            # U+2014 even under the template's utf8/T1 preamble.
            "re-checked --- typically a login wall, a bot block, or a page "
            "that renders only under JavaScript. Claims resting on them are "
            "marked in Appendix D and should be read as resting on a source "
            "nobody could open a second time."
        )

    # A promoted claim is supposed to have faced a search for its own
    # disproof — coverage will not halt otherwise. But a run can still
    # end with one unchallenged: stopped by signal before the challenge
    # ran, or with the refute task abandoned after max_attempts. "N
    # claims were challenged and survived" and "N were never challenged"
    # are materially different reports, and this is the section that has
    # to say which one this is.
    unchallenged = sorted(
        hypothesis_id for hypothesis_id, hypothesis
        in graph.readable("hypothesis")
        if hypothesis["status"] in ("supported", "contested")
        and not graph.was_challenged(hypothesis_id))
    if unchallenged:
        items.append(
            f"{len(unchallenged)} claim(s) the report stands behind were "
            "never challenged --- no search for evidence against them "
            "completed before this run ended. They are marked in "
            "Appendix A, and rest only on the evidence gathered in their "
            "favour."
        )

    threshold = cfg["config"]["promotion_threshold"]
    thin = sorted(
        hypothesis_id for hypothesis_id, hypothesis
        in graph.readable("hypothesis")
        if hypothesis["status"] != "refuted"
        and hypothesis["confidence"] < threshold)
    if thin:
        # The COUNT is computed from the confidence score and the sentence
        # no longer names it. That is deliberate, not an oversight: the
        # score is a gating constant doing its internal job, and this
        # sentence was the last place the report leaked the arithmetic to
        # a reader who has been shown no other number and given no
        # formula. Appendix A carries the verdict and the evidence now,
        # so "with their scores" would also be a promise the document
        # does not keep.
        items.append(
            f"{len(thin)} hypothesis(es) did not reach the evidence bar "
            "this run required. They are listed in Appendix A with what "
            "the adversarial check made of them, and should be read as "
            "open questions rather than findings."
        )

    # Filtered by kind. A `synthesize` task writes prose from evidence
    # already gathered and an `outline` task arranges sections; neither
    # researches anything, so counting either as a "line of enquiry"
    # inflates the number of real dead ends — in the one section of the
    # report whose whole purpose is not to overstate the run. Keyed on
    # outline.MACHINERY_KINDS rather than a literal "synthesize" so it
    # cannot drift: those two are exactly the report-production half of
    # task.json's kind enum, against decompose/search/extract/hypothesize/
    # verify, all five of which genuinely advance the research.
    #
    # Only reachable since an abandoned writer started rendering at all.
    # Before that `assemble` raised and no PDF shipped, so this sentence
    # could never appear in a document alongside one.
    abandoned = sorted(
        task_id for task_id, task in graph.readable("task")
        if task["status"] == "abandoned"
        and task["kind"] not in outline_mod.MACHINERY_KINDS)
    if abandoned:
        items.append(
            f"{len(abandoned)} line(s) of enquiry were abandoned after "
            f"{cfg['config']['max_attempts']} failed attempts and are listed "
            "in Appendix C."
        )

    # Excluded from the count above, but not hidden: a reader judging how
    # complete this report is has to learn that a chapter carries a
    # placeholder instead of prose. Says nothing about the evidence, which
    # is unaffected and still in the appendices.
    if placeholders:
        items.append(
            f"{len(placeholders)} section(s) of this report could not be "
            "written --- the writer was abandoned after repeated failures, so "
            "the body carries a placeholder where that prose should be, and "
            "the task is listed in Appendix C; the hypotheses and sources "
            "the section rested on are unaffected and remain in Appendices "
            "A and D."
        )

    empty = accepted.get("empty_themes") or []
    if empty:
        items.append(
            f"{len(empty)} theme(s) produced no findings at all; see "
            "Appendix C."
        )

    # Always true, and always worth saying: the loop has no budget
    # condition, so breadth is bounded only by the depth cap and by the
    # operator's stop signal. A reader deserves to know the run stopped
    # when someone said so, not when the question was exhausted.
    items.append(
        "This run had no budget condition. Breadth was bounded only by the "
        f"depth cap of {cfg['config']['max_depth']} and by the halt "
        "condition that ended it, so absence of a finding here is not "
        "evidence that none exists."
    )
    return _bullets("", items)


def _writers(graph, section_id):
    """Every synthesize task that owns a section's body."""
    return [task for _, task in graph.readable("task")
            if task["kind"] == "synthesize"
            and ((task.get("inputs") or {}).get("section")
                 or {}).get("id") == section_id]


def _abandoned_body(graph, section_id):
    """A placeholder for a section no writer will ever produce, or None.

    A synthesize task that fails gate 5 max_attempts times is abandoned by
    submit._fail. `abandoned` is not in Graph.OPEN_TASK_STATUSES, so it
    never re-enters the frontier: `research next` reports nothing to
    dispatch and, before this, `assemble` raised unconditionally — so a
    run with nineteen good sections and one stubborn writer produced no
    PDF and no report.tex at all. Spec section 7 asks for the tex "rather
    than nothing" and section 4 for a loop that "never blocks on a task it
    cannot complete"; this is both.

    Only `abandoned` earns a placeholder, not merely "not open". The
    sentence below states as fact that the writer was abandoned after N
    attempts, and emitting it for a `done` writer whose file happened to
    go missing would put a false claim into the report — the failure mode
    this whole surface exists to avoid. Every other state still raises.
    """
    writers = _writers(graph, section_id)
    if not writers or any(task["status"] != "abandoned" for task in writers):
        return None
    attempts = max(task.get("attempts") or 0 for task in writers)
    return ("\\emph{This section could not be written; the writer was "
            f"abandoned after {attempts} attempt"
            + ("" if attempts == 1 else "s")
            + " --- see Appendix C.}")


def _dangling_cites(graph, body):
    """Cite keys in a body that the bibliography will not emit.

    Gate 5 resolves cite keys against the live graph at SUBMIT time, which
    is correct and is not enough: the graph keeps moving underneath an
    accepted section. This used to say "submit runs ensure_evidence_tasks
    on every submit, so research carries on alongside the writers", and
    the synthesis freeze falsified that — step 4 is skipped for the whole
    `synthesize` phase, so no NEW research is scheduled once the outline
    is frozen. The case is still reachable by the tasks that were already
    outstanding when it froze: the phase gate is on follow-on scheduling,
    not on the frontier, so a `recheck` seeded before the freeze is still
    dispatched, and applying its artifact flips a citation to `rejected`
    after a section citing it was accepted. An `apply_verify` refutation
    lands the same way, via the cascade that quarantines the facts.
    `bibliography` then drops the citation while the body still carries
    the \\cite.

    So the danger here is the opposite of a dead guard. A maintainer who
    reads a premise that is no longer true, checks it, and finds it false
    concludes the case cannot occur and deletes a guard that still fires.
    tests/test_submit.py::test_an_outstanding_recheck_can_still_reject_a_citation_mid_synthesis
    pins the reachability so the reason above stays checkable.

    That combination compiles. Measured against tectonic 0.17.0: a
    \\cite{C-004} with no matching \\bibitem exits 0 and writes a PDF —
    carrying a `[?]` where the reference should be, while Appendix D says
    of that same citation "nothing in this report rests on it". Untrue,
    and code generated it.

    Reads the escaped body straight from sections/: `latex.escape` leaves
    \\cite{} spans verbatim, which is exactly why it can be parsed here.
    """
    emitted = set(appendices.bibliography_ids(graph))
    return sorted(set(latex.cite_keys(body)) - emitted)


def assemble(root, graph, cfg, *, today=None, memory=None):
    """The complete report.tex. Raises RenderError if anything is missing.

    `memory` is optional and write-only: when a section is found citing a
    source the bibliography no longer carries, it is what re-opens that
    section's writer so the loop can rewrite it. Without it the same
    RenderError is raised, just with no recovery scheduled.
    """
    today = today or memory_mod.utcnow()[:10]
    accepted = load_outline(root)

    missing, rendered, dangling, placeholders = [], [], {}, []

    def resolve(section_id):
        """The body to typeset, or None if this section blocks the build."""
        body = _body(root, section_id)
        if body is None:
            body = _abandoned_body(graph, section_id)
            if body is not None:
                # Recorded here, where it is known, rather than re-derived
                # from the graph in `limitations`. A writer can be
                # abandoned with its .tex still on disk from an earlier
                # accepted attempt, and that section is NOT unwritten.
                placeholders.append(section_id)
            return body
        bad = _dangling_cites(graph, body)
        if bad:
            dangling[section_id] = bad
        return body

    for section in accepted["sections"]:
        body = resolve(section["id"])
        if body is None:
            missing.append(section["id"])
            continue
        # The heading comes from the VALIDATED title, never from the
        # model's body. artifact.synthesize forbids a \section in the body
        # for this reason: a synthesizer must not be able to retitle its
        # own section after the outline was validated.
        rendered.append("\\section{" + latex.escape(section["title"]) + "}\n\n"
                        + body)

    synthesis = resolve(outline_mod.SYNTHESIS_SECTION_ID)
    if synthesis is None:
        missing.append(outline_mod.SYNTHESIS_SECTION_ID)

    if missing:
        # Every one at once: naming them one at a time costs a full loop
        # round-trip per missing file.
        raise RenderError(
            "these sections have not been written yet: "
            + ", ".join(sorted(missing))
            + ". Run `research next` and `research submit` until the loop "
            "halts, then render again."
        )

    if dangling:
        # Re-opened BEFORE raising, and every one of them, so a single
        # `research next` picks up all the rewrites at once. Both
        # mechanisms already existed; nothing here is a new escape hatch.
        for section_id in sorted(dangling):
            if memory is not None:
                reopen_section(
                    memory, root, section_id,
                    "cites " + ", ".join(dangling[section_id])
                    + ", which is no longer in the bibliography; drop the "
                    "claim or re-evidence it")
        raise RenderError(
            "these sections cite sources the bibliography no longer "
            "carries, so the PDF would ship a dangling reference: "
            + "; ".join(f"{section_id} cites "
                        + ", ".join(dangling[section_id])
                        for section_id in sorted(dangling))
            + ". Those citations were rejected after the section was "
            "accepted. The writers have been re-opened; run `research "
            "next` and `research submit`, then render again."
        )

    document = template_path().read_text(encoding="utf-8")
    for marker, value in (
        ("%%TITLE%%", latex.escape(cfg["question"])),
        ("%%DATE%%", latex.escape(today)),
        ("%%INTRODUCTION%%", introduction(cfg, accepted)),
        ("%%SECTIONS%%", "\n\n".join(rendered)),
        ("%%SYNTHESIS%%", synthesis),
        ("%%LIMITATIONS%%", limitations(graph, cfg, accepted,
                                        placeholders=sorted(placeholders))),
        ("%%BIBLIOGRAPHY%%", appendices.bibliography(graph)),
        ("%%APPENDICES%%", appendices.render_all(graph, accepted)),
    ):
        document = document.replace(marker, value)
    return document


# A LaTeX run that has not finished in three minutes is not going to.
TECTONIC_TIMEOUT = 180

# tectonic reports the failing line as "--- line 42 of report.tex ---".
_LINE = re.compile(r"line (\d+) of ", re.IGNORECASE)


@dataclass
class BuildResult:
    ok: bool
    tex: object
    pdf: object = None
    error: str = ""
    offending_line: str = ""
    section: str = None


def _tectonic_run(tex_path, out_dir):
    """The real invocation. Replaced wholesale in tests.

    Resolved inside `build`, never bound as a default in its signature: a
    default captures the function object at import time, so
    monkeypatch.setattr on this name would have no effect on a call that
    omitted the parameter. That exact bug shipped in workspace.preflight.
    """
    return subprocess.run(
        ["tectonic", "--keep-logs", "--outdir", str(out_dir), str(tex_path)],
        capture_output=True, text=True, timeout=TECTONIC_TIMEOUT, check=False,
    )


def _blame(error_text, accepted):
    """The section id a build error points at, or None.

    Best-effort by design. The line number tectonic reports is a line in
    the ASSEMBLED document, not in any section file, and mapping one to
    the other would mean tracking offsets through every substitution. A
    section id appearing in the error text — which it does whenever the
    failure is inside a \\cite or a \\factref — is both simpler and more
    often right. When nothing can be blamed, spec section 7's fallback
    applies: emit the tex and the build report rather than nothing.
    """
    for section in accepted.get("sections") or []:
        if section["id"] in error_text:
            return section["id"]
    return None


def build_report(result, accepted):
    lines = [
        "# Build report",
        "",
        "`tectonic` could not compile the assembled report.",
        "",
        f"- Offending line: {result.offending_line or 'not reported'}",
        f"- Blamed section: {result.section or 'could not be determined'}",
        "",
        "## What to do",
        "",
        "The assembled source is at `out/report.tex` and is complete — "
        "nothing was lost. If a section was blamed, its writer has been "
        "re-opened; run `research next` and `research submit`, then "
        "`research render` again. Otherwise the fault is in the template "
        "or in an appendix, and the error below is the place to start.",
        "",
        "## tectonic output",
        "",
        "```",
        result.error.strip() or "(no output)",
        "```",
        "",
    ]
    return "\n".join(lines)


def reopen_section(memory, root, section_id, error):
    """Mark a section's writer stale with the build error attached.

    memory.update, never create_task. TASK_KEY includes
    canonical(node.get("inputs")), so adding a build_error key changes the
    natural key and create_task would seed a SECOND synthesize task for
    the same section — two writers competing to produce one .tex file.
    Updating in place keeps `attempts` counting, which is what bounds the
    retries at max_attempts with no new counter.
    """
    for task_id in memory.ids("task"):
        try:
            task = memory.read(task_id)
        except Exception:
            continue
        inputs = task.get("inputs") or {}
        if task.get("kind") != "synthesize":
            continue
        if (inputs.get("section") or {}).get("id") != section_id:
            continue
        memory.update(task_id, status="stale",
                      inputs={**inputs, "build_error": error})
        return task_id
    return None


def build(root, graph, cfg, *, run=None, today=None, memory=None):
    """Assemble, compile, and on failure leave everything a human needs.

    Spec section 7: "If that fails, the run emits report.tex plus a build
    report rather than nothing." So the tex is written BEFORE tectonic is
    invoked, not after it succeeds.
    """
    run = run or _tectonic_run
    accepted = load_outline(root)
    out_dir = Path(root) / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / "report.tex"

    atomicio.write_text(
        tex_path, assemble(root, graph, cfg, today=today, memory=memory))

    completed = run(tex_path, out_dir)
    if completed.returncode == 0:
        pdf = out_dir / "report.pdf"
        if pdf.is_file():
            return BuildResult(ok=True, tex=tex_path, pdf=pdf)
        # tectonic reported success and produced nothing. Treated as a
        # failure because the caller's contract is a PDF, not an exit code.
        return BuildResult(
            ok=False, tex=tex_path,
            error="tectonic exited 0 but wrote no PDF")

    error_text = (completed.stderr or "") + (completed.stdout or "")
    match = _LINE.search(error_text)
    result = BuildResult(
        ok=False, tex=tex_path, error=error_text,
        offending_line=match.group(0) if match else "",
        section=_blame(error_text, accepted),
    )
    atomicio.write_text(out_dir / "build-report.md",
                        build_report(result, accepted))
    if result.section and memory is not None:
        reopen_section(memory, root, result.section, error_text.strip())
    return result


HELP = "assemble report.tex and build the PDF"


def add_arguments(parser):
    parser.add_argument(
        "--tex-only", action="store_true",
        help="assemble out/report.tex without invoking tectonic",
    )


def run(args):
    root = workspace.require(args.root)
    cfg = runconfig.load(root)
    memory = memory_mod.Memory(root)
    graph = Graph(memory, max_depth=cfg["config"]["max_depth"],
                  promotion_threshold=cfg["config"]["promotion_threshold"],
                  required_domains=cfg["config"]["required_domains"])

    if not args.tex_only and cfg["preflight"]["tectonic"] != "present":
        # A run started with --allow-missing-tectonic reaches here
        # legitimately. Say so plainly rather than dying inside subprocess
        # with FileNotFoundError.
        print(
            "error: tectonic is not installed, so the PDF cannot be built. "
            "Install it (`brew install tectonic`), then re-run. To produce "
            "the LaTeX source alone, use `research render --tex-only`.",
            file=sys.stderr,
        )
        return 1

    try:
        if args.tex_only:
            # `memory` here too: --tex-only took the same unrenderable
            # path as a full build, so it must get the same recovery. A
            # dangling \cite needs the section rewritten whether or not
            # tectonic was going to be invoked.
            document = assemble(root, graph, cfg, memory=memory)
            path = Path(root) / "out" / "report.tex"
            atomicio.write_text(path, document)
            print(f"wrote {path}")
            return 0
        result = build(root, graph, cfg, memory=memory)
    except RenderError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if not result.ok:
        print(f"error: the build failed. The assembled source is at "
              f"{result.tex} and the diagnosis is in "
              f"{Path(root) / 'out' / 'build-report.md'}.",
              file=sys.stderr)
        if result.section:
            print(f"       {result.section} has been re-opened; run "
                  "`research next`, `research submit`, then render again.",
                  file=sys.stderr)
        return 1

    cfg["status"]["phase"] = "done"
    runconfig.save(root, cfg)
    print(f"wrote {result.pdf}")
    print(f"  source   {result.tex}")
    return 0
