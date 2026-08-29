"""The three halt predicates, and the one thing that is NOT among them.

Spec section 4: "There is no budget condition; the loop runs
indefinitely."
"""
import re
from pathlib import Path

import pytest

import apply
import halt
import journal
import runconfig
from graph import Graph


@pytest.fixture
def cfg():
    return runconfig.default("why is the sky blue?")


@pytest.fixture
def finished(mem, mktask, mkcitation, mkfact, mkhypothesis):
    """A run with nothing left to do and one well-evidenced hypothesis:
    3 verified citations across 3 registrable domains."""
    root = mktask(question="root", kind="decompose", status="done")
    worker = mktask(question="w", kind="extract", parent=root["id"], depth=1,
                    status="done")
    citations = [mkcitation(url=f"https://d{i}-example.com/x",
                            domain=f"d{i}-example.com", quote=f"a quoted span {i}")
                 for i in range(3)]
    for index, citation in enumerate(citations):
        mkfact(statement=f"f{index}", citations=[citation["id"]],
               task=worker["id"])
    hypothesis = mkhypothesis(
        claim="c", supporting=[c["id"] for c in citations],
        status="supported", confidence=0.6, verdict="supported",
        task=worker["id"])
    # A promoted claim now holds coverage open until it has faced a
    # search for its own disproof, so a fixture called `finished` has to
    # have faced one. `done` and empty-handed is the ordinary outcome:
    # the claim survived a search for its opposite.
    #
    # The tests that mean to assert the unchallenged case delete this —
    # see test_coverage_does_not_halt_while_a_promoted_claim_is_
    # unchallenged, which uses `unchallenged` below.
    refute = mktask(question="Find evidence that would show this claim is "
                             "false: c",
                    kind="search", parent=worker["id"], depth=1,
                    status="done")
    mem.update(refute["id"], inputs={"for_hypothesis": hypothesis["id"],
                                     "stance": "against"})
    return {"root": root["id"], "worker": worker["id"],
            "hypothesis": hypothesis["id"], "refute": refute["id"]}


@pytest.fixture
def unchallenged(mem, finished):
    """`finished`, minus the refute search. A promoted claim nothing has
    tried to break.

    The file is unlinked rather than the node marked abandoned: an
    abandoned refute task COUNTS as challenged (it never re-enters the
    frontier, so waiting on it waits forever), which is the opposite of
    what this fixture is for. `Memory` has no delete — it is an
    append-and-update store by design — so this reaches the path
    directly."""
    mem.path_for(finished["refute"]).unlink()
    return finished


def check(mem, cfg, root=None):
    events = journal.read(root) if root else []
    return halt.check(mem, Graph(mem), cfg, events)


# --- there is no budget halt -----------------------------------------

def test_there_is_no_budget_halt_predicate():
    """Spec non-goals and section 4 both say so explicitly. A budget
    condition added later would silently truncate a run the user asked
    to leave going."""
    source = halt.__doc__ + "".join(
        getattr(halt, name).__doc__ or ""
        for name in dir(halt) if callable(getattr(halt, name, None))
    )
    assert "budget" in source.lower()  # named, as a thing that does not exist
    assert not hasattr(halt, "budget_halt")


def test_a_long_run_with_plenty_of_work_does_not_halt(mem, cfg, mktask):
    for index in range(50):
        mktask(question=f"q{index}")
    cfg["status"]["tick"] = 5000
    assert check(mem, cfg) is None


# --- signal -----------------------------------------------------------

def test_an_explicit_stop_request_halts(mem, cfg, mktask):
    mktask(question="unfinished work")
    cfg["signals"]["stop_requested"] = True
    result = check(mem, cfg)
    assert result.reason == "signal"


def test_a_stop_request_halts_even_with_work_outstanding(mem, cfg, mktask):
    """The user's instruction outranks the graph's opinion."""
    mktask(question="lots left to do")
    cfg["signals"]["stop_requested"] = True
    assert check(mem, cfg) is not None


def test_a_satisfied_stop_when_predicate_halts(mem, cfg, finished):
    cfg["signals"]["stop_when"] = {"all": [{"min_facts": 3}]}
    result = check(mem, cfg)
    assert result.reason == "signal"
    assert "min_facts" in result.detail or "3" in result.detail


def test_an_unsatisfied_stop_when_predicate_does_not_halt(mem, cfg, mktask):
    mktask(question="work")
    cfg["signals"]["stop_when"] = {"all": [{"min_facts": 500}]}
    assert check(mem, cfg) is None


def test_signal_is_checked_before_coverage(mem, cfg, finished):
    """Both would fire. The reported reason must be the user's, because
    that is what `research status` shows them."""
    cfg["signals"]["stop_requested"] = True
    assert check(mem, cfg).reason == "signal"


# --- coverage ---------------------------------------------------------

def test_coverage_halts_when_nothing_is_left_and_evidence_is_sound(
    mem, cfg, finished
):
    result = check(mem, cfg)
    assert result.reason == "coverage"


def test_coverage_does_not_halt_while_a_task_is_dispatchable(
    mem, cfg, finished, mktask
):
    mktask(question="one more thing")
    assert check(mem, cfg) is None


def test_coverage_does_not_halt_while_a_task_is_running(
    mem, cfg, finished, mktask
):
    task = mktask(question="in flight")
    mem.update(task["id"], status="running")
    assert check(mem, cfg) is None


def test_coverage_ignores_a_task_over_the_depth_cap(
    mem, cfg, finished, mktask
):
    """Carry-forward (e), the detection half. Taken literally the spec's
    'no pending tasks remain' is never true again once one of these
    exists, and the run can never stop by itself."""
    mktask(question="too deep", depth=99)
    assert check(mem, cfg).reason == "coverage"


def test_coverage_ignores_a_task_waiting_on_an_abandoned_dependency(
    mem, cfg, finished, mktask
):
    dead = mktask(question="failed three times")
    mktask(question="waiting forever", depends_on=[dead["id"]])
    mem.update(dead["id"], status="abandoned")
    assert check(mem, cfg).reason == "coverage"


def test_coverage_does_not_halt_with_an_under_evidenced_hypothesis(
    mem, cfg, finished, mkcitation, mkfact, mkhypothesis
):
    thin = mkcitation(url="https://one-example.com/x", domain="one-example.com",
                      quote="a thin quoted span")
    mkfact(statement="thin", citations=[thin["id"]], task=finished["worker"])
    mkhypothesis(claim="under-evidenced", supporting=[thin["id"]],
                 task=finished["worker"])
    assert check(mem, cfg) is None


def test_coverage_does_not_halt_when_evidence_is_all_one_domain(
    mem, cfg, finished, mkcitation, mkfact, mkhypothesis
):
    """Spec section 9's adversarial case, at the halt level: three
    citations that are really one source must not look like coverage."""
    ids = []
    for index in range(3):
        citation = mkcitation(url=f"https://same-example.com/{index}",
                              domain="same-example.com", quote=f"a same-site span {index}")
        mkfact(statement=f"s{index}", citations=[citation["id"]],
               task=finished["worker"])
        ids.append(citation["id"])
    mkhypothesis(claim="one source only", supporting=ids,
                 task=finished["worker"])
    assert check(mem, cfg) is None


def test_coverage_ignores_a_refuted_hypothesis(
    mem, cfg, finished, mkhypothesis
):
    """A refuted claim can never gather three verified citations for
    itself. Counting it makes coverage unreachable as soon as anything is
    disproven."""
    mkhypothesis(claim="disproven", supporting=["C-001"], status="refuted",
                 verdict="contradicted", task=finished["worker"])
    assert check(mem, cfg).reason == "coverage"


# --- coverage requires that promoted claims were challenged -----------
#
# Without this the invariant is emergent rather than asserted: submit's
# step 4 happens to run ensure_refute_tasks before the next halt check,
# so a promoted claim happens to acquire a refute search in time. That
# holds only as long as nobody reorders submit, and nothing tested it.
#
# THE LIVELOCK is the thing to watch in every test here. coverage_halt
# has a documented history of being unfireable (see its docstring: "13
# tasks done, 0 in flight, 6 of 6 dry, no halt, forever"), and every
# terminal state below has to count as "challenged" or it recurs.

def _refute_task(mem, mktask, hypothesis_id, parent, status="done"):
    task = mktask(question=f"Find evidence that would show this claim is "
                           f"false: c", kind="search", parent=parent,
                  depth=1, status=status)
    return mem.update(task["id"], inputs={"for_hypothesis": hypothesis_id,
                                          "stance": "against"})


def test_coverage_does_not_halt_while_a_promoted_claim_is_unchallenged(
    mem, cfg, unchallenged
):
    """The guarantee this whole plan exists for: a run cannot report a
    claim it never tried to break."""
    assert check(mem, cfg) is None


def test_coverage_halts_once_the_promoted_claim_has_been_challenged(
    mem, cfg, finished
):
    assert check(mem, cfg).reason == "coverage"


def test_a_refute_search_that_found_nothing_counts_as_challenged(
    mem, cfg, unchallenged, mktask
):
    """THE LIVELOCK TEST. A refute search returning no sources is a real
    and useful answer — the claim survived a search for its opposite —
    and searcher.md says so. If an empty result did not count, nothing
    further would ever be scheduled and the run could never halt: the
    task is `done`, create_task would reuse it, and it would never be
    dispatchable again."""
    _refute_task(mem, mktask, unchallenged["hypothesis"],
                 unchallenged["worker"], status="done")
    assert check(mem, cfg).reason == "coverage"


def test_an_abandoned_refute_search_counts_as_challenged(
    mem, cfg, unchallenged, mktask
):
    """Abandoned after max_attempts never re-enters the frontier, so
    waiting on it waits forever. It is reported in Appendix C."""
    _refute_task(mem, mktask, unchallenged["hypothesis"],
                 unchallenged["worker"], status="abandoned")
    assert check(mem, cfg).reason == "coverage"


def test_an_unpromoted_claim_never_blocks_the_halt(mem, cfg, unchallenged):
    """ensure_refute_tasks declines for these, so a claim waited on here
    is waited on forever. Same trap evidence_exhausted names for its own
    two decline cases."""
    mem.update(mem.ids("hypothesis")[0], status="proposed")
    assert check(mem, cfg).reason == "coverage"


def test_a_refuted_claim_never_blocks_the_halt(mem, cfg, unchallenged):
    mem.update(mem.ids("hypothesis")[0], status="refuted")
    assert check(mem, cfg).reason == "coverage"


def test_a_claim_with_a_malformed_provenance_task_does_not_block(
    mem, cfg, unchallenged
):
    """ensure_refute_tasks drops exactly this case, so no refute task
    will ever appear for it. evidence_exhausted's decline 1, mirrored."""
    mem.update(mem.ids("hypothesis")[0],
               provenance={"task": None, "agent": "hypothesizer"})
    assert check(mem, cfg).reason == "coverage"


def test_synthesis_is_not_blocked_by_an_unchallenged_claim(
    mem, cfg, unchallenged
):
    """A livelock the plan did not anticipate, found reading submit.

    submit skips ALL follow-on scheduling while the phase is
    `synthesize` — no new searches, no new hypothesis tasks, and so no
    new refute tasks either. So a promoted claim that reached synthesis
    unchallenged can never acquire a challenge, and requiring one would
    mean the synthesis phase could never halt and the report could never
    be rendered. The requirement is a research-phase invariant."""
    cfg["status"]["phase"] = "synthesize"
    assert check(mem, cfg).reason == "coverage"


def test_coverage_halts_a_run_with_no_hypotheses_at_all(mem, cfg, mktask):
    """Vacuous, and correct: there is nothing left to dispatch and no
    claim to under-evidence. The alternative is a run that cannot end
    because it never started."""
    mktask(question="root", status="done")
    assert check(mem, cfg).reason == "coverage"


def test_coverage_uses_the_run_s_own_thresholds(mem, cfg, finished):
    cfg["config"]["required_domains"] = 9
    assert check(mem, cfg) is None


# --- carry-forward: a schema-invalid task must not block the halt, but
# must not vanish from it either. Same trick tests/test_fsck.py and
# tests/test_graph_total.py use, kept local rather than imported across
# test modules (matching how those two keep their own copies too).

def _strip_line(path, prefix):
    """Delete the frontmatter line starting with `prefix`, leaving the
    file parseable but schema-invalid."""
    kept = [
        line for line in path.read_text(encoding="utf-8").splitlines(True)
        if not line.startswith(prefix)
    ]
    path.write_text("".join(kept), encoding="utf-8")


def test_coverage_ignores_a_schema_invalid_task_but_names_it(
    mem, cfg, finished, mktask
):
    """A task missing a required field is invisible to
    eventually_dispatchable()/undispatchable() alike (neither may trust a
    field it might be missing) so it must not hold coverage open forever
    -- but its id must still be named, or the halt reads as completion
    while a malformed task's fate is unknown."""
    malformed = mktask(question="malformed")
    _strip_line(mem.path_for(malformed["id"]), "status:")
    result = check(mem, cfg)
    assert result.reason == "coverage"
    assert malformed["id"] in result.detail


def test_coverage_ignores_an_unparseable_task_but_names_it(
    mem, cfg, finished, mktask
):
    """A task file that fails to parse at all is even more invisible than
    a schema-invalid one -- Graph.tasks itself drops it -- so it needs
    the same guarantee via memory.ids('task') rather than anything keyed
    off Graph's own task dict."""
    garbled = mktask(question="garbled")
    mem.path_for(garbled["id"]).write_text("garbage\n", encoding="utf-8")
    result = check(mem, cfg)
    assert result.reason == "coverage"
    assert garbled["id"] in result.detail


# --- saturation -------------------------------------------------------

def _complete(root, tick, task, branch, facts=0, doms=0):
    journal.append(root, "task_completed", tick=tick, task=task,
                   root_branch=branch, new_facts=facts, new_domains=doms)


def test_six_dry_completions_across_two_branches_halt(mem, cfg, tmp_path,
                                                      mktask):
    mktask(question="still open")
    for index in range(6):
        _complete(tmp_path, index, f"T-{index + 10:03d}",
                  "T-001" if index < 3 else "T-002")
    result = check(mem, cfg, root=tmp_path)
    assert result.reason == "saturation"


def test_five_dry_completions_are_not_enough(mem, cfg, tmp_path, mktask):
    mktask(question="still open")
    for index in range(5):
        _complete(tmp_path, index, f"T-{index + 10:03d}",
                  "T-001" if index < 3 else "T-002")
    assert check(mem, cfg, root=tmp_path) is None


def test_a_single_new_fact_in_the_window_prevents_saturation(
    mem, cfg, tmp_path, mktask
):
    mktask(question="still open")
    for index in range(6):
        _complete(tmp_path, index, f"T-{index + 10:03d}",
                  "T-001" if index < 3 else "T-002",
                  facts=1 if index == 4 else 0)
    assert check(mem, cfg, root=tmp_path) is None


def test_a_single_new_domain_in_the_window_prevents_saturation(
    mem, cfg, tmp_path, mktask
):
    mktask(question="still open")
    for index in range(6):
        _complete(tmp_path, index, f"T-{index + 10:03d}",
                  "T-001" if index < 3 else "T-002",
                  doms=1 if index == 0 else 0)
    assert check(mem, cfg, root=tmp_path) is None


def test_six_dry_completions_in_one_branch_do_not_halt(mem, cfg, tmp_path,
                                                       mktask):
    """The branch guard. One exhausted branch says nothing about the
    others, and halting on it would abandon fresh work."""
    mktask(question="still open")
    for index in range(6):
        _complete(tmp_path, index, f"T-{index + 10:03d}", "T-001")
    assert check(mem, cfg, root=tmp_path) is None


def test_only_the_last_six_completions_count(mem, cfg, tmp_path, mktask):
    """A productive tick eight completions ago must not keep the run
    alive forever."""
    mktask(question="still open")
    _complete(tmp_path, 0, "T-900", "T-001", facts=5, doms=3)
    for index in range(6):
        _complete(tmp_path, index + 1, f"T-{index + 10:03d}",
                  "T-001" if index < 3 else "T-002")
    assert check(mem, cfg, root=tmp_path).reason == "saturation"


def test_the_saturation_window_is_configurable(mem, cfg, tmp_path, mktask):
    """Spec section 4: 'Thresholds (3, 2, 6) are configurable in
    run.yaml.'"""
    mktask(question="still open")
    cfg["config"]["saturation_window"] = 2
    for index in range(2):
        _complete(tmp_path, index, f"T-{index + 10:03d}",
                  f"T-00{index + 1}")
    assert check(mem, cfg, root=tmp_path).reason == "saturation"


def test_the_saturation_branch_count_is_configurable(mem, cfg, tmp_path,
                                                     mktask):
    mktask(question="still open")
    cfg["config"]["saturation_branches"] = 1
    for index in range(6):
        _complete(tmp_path, index, f"T-{index + 10:03d}", "T-001")
    assert check(mem, cfg, root=tmp_path).reason == "saturation"


def test_coverage_is_checked_before_saturation(mem, cfg, tmp_path, finished):
    """A finished run is covered, not saturated. Reporting saturation
    would tell the user the research stalled when in fact it completed."""
    for index in range(6):
        _complete(tmp_path, index, f"T-{index + 10:03d}",
                  "T-001" if index < 3 else "T-002")
    assert check(mem, cfg, root=tmp_path).reason == "coverage"


# --- the digest -------------------------------------------------------

def test_the_digest_is_one_line(mem, cfg, finished, tmp_path):
    """Spec section 4: 'Every 25 ticks the loop prints one line and keeps
    going. Notice, not gate.'"""
    line = halt.digest(mem, Graph(mem), cfg, journal.read(tmp_path))
    assert "\n" not in line


def test_the_digest_reports_the_counts_the_spec_shows(mem, cfg, finished,
                                                      tmp_path):
    cfg["status"]["tick"] = 175
    line = halt.digest(mem, Graph(mem), cfg, journal.read(tmp_path))
    for expected in ("TICK 175", "tasks", "facts", "domains",
                     "hypotheses", "saturation"):
        assert expected in line, expected


def test_the_digest_counts_distinct_domains_not_citations(
    mem, cfg, finished, mkcitation, mkfact, tmp_path
):
    extra = mkcitation(url="https://d0-example.com/other", domain="d0-example.com",
                       quote="another quoted span")
    mkfact(statement="extra", citations=[extra["id"]],
           task=finished["worker"])
    line = halt.digest(mem, Graph(mem), cfg, journal.read(tmp_path))
    assert "domains 3" in line


# --- out/status.md ----------------------------------------------------

def test_render_status_names_the_halt_reason(mem, cfg, finished, tmp_path):
    halted = halt.check(mem, Graph(mem), cfg, [])
    text = halt.render_status(mem, Graph(mem), cfg, [], halted)
    assert "coverage" in text


def test_render_status_lists_abandoned_tasks_as_open_questions(
    mem, cfg, finished, mktask
):
    """Spec section 4: an abandoned task 'surfaces in Appendix C as an
    open question'. status.md is where the user sees it first."""
    dead = mktask(question="could not answer this")
    mem.update(dead["id"], status="abandoned",
               abandoned_reason="3 rejected artifacts")
    text = halt.render_status(mem, Graph(mem), cfg, [], None)
    assert "could not answer this" in text
    assert "3 rejected artifacts" in text


def test_render_status_lists_undispatchable_tasks(mem, cfg, finished, mktask):
    """These are the tasks coverage deliberately ignores. Ignoring them
    in the report too would make the halt look like completion."""
    mktask(question="stranded work", depth=99)
    text = halt.render_status(mem, Graph(mem), cfg, [], None)
    assert "stranded work" in text


def test_render_status_lists_a_schema_invalid_task(mem, cfg, finished, mktask):
    """Same carry-forward as coverage_halt's: a task missing a required
    field is invisible to undispatchable() too, so it needs its own
    listing or the report simply never mentions it."""
    malformed = mktask(question="malformed")
    _strip_line(mem.path_for(malformed["id"]), "status:")
    text = halt.render_status(mem, Graph(mem), cfg, [], None)
    assert malformed["id"] in text


def test_render_status_lists_an_unparseable_task(mem, cfg, finished, mktask):
    garbled = mktask(question="garbled")
    mem.path_for(garbled["id"]).write_text("garbage\n", encoding="utf-8")
    text = halt.render_status(mem, Graph(mem), cfg, [], None)
    assert garbled["id"] in text


def test_render_status_lists_refuted_assumptions(mem, cfg, finished,
                                                 mkassumption):
    mkassumption(statement="v3 is current", raised_by=finished["root"],
                 status="refuted")
    text = halt.render_status(mem, Graph(mem), cfg, [], None)
    assert "v3 is current" in text


def test_render_status_lists_the_weakest_hypotheses(mem, cfg, finished,
                                                    mkhypothesis):
    mkhypothesis(claim="shakiest claim", supporting=["C-001"],
                 confidence=0.05, task=finished["worker"])
    text = halt.render_status(mem, Graph(mem), cfg, [], None)
    assert "shakiest claim" in text


def test_render_status_survives_a_corrupt_node(mem, cfg, finished):
    """The status report is what a user reads when something has gone
    wrong. It must not be the thing that breaks."""
    mem.path_for("F-001").write_text("garbage\n", encoding="utf-8")
    halt.render_status(mem, Graph(mem), cfg, [], None)  # must not raise


def test_write_status_lands_in_out(tmp_path):
    path = halt.write_status(tmp_path, "# status\n")
    assert path == tmp_path / "out" / "status.md"
    assert path.read_text(encoding="utf-8") == "# status\n"


def test_record_stores_the_halt_in_run_yaml(workspace_root, mem, cfg,
                                            finished):
    halted = halt.Halt("coverage", "nothing left to dispatch")
    updated = halt.record(workspace_root, runconfig.load(workspace_root),
                          halted)
    stored = runconfig.load(workspace_root)["status"]["halted"]
    assert stored["reason"] == "coverage"
    assert stored["detail"] == "nothing left to dispatch"
    assert stored["at"].endswith("Z")
    assert updated["status"]["halted"] == stored


def test_record_of_none_clears_a_stored_halt(workspace_root):
    halt.record(workspace_root, runconfig.load(workspace_root),
                halt.Halt("signal", "user asked"))
    halt.record(workspace_root, runconfig.load(workspace_root), None)
    assert runconfig.load(workspace_root)["status"]["halted"] is None


# --- coverage: an exhausted search is a finding, not a wait -----------

def _exhausted_evidence_search(mem, mktask, hypothesis_id, parent):
    """The search task ensure_evidence_tasks spawns for an under-evidenced
    hypothesis, in the state it ends up in: `done`, having found nothing
    more. The gap string is stable, so TASK_KEY resolves to this same task
    on every later tick and no new work is ever created."""
    task = mktask(question="find further evidence", kind="search",
                  parent=parent, depth=1, status="done")
    mem.update(task["id"], inputs={"for_hypothesis": hypothesis_id})
    return task


def test_coverage_halts_when_the_evidence_search_is_already_exhausted(
    mem, cfg, finished, mktask, mkcitation, mkfact, mkhypothesis
):
    """C4(b). coverage refused while any non-refuted hypothesis failed
    gate 3, and ensure_evidence_tasks could not make new work for it: the
    gap string is stable, so TASK_KEY resolves to the search task that
    already ran and create_task reuses it. Reproduced at 13 tasks done, 0
    in flight, "6 of 6 dry", no halt, forever — with saturation unable to
    rescue it for a separate reason.

    An under-evidenced claim whose search is exhausted is a FINDING. It
    gets reported, not waited on."""
    thin = mkcitation(url="https://one-example.com/x",
                      domain="one-example.com", quote="a thin quoted span")
    mkfact(statement="thin", citations=[thin["id"]], task=finished["worker"])
    hypothesis = mkhypothesis(claim="under-evidenced", supporting=[thin["id"]],
                              task=finished["worker"])
    _exhausted_evidence_search(mem, mktask, hypothesis["id"],
                               finished["worker"])
    result = check(mem, cfg)
    assert result is not None
    assert result.reason == "coverage"
    assert hypothesis["id"] in result.detail
    assert "gate 3" in result.detail


def test_coverage_still_waits_while_the_evidence_search_can_still_run(
    mem, cfg, finished, mktask, mkcitation, mkfact, mkhypothesis
):
    """The other side of the ruling. A search that has not run yet is
    real outstanding work, and halting on it would abandon evidence the
    run was about to gather."""
    thin = mkcitation(url="https://one-example.com/x",
                      domain="one-example.com", quote="a thin quoted span")
    mkfact(statement="thin", citations=[thin["id"]], task=finished["worker"])
    hypothesis = mkhypothesis(claim="under-evidenced", supporting=[thin["id"]],
                              task=finished["worker"])
    pending = mktask(question="find further evidence", kind="search",
                     parent=finished["worker"], depth=1)
    mem.update(pending["id"], inputs={"for_hypothesis": hypothesis["id"]})
    assert check(mem, cfg) is None


def test_coverage_does_not_wait_on_a_hypothesis_no_search_can_ever_serve(
    mem, cfg, finished, mkcitation, mkfact, mkhypothesis
):
    """ensure_evidence_tasks drops a hypothesis whose own provenance task
    is missing or malformed, so no evidence task will ever be spawned for
    it. Waiting for one is waiting forever."""
    thin = mkcitation(url="https://one-example.com/x",
                      domain="one-example.com", quote="a thin quoted span")
    mkfact(statement="thin", citations=[thin["id"]], task=finished["worker"])
    orphan = mkhypothesis(claim="nobody owns this", supporting=[thin["id"]])
    result = check(mem, cfg)
    assert result is not None and result.reason == "coverage"
    assert orphan["id"] in result.detail


def test_render_status_names_an_under_evidenced_hypothesis(
    mem, cfg, finished, mktask, mkcitation, mkfact, mkhypothesis
):
    """coverage no longer waits on these, so the open questions section is
    the only place the user ever hears about them."""
    thin = mkcitation(url="https://one-example.com/x",
                      domain="one-example.com", quote="a thin quoted span")
    mkfact(statement="thin", citations=[thin["id"]], task=finished["worker"])
    hypothesis = mkhypothesis(claim="a claim we could not evidence",
                              supporting=[thin["id"]], task=finished["worker"])
    _exhausted_evidence_search(mem, mktask, hypothesis["id"],
                               finished["worker"])
    text = halt.render_status(mem, Graph(mem), cfg, [], None)
    section = text.split("## Open questions")[1].split("##")[0]
    assert hypothesis["id"] in section
    assert "a claim we could not evidence" in section
    assert "gate 3" in section


# --- coverage: a starved re-check must not livelock the run -----------

def _starved_by_a_recheck(mem, mktask, mkcitation, mkfact, mkhypothesis,
                          worker, recheck_status, depth=1):
    """A hypothesis whose sole evidence is one `pending` citation, plus the
    `recheck` task apply_extract seeds beside such a citation, in
    `recheck_status` and at `depth`. Returns the hypothesis id."""
    thin = mkcitation(url="https://one-example.com/x",
                      domain="one-example.com", quote="a thin quoted span",
                      status="pending")
    mkfact(statement="thin", citations=[thin["id"]], task=worker)
    hypothesis = mkhypothesis(claim="waiting on a check that never comes",
                              supporting=[thin["id"]], task=worker)
    recheck = mktask(question="re-read https://one-example.com/x",
                     kind="recheck", parent=worker, depth=depth,
                     status=recheck_status)
    mem.update(recheck["id"], inputs={"url": "https://one-example.com/x",
                                      "quotes": ["a thin quoted span"],
                                      "citations": [thin["id"]]})
    return hypothesis["id"]


def test_an_abandoned_recheck_does_not_livelock_the_run(
    mem, cfg, finished, mktask, mkcitation, mkfact, mkhypothesis
):
    """The end-to-end defect. A `recheck` abandoned after three attempts
    leaves its citations `pending` for ever; `abandoned` is not in
    Graph.OPEN_TASK_STATUSES, so nothing is dispatchable, and the Task-6
    veto suppressed the one thing that could still make work. Measured:

        SPAWNED BY ensure_evidence_tasks: []
        EVENTUALLY DISPATCHABLE:          []
        FRONTIER:                         []
        COVERAGE HALT:                    None

    -- `research next` printing "nothing to dispatch" forever, with no
    halt ever recorded. The halt cannot rescue this on its own: submit
    step 4 has to be able to spawn the search."""
    hypothesis_id = _starved_by_a_recheck(
        mem, mktask, mkcitation, mkfact, mkhypothesis, finished["worker"],
        recheck_status="abandoned")

    graph = Graph(mem)
    assert graph.frontier() == []
    assert graph.eventually_dispatchable() == []
    assert check(mem, cfg) is None

    spawned = apply.ensure_evidence_tasks(mem, Graph(mem), cfg)
    assert len(spawned.spawned) == 1
    assert Graph(mem).frontier() == spawned.spawned

    # And once that search has run and come back with nothing more, the
    # run reaches a clean coverage halt instead of sitting silent.
    mem.update(spawned.spawned[0], status="done")
    result = check(mem, cfg)
    assert result is not None
    assert result.reason == "coverage"
    assert hypothesis_id in result.detail
    assert "gate 3" in result.detail


def test_coverage_does_not_wait_on_a_recheck_that_can_never_be_dispatched(
    mem, cfg, finished, mktask, mkcitation, mkfact, mkhypothesis
):
    """Belt and braces for the same livelock, one layer down.

    `evidence_exhausted`'s docstring justifies "no evidence task at all
    means NOT exhausted" with: submit step 4 runs ensure_evidence_tasks
    before the halt check, so one is about to exist. The re-check veto is
    the second case that falsifies that premise, and narrowing the veto
    to LIVE re-checks is not quite enough on its own: an OPEN re-check
    that can never reach the frontier -- here, past the depth cap, which
    a mid-run `max_depth` reduction produces -- still holds the veto while
    contributing nothing to eventually_dispatchable(). The halt has to be
    able to see that for itself rather than trust ensure_evidence_tasks."""
    hypothesis_id = _starved_by_a_recheck(
        mem, mktask, mkcitation, mkfact, mkhypothesis, finished["worker"],
        recheck_status="pending", depth=99)

    graph = Graph(mem)
    assert graph.eventually_dispatchable() == []
    assert apply.ensure_evidence_tasks(mem, Graph(mem), cfg).spawned == []

    result = check(mem, cfg)
    assert result is not None
    assert result.reason == "coverage"
    assert hypothesis_id in result.detail


def test_a_hypothesis_is_not_called_starved_while_its_recheck_is_still_live(
    mem, cfg, finished, mktask, mkcitation, mkfact, mkhypothesis
):
    """The other side. `render_status` calls evidence_exhausted with a
    live set that is genuinely non-empty mid-run, so the branch above must
    not fire for a hypothesis whose re-check is sitting on the frontier
    waiting to be dispatched. Saying "no evidence-seeking task remains"
    there would report a limitation the run does not have."""
    hypothesis_id = _starved_by_a_recheck(
        mem, mktask, mkcitation, mkfact, mkhypothesis, finished["worker"],
        recheck_status="pending")

    graph = Graph(mem)
    assert graph.eventually_dispatchable() != []
    assert check(mem, cfg) is None

    section = halt.render_status(mem, graph, cfg, [], None)
    section = section.split("## Open questions")[1].split("##")[0]
    assert "no evidence-seeking task remains" not in section
    assert hypothesis_id not in section


# --- phase branches ------------------------------------------------------

_PHASE_ASSIGNED = re.compile(r'"phase"\]\s*=\s*"([a-z]+)"')
_PHASE_DEFAULTED = re.compile(r'"phase":\s*"([a-z]+)"')
_PHASE_BRANCH = re.compile(r'phase == "([a-z]+)"')


def test_render_status_only_branches_on_phases_the_code_can_produce():
    """`render_status` carried `elif phase == "render":`, and nothing in
    scripts/ ever assigns that phase — scheduler sets "research",
    synthesis "synthesize", render "done", and runconfig.default starts at
    "scope". The branch was unreachable.

    "render" is not a state a run rests in; it is one synchronous command.
    The three assigned phases plus the `halted` flag already cover every
    observable state, and making it reachable would now be actively
    harmful: a phase="render" written on a failed build would SURVIVE the
    next `research next` (that write is guarded as of this wave) and tell
    the operator to render while a reopened writer is still pending.

    schemas/run.json's enum keeps init/scope/decompose/render on purpose —
    it documents spec section 3's phase list and is a superset by design.
    A branch is not: it promises a state the code will reach.
    """
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    assigned = set()
    for path in sorted(scripts.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assigned |= set(_PHASE_ASSIGNED.findall(source))
        assigned |= set(_PHASE_DEFAULTED.findall(source))
    assert assigned == {"scope", "research", "synthesize", "done"}

    branched = set(_PHASE_BRANCH.findall(
        (scripts / "halt.py").read_text(encoding="utf-8")))
    assert branched <= assigned, (
        "halt.py branches on phases nothing assigns: "
        + ", ".join(sorted(branched - assigned)))


# --- a saturation halt has to be clearable ----------------------------

def test_continue_clears_a_saturation_halt(mem, cfg, mktask, tmp_path):
    """Found by driving a refutation end to end, not by inspection.

    Saturation is not a stored flag: it is recomputed every `next` from
    the journal's last N completions. So `research continue` cleared the
    halt in run.yaml and changed nothing — `next` re-read the same dry
    window and halted again, and no new completion could ever arrive
    because the halt is what stops anything being dispatched. A run with
    real work outstanding was stranded permanently; the only exits were
    `research signal stop` or hand-editing the journal.

    Measured on a real driven run: a refutation in flight is a run of
    `recheck` and `verify` tasks, none of which yields a new fact or a
    new domain, so the window went dry and fired with the
    counter-evidence still pending — and the run could not be resumed.
    """
    root = tmp_path / "research"
    (root / "memory").mkdir(parents=True)
    for index in range(cfg["config"]["saturation_window"]):
        journal.append(root, "task_completed", tick=index, task=f"T-{index:03}",
                       root_branch=f"T-{index % 2:03}", new_facts=0,
                       new_domains=0)
    events = journal.read(root)
    assert halt.saturation_halt(cfg, events) is not None

    journal.append(root, "resumed", tick=9)
    assert halt.saturation_halt(cfg, journal.read(root)) is None


def test_completions_after_a_resume_can_saturate_again(mem, cfg, tmp_path):
    """The cut must not switch saturation off for the rest of the run —
    a resumed run that goes dry again has still gone dry."""
    root = tmp_path / "research"
    (root / "memory").mkdir(parents=True)
    journal.append(root, "resumed", tick=0)
    for index in range(cfg["config"]["saturation_window"]):
        journal.append(root, "task_completed", tick=index, task=f"T-{index:03}",
                       root_branch=f"T-{index % 2:03}", new_facts=0,
                       new_domains=0)
    assert halt.saturation_halt(cfg, journal.read(root)) is not None


def test_a_resume_does_not_hide_productive_completions_before_it(mem, cfg,
                                                                  tmp_path):
    """Guards the guard: the cut drops history, so a window that has not
    yet refilled must simply not fire rather than fire on stale records."""
    root = tmp_path / "research"
    (root / "memory").mkdir(parents=True)
    for index in range(cfg["config"]["saturation_window"]):
        journal.append(root, "task_completed", tick=index, task=f"T-{index:03}",
                       root_branch=f"T-{index % 2:03}", new_facts=0,
                       new_domains=0)
    journal.append(root, "resumed", tick=9)
    journal.append(root, "task_completed", tick=10, task="T-100",
                   root_branch="T-000", new_facts=0, new_domains=0)
    assert halt.saturation_halt(cfg, journal.read(root)) is None
