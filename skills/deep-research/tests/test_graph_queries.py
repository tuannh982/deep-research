"""Queries the halt predicates and the scheduler need, and that plan 1
did not provide.

`undispatchable` is the important one. Spec section 4's coverage predicate
fires when no open task remains; without this, one structurally stuck task
holds a multi-day run open forever with nothing to dispatch."""
import pytest

import nodes
from graph import Graph


@pytest.fixture
def g(mem):
    return Graph(mem, max_depth=4)


# --- over_cap ---------------------------------------------------------

def test_over_cap_is_empty_when_everything_is_within_the_cap(g, mktask):
    mktask(depth=4)
    assert g.over_cap() == []


def test_over_cap_reports_an_open_task_past_the_cap(g, mktask):
    deep = mktask(depth=5)
    assert g.over_cap() == [deep["id"]]


def test_over_cap_ignores_a_closed_task_past_the_cap(mem, g, mktask):
    deep = mktask(depth=5)
    mem.update(deep["id"], status="done")
    assert g.over_cap() == []


def test_over_cap_is_sorted(mem, mktask):
    for _ in range(3):
        mktask(depth=9)
    assert Graph(mem, max_depth=4).over_cap() == ["T-001", "T-002", "T-003"]


# --- undispatchable ---------------------------------------------------

def test_a_plain_chain_is_all_eventually_dispatchable(g, mktask):
    first = mktask()
    second = mktask(depends_on=[first["id"]])
    assert g.eventually_dispatchable() == [first["id"], second["id"]]
    assert g.undispatchable() == []


def test_a_task_over_the_cap_is_undispatchable(g, mktask):
    deep = mktask(depth=5)
    assert g.undispatchable() == [deep["id"]]


def test_a_dependent_of_an_over_cap_task_is_undispatchable(g, mktask):
    deep = mktask(depth=5)
    waiter = mktask(depends_on=[deep["id"]])
    assert g.undispatchable() == [deep["id"], waiter["id"]]


def test_a_task_with_a_dangling_dependency_is_undispatchable(g, mktask):
    stuck = mktask(depends_on=["T-999"])
    assert g.undispatchable() == [stuck["id"]]


def test_a_dependent_of_an_abandoned_task_is_undispatchable(mem, g, mktask):
    dead = mktask()
    waiter = mktask(depends_on=[dead["id"]])
    mem.update(dead["id"], status="abandoned")
    assert g.undispatchable() == [waiter["id"]]


def test_a_dependent_of_a_running_task_is_still_dispatchable(mem, g, mktask):
    """A running task will produce output or time out into a requeue, so
    its dependents are waiting, not stuck."""
    active = mktask()
    waiter = mktask(depends_on=[active["id"]])
    mem.update(active["id"], status="running")
    assert g.undispatchable() == []
    assert g.eventually_dispatchable() == [waiter["id"]]


def test_a_dependent_of_a_blocked_task_is_still_dispatchable(mem, g, mktask):
    """`blocked` is a human hold. Declaring its dependents permanently
    stuck would let the coverage predicate fire — and the run halt — while
    a checkpoint is waiting on the user."""
    held = mktask()
    waiter = mktask(depends_on=[held["id"]])
    mem.update(held["id"], status="blocked")
    assert g.undispatchable() == []


def test_both_members_of_a_dependency_cycle_are_undispatchable(mem, g, mktask):
    first = mktask()
    second = mktask(depends_on=[first["id"]])
    mem.update(first["id"], depends_on=[second["id"]])
    assert Graph(mem).undispatchable() == ["T-001", "T-002"]


def test_a_task_whose_dependency_is_done_is_dispatchable(mem, g, mktask):
    first = mktask()
    second = mktask(depends_on=[first["id"]])
    mem.update(first["id"], status="done")
    assert Graph(mem).undispatchable() == []


def test_undispatchable_and_eventually_dispatchable_partition_the_open_set(
    mem, mktask
):
    ok = mktask()
    deep = mktask(depth=9)
    stuck = mktask(depends_on=["T-404"])
    mktask(status="done")
    graph = Graph(mem, max_depth=4)
    assert (set(graph.undispatchable()) | set(graph.eventually_dispatchable())
            == {ok["id"], deep["id"], stuck["id"]})
    assert not (set(graph.undispatchable())
                & set(graph.eventually_dispatchable()))


def test_the_frontier_is_a_subset_of_eventually_dispatchable(mem, mktask):
    first = mktask()
    mktask(depends_on=[first["id"]])
    mktask(depth=9)
    graph = Graph(mem, max_depth=4)
    assert set(graph.frontier()) <= set(graph.eventually_dispatchable())


# --- find_cycle, iteratively -----------------------------------------

def _write_chain(mem, length):
    """Write a linear depends_on chain directly, bypassing memory.create.

    memory.create calls ids() — a directory glob — on every call, so
    building this through the store is quadratic and takes minutes at this
    length. The chain only needs to be deep, not created through the
    writer, and one node is validated below to prove the shape is honest.

    Descending orientation: T-0001 depends on T-0002, ..., T-1999 depends
    on T-2000, and T-2000 depends on nothing. find_cycle() iterates
    sorted(color) and starts its walk at T-0001, so this shape nests the
    walk 2000 deep before it ever colors a node BLACK. The opposite
    (ascending) orientation was measured to NOT exercise the recursion at
    all: the walk still starts at the shallowest id, T-0001, but T-0001
    has no dependencies to descend into, and by the time the walk reaches
    T-0002..T-2000 each one's single dependency is already BLACK from a
    prior top-level iteration, so recursion never nests past depth 2.
    """
    directory = mem.dir_for("task")
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(1, length + 1):
        node = {
            "id": f"T-{index:04d}", "type": "task",
            "created_at": "2026-08-20T10:00:00Z",
            "updated_at": "2026-08-20T10:00:00Z",
            "status": "pending",
            "provenance": {"task": None, "agent": "decomposer"},
            "question": "q",
            "depends_on": [f"T-{index + 1:04d}"] if index < length else [],
            "parent": None, "depth": 0, "kind": "search", "attempts": 0,
        }
        (directory / f"{node['id']}.md").write_text(
            nodes.dumps(node), encoding="utf-8")
    mem.validate(mem.read(f"T-{length:04d}"))


def test_find_cycle_survives_a_dependency_chain_2000_long(mem):
    """The recursive implementation raised RecursionError here."""
    _write_chain(mem, 2000)
    assert Graph(mem).find_cycle() is None


def test_find_cycle_still_finds_a_cycle_at_the_end_of_a_long_chain(mem):
    """Closes the descending chain into a loop by pointing the *tail*,
    T-2000 (which otherwise depends on nothing), back at the head,
    T-0001 — consistent with _write_chain's T-000N -> T-000(N+1)
    orientation. The walk still has to descend the full 2000-deep chain
    from T-0001 before it finds the back-edge at T-2000, so this keeps
    exercising the same recursion depth as the acyclic case above.
    """
    _write_chain(mem, 2000)
    mem.update("T-2000", depends_on=["T-0001"])
    cycle = Graph(mem).find_cycle()
    assert cycle is not None
    assert cycle[0] == cycle[-1]
    assert len(set(cycle)) == 2000


def test_find_cycle_returns_a_closed_walk(mem, mktask):
    first = mktask()
    second = mktask(depends_on=[first["id"]])
    mem.update(first["id"], depends_on=[second["id"]])
    cycle = Graph(mem).find_cycle()
    assert cycle[0] == cycle[-1]
    assert set(cycle) == {"T-001", "T-002"}


def test_find_cycle_is_deterministic_across_calls(mem, mktask):
    first, second, third = mktask(), mktask(), mktask()
    mem.update(first["id"], depends_on=[second["id"]])
    mem.update(second["id"], depends_on=[third["id"]])
    mem.update(third["id"], depends_on=[first["id"]])
    graph = Graph(mem)
    assert graph.find_cycle() == Graph(mem).find_cycle()


# --- live_rechecks_for ------------------------------------------------
# The single definition of "a gate-2 check is still coming for this
# hypothesis". Two callers depend on it and must never disagree:
# apply.ensure_evidence_tasks declines to spawn an evidence search while it
# is non-empty, and halt.evidence_exhausted decides whether a hypothesis
# with no evidence task is starved or merely early.

def _pending_evidence(mem, mkcitation, mkfact, mkhypothesis):
    citation = mkcitation(url="https://one-example.com/x",
                          domain="one-example.com",
                          quote="a thin quoted span", status="pending")
    mkfact(statement="thin", citations=[citation["id"]])
    hypothesis = mkhypothesis(claim="c", supporting=[citation["id"]])
    return citation["id"], hypothesis["id"]


def _recheck(mem, mktask, citation_id, status):
    task = mktask(question="re-read", kind="recheck", status=status)
    return mem.update(task["id"],
                      inputs={"url": "https://one-example.com/x",
                              "quotes": ["a thin quoted span"],
                              "citations": [citation_id]})


@pytest.mark.parametrize("status", ["pending", "ready", "stale", "running"])
def test_a_recheck_that_can_still_produce_a_verdict_is_live(
    mem, g, mkcitation, mkfact, mkhypothesis, mktask, status
):
    citation_id, hypothesis_id = _pending_evidence(
        mem, mkcitation, mkfact, mkhypothesis)
    task = _recheck(mem, mktask, citation_id, status)
    assert Graph(mem).live_rechecks_for(hypothesis_id) == [task["id"]]


@pytest.mark.parametrize("status", ["abandoned", "done", "blocked"])
def test_a_recheck_in_a_state_no_verdict_can_come_from_is_not_live(
    mem, g, mkcitation, mkfact, mkhypothesis, mktask, status
):
    """`abandoned` is the livelock: three timed-out attempts leave the
    citation pending for ever. `done` already landed whatever verdict it
    had. `blocked` is a human hold that nothing in this codebase writes
    and that frontier() never picks up."""
    citation_id, hypothesis_id = _pending_evidence(
        mem, mkcitation, mkfact, mkhypothesis)
    _recheck(mem, mktask, citation_id, status)
    assert Graph(mem).live_rechecks_for(hypothesis_id) == []


def test_a_verified_citation_is_not_awaiting_anything(
    mem, g, mkcitation, mkfact, mkhypothesis, mktask
):
    """Only a `pending` citation can be waiting on gate 2. A stray open
    recheck task naming an already-settled citation must not veto the
    evidence search for ever."""
    citation = mkcitation(url="https://one-example.com/x",
                          domain="one-example.com",
                          quote="a thin quoted span", status="verified")
    mkfact(statement="thin", citations=[citation["id"]])
    hypothesis = mkhypothesis(claim="c", supporting=[citation["id"]])
    _recheck(mem, mktask, citation["id"], "pending")
    assert Graph(mem).live_rechecks_for(hypothesis["id"]) == []


def test_a_recheck_for_someone_elses_citation_is_not_live_for_this_one(
    mem, g, mkcitation, mkfact, mkhypothesis, mktask
):
    citation_id, hypothesis_id = _pending_evidence(
        mem, mkcitation, mkfact, mkhypothesis)
    other = mkcitation(url="https://two-example.com/y",
                       domain="two-example.com", quote="another span",
                       status="pending")
    _recheck(mem, mktask, other["id"], "pending")
    assert Graph(mem).live_rechecks_for(hypothesis_id) == []


def test_live_rechecks_for_is_sorted(
    mem, g, mkcitation, mkfact, mkhypothesis, mktask
):
    citation_id, hypothesis_id = _pending_evidence(
        mem, mkcitation, mkfact, mkhypothesis)
    ids = [_recheck(mem, mktask, citation_id, "pending")["id"]
           for _ in range(3)]
    assert Graph(mem).live_rechecks_for(hypothesis_id) == sorted(ids)


def test_a_schema_invalid_recheck_cannot_hold_the_veto(
    mem, g, mkcitation, mkfact, mkhypothesis, mktask
):
    """A task whose file fails its own schema is invisible to every other
    Graph query for scheduling purposes, so it can never run -- letting it
    veto the evidence search would starve the hypothesis permanently."""
    citation_id, hypothesis_id = _pending_evidence(
        mem, mkcitation, mkfact, mkhypothesis)
    task = _recheck(mem, mktask, citation_id, "pending")
    path = mem.path_for(task["id"])
    data = nodes.loads(path.read_text(encoding="utf-8"))
    del data["depth"]
    path.write_text(nodes.dumps(data), encoding="utf-8")
    assert Graph(mem).live_rechecks_for(hypothesis_id) == []


def test_an_unreadable_hypothesis_is_awaiting_nothing(mem, g):
    """Same skip-and-continue discipline as _domains_of: a dangling id is
    fsck's to report, not this query's to raise on."""
    assert Graph(mem).live_rechecks_for("H-404") == []
