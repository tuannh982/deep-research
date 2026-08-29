"""The whole pipeline, driven through research.main exactly as an operator
would drive it. Every subagent is replaced by a canned artifact written to
the inbox; nothing else is stubbed except the network and tectonic."""
import json

import pytest

import graph as graph_mod
import memory as memory_mod
import outline
import render
import research
import runconfig
import stubs


def cli(*argv):
    """research.main, with its exit code. The real dispatch table."""
    return research.main(list(argv))


def artifact(root, task_id, payload):
    (root / "inbox" / f"{task_id}.json").write_text(
        json.dumps(payload), encoding="utf-8")


def tasks_of_kind(store, kind):
    return sorted(t for t in store.ids("task")
                  if store.read(t)["kind"] == kind
                  and store.read(t)["status"] in ("pending", "ready", "stale"))


@pytest.fixture
def run_root(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "_tectonic_run", stubs.tectonic_stub())
    return tmp_path / "research"


def test_a_canned_question_produces_a_pdf(run_root, monkeypatch):
    root = run_root
    monkeypatch.setattr("workspace.shutil.which", lambda name: f"/usr/bin/{name}")

    assert cli("init", "why is the sky blue?", "--root", str(root)) == 0
    # The scoping step, standing in for the research-brainstorming skill.
    # SKILL.md's "Before the loop" puts it between `init` and the first
    # tick, and scheduler.run refuses tick 1 without it — so a pipeline
    # test that skipped it would no longer be driving the pipeline as an
    # operator drives it, which is this module's whole premise.
    cfg = runconfig.load(root)
    cfg["scope"]["in_scope"] = ["how sunlight scatters in the atmosphere"]
    runconfig.save(root, cfg)
    store = memory_mod.Memory(root)

    # --- tick 1: the decomposer answers the seeded root task -----------
    # Captured BEFORE `next`, which moves the dispatched task to `running`
    # and out of tasks_of_kind's (pending, ready, stale) filter — exactly
    # the pattern the outline/synthesize lookups below already use.
    root_task = tasks_of_kind(store, "decompose")[0]
    assert cli("next", "--root", str(root)) == 0
    # The real artifact.decompose shape: `children`, not `subquestions`, and
    # each child needs question/kind/rationale/depends_on_index. `kind` is
    # restricted to decompose|search — the decomposer may not conjure an
    # extract or verify task, because nothing would feed it.
    artifact(root, root_task, {
        "task_id": root_task,
        "children": [{"question": "how does scattering work?",
                      "kind": "search",
                      "rationale": "the mechanism is the core of the answer",
                      "depends_on_index": []}],
        "assumptions": [],
    })
    assert cli("submit", "--root", str(root), "--tick", "1") == 0

    # --- the search and extract steps are hand-fed -----------------------
    # A real run reaches these through the searcher and extractor; the
    # point of this test is the SHAPE of the pipeline, so the graph is
    # advanced directly to the state synthesis needs.
    theme = tasks_of_kind(store, "search")[0]
    citation = store.create("citation", {
        "url": "https://a-example.com/scattering", "domain": "a-example.com",
        "title": "Rayleigh scattering", "quote": "short wavelengths scatter",
        "quote_sha256": "0" * 64, "fetched_at": "2026-08-22T10:00:00Z",
        "http_status": 200, "status": "verified",
        "provenance": {"task": theme, "agent": "extractor"}})["id"]
    store.create("fact", {
        "statement": "short wavelengths scatter more", "citations": [citation],
        "status": "active",
        "provenance": {"task": theme, "agent": "extractor"}})
    store.create("hypothesis", {
        "claim": "Rayleigh scattering explains the blue sky",
        "supporting": [citation], "counter": [], "status": "supported",
        "confidence": 0.75, "verdict": "supported",
        "provenance": {"task": theme, "agent": "hypothesizer"}})
    for task_id in store.ids("task"):
        if store.read(task_id)["status"] != "done":
            store.update(task_id, status="done")

    # --- synthesis -------------------------------------------------------
    assert cli("synthesize", "--root", str(root)) == 0
    assert runconfig.load(root)["status"]["phase"] == "synthesize"

    outline_task = tasks_of_kind(store, "outline")[0]
    frozen = store.read(outline_task)["inputs"]["outline"]
    assert cli("next", "--root", str(root)) == 0
    artifact(root, outline_task, {
        "task_id": outline_task,
        "sections": [{"id": s["id"], "title": "How light scatters",
                      "hypotheses": s["hypotheses"], "facts": s["facts"]}
                     for s in frozen["sections"]],
    })
    tick = runconfig.load(root)["status"]["tick"]
    assert cli("submit", "--root", str(root), "--tick", str(tick)) == 0

    # --- the section writers ---------------------------------------------
    writers = tasks_of_kind(store, "synthesize")
    assert len(writers) == 2  # one theme section plus the Synthesis
    assert cli("next", "--root", str(root)) == 0
    for task_id in writers:
        section = store.read(task_id)["inputs"]["section"]
        artifact(root, task_id, {
            "task_id": task_id, "section": section["id"],
            "body": ("Short wavelengths are scattered far more strongly than "
                     "long ones, which is what a ground observer sees as a "
                     "blue sky \\cite{%s}." % citation),
        })
    tick = runconfig.load(root)["status"]["tick"]
    assert cli("submit", "--root", str(root), "--tick", str(tick)) == 0
    assert (root / "sections" / "S-001.tex").is_file()
    assert (root / "sections" / "S-999.tex").is_file()

    # --- render ----------------------------------------------------------
    assert cli("render", "--root", str(root)) == 0

    pdf = root / "out" / "report.pdf"
    assert pdf.read_bytes().startswith(b"%PDF-")
    assert runconfig.load(root)["status"]["phase"] == "done"

    source = (root / "out" / "report.tex").read_text(encoding="utf-8")
    assert "%%" not in source            # every marker substituted
    assert "\\section{How light scatters}" in source
    assert "\\bibitem{%s}" % citation in source
    assert "Source inventory" in source


def test_render_before_synthesis_fails_cleanly(run_root, monkeypatch):
    """The obvious wrong order. It must produce a message, not a traceback."""
    root = run_root
    monkeypatch.setattr("workspace.shutil.which", lambda name: f"/usr/bin/{name}")
    assert cli("init", "q", "--root", str(root)) == 0
    assert cli("render", "--root", str(root)) == 1


def test_the_pipeline_is_idempotent_at_every_step(run_root, monkeypatch):
    """Re-running render on a finished run rewrites the same PDF and
    changes nothing else. `render` is the one command safe to repeat."""
    root = run_root
    monkeypatch.setattr("workspace.shutil.which", lambda name: f"/usr/bin/{name}")
    cli("init", "q", "--root", str(root))
    store = memory_mod.Memory(root)
    accepted = {"question": "q", "sections": [], "empty_themes": [],
                "orphans": {"hypotheses": [], "facts": []}}
    (root / "out" / outline.PATH_NAME).write_text(json.dumps(accepted),
                                                   encoding="utf-8")
    (root / "sections" / "S-999.tex").write_text("Nothing was found.",
                                                  encoding="utf-8")
    assert cli("render", "--root", str(root)) == 0
    first = (root / "out" / "report.tex").read_text(encoding="utf-8")
    assert cli("render", "--root", str(root)) == 0
    assert (root / "out" / "report.tex").read_text(encoding="utf-8") == first


def test_a_run_that_promotes_a_claim_challenges_it_and_still_halts(
    run_root, monkeypatch
):
    """The livelock guard for the refutation requirement.

    `coverage_halt` now refuses while a promoted claim has not faced a
    search for its own disproof, and that predicate has a documented
    history of being unfireable ("13 tasks done, 0 in flight, 6 of 6 dry,
    no halt, forever"). Nothing else in this suite exercises the path,
    because the canned pipeline above lands its hypothesis at 0.17 and
    leaves it `proposed` — never promoted, so never challenged.

    This drives a claim all the way to `supported` (3 verified citations
    across 3 registrable domains, adversarial verdict `supported`),
    answers the refute search that promotion triggers with ZERO sources,
    and requires the run to halt anyway. An empty refutation is the exact
    case that livelocks if it does not count as challenged: the task is
    `done`, create_task would reuse it, and it can never be dispatchable
    again — so nothing further would ever be scheduled.
    """
    root = run_root
    monkeypatch.setattr("workspace.shutil.which", lambda name: f"/usr/bin/{name}")
    assert cli("init", "why is the sky blue?", "--root", str(root)) == 0
    cfg = runconfig.load(root)
    cfg["scope"]["in_scope"] = ["atmospheric optics"]
    runconfig.save(root, cfg)
    store = memory_mod.Memory(root)

    domains = ["a-example.com", "b-example.com", "c-example.com"]
    quote = "short wavelengths scatter more"
    refuted_ids = []

    for _ in range(30):
        cli("next", "--root", str(root))
        if runconfig.load(root)["status"]["halted"]:
            break
        for task_id in sorted(store.ids("task")):
            task = store.read(task_id)
            if task["status"] != "running":
                continue
            inputs = task.get("inputs") or {}
            kind = task["kind"]
            if kind == "decompose":
                artifact(root, task_id, {
                    "task_id": task_id, "assumptions": [],
                    "children": [{"question": "how does scattering work?",
                                  "kind": "search", "rationale": "the core",
                                  "depends_on_index": []}]})
            elif kind == "search" and inputs.get("stance") == "against":
                refuted_ids.append(inputs["for_hypothesis"])
                artifact(root, task_id, {
                    "task_id": task_id, "sources": [],
                    "queries": ["a search query"],
                    "no_sources_reason": "no contrary source exists"})
            elif kind == "search":
                artifact(root, task_id, {
                    "task_id": task_id, "no_sources_reason": None,
                    "queries": ["how does scattering work"],
                    "sources": [{"url": f"https://{d}/p", "title": f"src {d}",
                                 "relevance": 0.9, "why": "w"}
                                for d in domains]})
            elif kind == "extract":
                artifact(root, task_id, {
                    "task_id": task_id, "url": inputs["url"],
                    "published_at": None,
                    "source_type": "primary",
                    "no_facts_reason": None,
                    "facts": [{"statement": f"finding from {inputs['url']}",
                               "quote": quote}]})
            elif kind == "recheck":
                artifact(root, task_id, {
                    "task_id": task_id, "url": inputs["url"], "outcome": "read",
                    "quotes": [{"index": i, "present": True}
                               for i in range(len(inputs["quotes"]))]})
            elif kind == "hypothesize":
                artifact(root, task_id, {
                    "task_id": task_id, "no_hypotheses_reason": None,
                    "hypotheses": [{"claim": "Rayleigh explains the blue sky",
                                    "supporting": sorted(store.ids("citation")),
                                    "counter": [], "refutes": None}]})
            elif kind == "verify":
                artifact(root, task_id, {
                    "task_id": task_id, "hypothesis": inputs["hypothesis"],
                    "verdict": "supported", "failing_citations": [],
                    "reasoning": "the quotes state the claim directly"})
        cli("submit", "--tick",
            str(runconfig.load(root)["status"]["tick"]), "--root", str(root))
    else:
        raise AssertionError("no halt after 30 ticks — livelocked")

    halted = runconfig.load(root)["status"]["halted"]
    assert halted["reason"] == "coverage", halted

    promoted = [h for h in (store.read(i) for i in store.ids("hypothesis"))
                if h["status"] in ("supported", "contested")]
    assert promoted, "the run never promoted anything, so it never challenged"
    # Every promoted claim was actually challenged, not merely allowed to
    # halt: the halt firing is necessary but not sufficient evidence.
    assert sorted(refuted_ids) == sorted(h["id"] for h in promoted)


def test_a_refutation_the_loop_finds_demotes_the_claim_and_still_halts(
    run_root, monkeypatch
):
    """The whole disconfirmation apparatus, driven through research.main.

    The test above answers its refute search with ZERO sources, so this
    chain had never run end to end:

        refute search finds a contradicting source -> extract -> citation
        -> attaches to the claim's `counter` -> gate 2 verifies it -> the
        score drops -> the claim is demoted -> a fresh verify is seeded
        -> the verifier sees stance-labelled counter quotes -> refuted

    Every link has a unit test. The chain spans plan 8 (refute searches,
    stance labelling), plan 9 (claim merging) and plan 11 (the counter
    term, the demotion, the re-verification), and nothing had composed
    them.

    Plan 11's F3 is why that matters: the re-verification shipped inert.
    The code read correctly and `create_task` silently deduped the new
    task into the finished one, so no re-adjudication happened at all.
    Hence the verify-task count below, which is the assertion that would
    have caught it.
    """
    root = run_root
    monkeypatch.setattr("workspace.shutil.which", lambda name: f"/usr/bin/{name}")
    assert cli("init", "why is the sky blue?", "--root", str(root)) == 0
    cfg = runconfig.load(root)
    cfg["scope"]["in_scope"] = ["atmospheric optics"]
    runconfig.save(root, cfg)
    store = memory_mod.Memory(root)

    domains = ["a-example.com", "b-example.com", "c-example.com"]
    quote = "short wavelengths scatter more"
    counter_url = "https://contrary-example.com/p"
    counter_quote = "we measured no wavelength dependence at all"
    claim = "Rayleigh scattering explains the blue sky"

    seen_supported = False
    verify_counts = []
    resumed = 0

    def verifies_for(hypothesis_id):
        return [t for t in store.ids("task")
                if store.read(t)["kind"] == "verify"
                and (store.read(t).get("inputs") or {}).get("hypothesis")
                == hypothesis_id]

    def refuted():
        return any(store.read(h)["status"] == "refuted"
                   for h in store.ids("hypothesis"))

    for _ in range(40):
        cli("next", "--root", str(root))
        if runconfig.load(root)["status"]["halted"]:
            if refuted():
                break
            # A halt is not an error, and an operator resumes past one.
            # Worth recording that this happens here at all: a refutation
            # in flight is a run of `recheck` and `verify` tasks, none of
            # which yields a new fact or a new domain, so the saturation
            # window goes dry and fires BEFORE the counter-evidence has
            # landed. `research continue` is exactly the documented
            # answer, and this is what an operator would do.
            resumed += 1
            assert cli("continue", "--root", str(root)) == 0
            continue
        for task_id in sorted(store.ids("task")):
            task = store.read(task_id)
            if task["status"] != "running":
                continue
            inputs = task.get("inputs") or {}
            kind = task["kind"]
            if kind == "decompose":
                artifact(root, task_id, {
                    "task_id": task_id, "assumptions": [],
                    "children": [{"question": "how does scattering work?",
                                  "kind": "search", "rationale": "the core",
                                  "depends_on_index": []}]})
            elif kind == "search" and inputs.get("stance") == "against":
                # THE DIFFERENCE from the test above: the loop's search
                # for this claim's disproof actually finds something.
                artifact(root, task_id, {
                    "task_id": task_id, "no_sources_reason": None,
                    "queries": ["evidence against rayleigh scattering"],
                    "sources": [{"url": counter_url, "title": "a contrary study",
                                 "relevance": 0.95,
                                 "why": "reports the opposite measurement"}]})
            elif kind == "search":
                artifact(root, task_id, {
                    "task_id": task_id, "no_sources_reason": None,
                    "queries": ["how does scattering work"],
                    "sources": [{"url": f"https://{d}/p", "title": f"src {d}",
                                 "relevance": 0.9, "why": "w"}
                                for d in domains]})
            elif kind == "extract":
                contrary = inputs["url"] == counter_url
                artifact(root, task_id, {
                    "task_id": task_id, "url": inputs["url"],
                    "published_at": None,
                    "source_type": "primary", "no_facts_reason": None,
                    "facts": [{"statement": ("the effect does not hold"
                                             if contrary
                                             else f"finding from {inputs['url']}"),
                               "quote": counter_quote if contrary else quote}]})
            elif kind == "recheck":
                # Routed through the real re-check rather than writing the
                # status directly: a counter citation only becomes LIVE
                # once gate 2 has passed it, and that is part of what is
                # under test.
                artifact(root, task_id, {
                    "task_id": task_id, "url": inputs["url"], "outcome": "read",
                    "quotes": [{"index": i, "present": True}
                               for i in range(len(inputs["quotes"]))]})
            elif kind == "hypothesize":
                # The same claim every round, so plan 9's merge is
                # exercised rather than sidestepped.
                artifact(root, task_id, {
                    "task_id": task_id, "no_hypotheses_reason": None,
                    "hypotheses": [{"claim": claim,
                                    "supporting": sorted(store.ids("citation")),
                                    "counter": [], "refutes": None}]})
            elif kind == "verify":
                hypothesis = store.read(inputs["hypothesis"])
                # `contradicted` once opposition is actually on the node.
                # This is the end state the whole apparatus exists to be
                # able to reach, and nothing had ever demonstrated it.
                against = bool(hypothesis["counter"])
                artifact(root, task_id, {
                    "task_id": task_id, "hypothesis": inputs["hypothesis"],
                    "verdict": "contradicted" if against else "supported",
                    "failing_citations": [],
                    "reasoning": ("the contrary measurement is direct and "
                                  "unrebutted" if against
                                  else "the quotes state the claim directly")})
        cli("submit", "--tick",
            str(runconfig.load(root)["status"]["tick"]), "--root", str(root))

        for hypothesis_id in store.ids("hypothesis"):
            node = store.read(hypothesis_id)
            if node["status"] == "supported":
                seen_supported = True
            verify_counts.append((hypothesis_id, len(verifies_for(hypothesis_id))))
    else:
        raise AssertionError("the refutation never completed in 40 ticks")

    # It halted, and the reason is a legitimate one rather than a wedge.
    halted = runconfig.load(root)["status"]["halted"]
    assert halted and halted["reason"] in ("coverage", "saturation"), halted

    # 1. plan 9: one claim, one node — not one per evidence round.
    hypotheses = store.ids("hypothesis")
    assert len(hypotheses) == 1, hypotheses
    node = store.read(hypotheses[0])

    # 2. it really was promoted before the refutation arrived.
    assert seen_supported, "the claim was never promoted, so nothing was refuted"

    # 3. plan 8: the loop's own refute search produced live opposition.
    assert node["counter"], "no counter-evidence ever reached the claim"
    live = graph_mod.Graph(store).live_citations()
    assert [c for c in node["counter"] if c in live], (
        "counter-evidence attached but never became live, so it could not "
        "have moved anything")

    # 4. plan 11: opposition moved it out of the findings.
    assert node["status"] == "refuted", node["status"]

    # 5. plan 11's F3: a verification the hypothesizer did not create.
    #    The counts rise past 1 only if the re-opened verify was a NEW
    #    task rather than create_task handing back the finished one.
    assert max(count for _, count in verify_counts) > 1, verify_counts
