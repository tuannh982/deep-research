"""The cascade must never raise. It writes across the whole graph from
inside submit, which the spec requires to be idempotent, so a raise
mid-cascade leaves a partial invalidation that re-running cannot repair.

Every test here corrupts one node and asserts the cascade completes and
still does its job to the healthy remainder — the same discipline fsck.py
already follows."""
import pytest

import graph as graph_mod
from graph import Graph


@pytest.fixture
def g(mem):
    return Graph(mem, max_depth=4)


def _strip_line(path, prefix):
    """Delete the frontmatter line starting with `prefix`, leaving the file
    parseable but schema-invalid."""
    kept = [
        line for line in path.read_text(encoding="utf-8").splitlines(True)
        if not line.startswith(prefix)
    ]
    path.write_text("".join(kept), encoding="utf-8")


@pytest.fixture
def doomed(mem, mktask, mkassumption):
    """A refuted assumption raised by T-001, with T-002 done underneath it."""
    root = mktask(question="root")
    child = mktask(question="child", parent=root["id"], status="done")
    assumption = mkassumption(raised_by=root["id"], status="refuted")
    return {"root": root, "child": child, "assumption": assumption}


# --- unparseable files ------------------------------------------------

def test_an_unparseable_citation_does_not_stop_the_cascade(
    mem, g, doomed, mktask, mkcitation, mkfact, mkhypothesis
):
    # The citing fact is provenanced to a task OUTSIDE the affected set
    # (a third, unrelated root) so it stays active through the cascade's
    # own quarantine pass. A fact provenanced inside the affected set
    # would be quarantined before recompute_confidence() ever runs,
    # dropping this citation out of the live set for free and letting the
    # test pass without ever reaching live_citations()'s NodeFormatError
    # handling at all.
    survivor = mktask(question="unrelated root")
    citation = mkcitation(domain="a.com")
    mkfact(statement="e", citations=[citation["id"]], task=survivor["id"])
    mkhypothesis(supporting=[citation["id"]], task=survivor["id"])
    mem.path_for(citation["id"]).write_text("not a node at all\n",
                                           encoding="utf-8")
    result = g.cascade(doomed["assumption"]["id"])
    assert result.stale_tasks == ["T-002"]


def test_an_unparseable_fact_does_not_stop_the_cascade(
    mem, g, doomed, mkcitation, mkfact
):
    citation = mkcitation(domain="a.com")
    broken = mkfact(statement="broken", citations=[citation["id"]], task="T-002")
    healthy = mkfact(statement="healthy", citations=[citation["id"]],
                     task="T-002")
    mem.path_for(broken["id"]).write_text("garbage\n", encoding="utf-8")
    result = g.cascade(doomed["assumption"]["id"])
    assert result.quarantined_facts == [healthy["id"]]


def test_an_unparseable_hypothesis_does_not_stop_the_cascade(
    mem, g, doomed, mkcitation, mkfact, mkhypothesis
):
    citation = mkcitation(domain="a.com")
    mkfact(statement="e", citations=[citation["id"]], task="T-002")
    broken = mkhypothesis(claim="broken", supporting=[citation["id"]],
                          task="T-002")
    mem.path_for(broken["id"]).write_text("garbage\n", encoding="utf-8")
    g.cascade(doomed["assumption"]["id"])  # must not raise


def test_an_unparseable_task_does_not_stop_the_cascade(
    mem, g, doomed, mktask
):
    other = mktask(question="other", parent=doomed["root"]["id"], status="done")
    mem.path_for(other["id"]).write_text("garbage\n", encoding="utf-8")
    result = g.cascade(doomed["assumption"]["id"])
    assert result.stale_tasks == ["T-002"]


# --- schema-invalid files ---------------------------------------------

def test_a_citation_missing_its_domain_does_not_stop_the_cascade(
    mem, g, doomed, mktask, mkcitation, mkfact, mkhypothesis
):
    """NOT the shape of plan 1's Critical finding C2 -- C2 was already
    closed in plan 1's fix wave, via `.get("domain")`. Carry-forward (a)
    only asked to widen `_domains_of`'s guard from catching bare KeyError
    to also catching NodeFormatError; this pins that widened guard as a
    regression test, not a still-open crash.

    This does NOT exercise `_domains_of`'s guard, and it cannot: a
    citation missing `domain` fails `_readable("citation")`'s own
    validation, so `live_citations()` never returns it in the first
    place, and `recompute_confidence()` filters `supporting` against
    `live_citations()` before `_domains_of` is ever called -- `domains =
    self._domains_of([])` on an already-empty list, every time, regardless
    of which task the citing fact is provenanced to. What this pins is
    narrower and still real: the cascade completes -- stales T-002,
    reaches `recompute_confidence()`, and returns -- over a store that
    contains a schema-invalid citation, rather than raising from
    `live_citations()`'s own `_readable` call. The task is aimed outside
    the affected set only for consistency with its sibling tests above and
    below, not because it changes this test's outcome."""
    survivor = mktask(question="unrelated root")
    citation = mkcitation(domain="a.com")
    mkfact(statement="e", citations=[citation["id"]], task=survivor["id"])
    mkhypothesis(supporting=[citation["id"]], task=survivor["id"])
    _strip_line(mem.path_for(citation["id"]), "domain:")
    result = g.cascade(doomed["assumption"]["id"])
    assert result.stale_tasks == ["T-002"]


def test_a_fact_missing_its_provenance_does_not_stop_the_cascade(
    mem, g, doomed, mkcitation, mkfact
):
    citation = mkcitation(domain="a.com")
    broken = mkfact(statement="broken", citations=[citation["id"]], task="T-002")
    healthy = mkfact(statement="healthy", citations=[citation["id"]],
                     task="T-002")
    _strip_line(mem.path_for(broken["id"]), "provenance:")
    result = g.cascade(doomed["assumption"]["id"])
    assert result.quarantined_facts == [healthy["id"]]


def test_a_hypothesis_missing_its_confidence_does_not_stop_the_cascade(
    mem, g, doomed, mkcitation, mkfact, mkhypothesis
):
    citation = mkcitation(domain="a.com")
    mkfact(statement="e", citations=[citation["id"]], task="T-002")
    broken = mkhypothesis(claim="broken", supporting=[citation["id"]],
                          task="T-002")
    _strip_line(mem.path_for(broken["id"]), "confidence:")
    g.cascade(doomed["assumption"]["id"])  # must not raise
    # The healthy remainder still got its work done.
    assert mem.read("T-002")["status"] == "stale"


def test_a_dangling_supporting_citation_does_not_stop_the_cascade(
    mem, g, doomed, mktask, mkcitation, mkfact, mkhypothesis
):
    # This does NOT exercise `_domains_of`'s guard either. C-404 has no
    # file at all, so it is filtered out of `supporting` inside
    # recompute_confidence() -- `[c for c in hypothesis["supporting"] if c
    # in live]` -- before `_domains_of` is ever called: `_domains_of` is
    # invoked with `['C-001']` only, never `'C-404'`. What this pins is
    # that a hypothesis whose supporting list names a citation that does
    # not exist does not stop the cascade -- which is true regardless of
    # where the citing fact's task points, but the task is aimed outside
    # the affected set for consistency with its sibling tests.
    survivor = mktask(question="unrelated root")
    citation = mkcitation(domain="a.com")
    mkfact(statement="e", citations=[citation["id"]], task=survivor["id"])
    mkhypothesis(supporting=[citation["id"], "C-404"], task=survivor["id"])
    g.cascade(doomed["assumption"]["id"])  # must not raise


# --- filename-keyed writes -------------------------------------------

def test_a_fact_whose_frontmatter_id_diverges_is_written_by_filename(
    mem, g, doomed, mkcitation, mkfact
):
    """graph.py:299 used to pass fact["id"] to memory.update. A file
    F-001.md claiming `id: F-777` made that a KeyError on a node that is
    right there on disk."""
    citation = mkcitation(domain="a.com")
    fact = mkfact(statement="e", citations=[citation["id"]], task="T-002")
    path = mem.path_for(fact["id"])
    path.write_text(path.read_text(encoding="utf-8").replace(
        "id: F-001", "id: F-777"), encoding="utf-8")
    g.cascade(doomed["assumption"]["id"])  # must not raise
    assert mem.read("F-001")["status"] == "quarantined"


def test_frontier_ignores_a_malformed_task_instead_of_crashing(mem, mktask):
    good = mktask(question="good")
    bad = mktask(question="bad")
    _strip_line(mem.path_for(bad["id"]), "depends_on:")
    assert Graph(mem).frontier() == [good["id"]]


def test_frontier_treats_a_present_but_invalid_dependency_as_not_done(
    mem, mktask
):
    """A dependency that exists and parses but fails its own schema (here,
    `status` stripped) cannot have its `status` trusted -- indexing
    `self.tasks[dep]["status"]` directly, as the final `all(...)` check
    does, would raise KeyError. frontier() must treat this the same as a
    dangling dependency: blocking, not silently `all()`-true because the
    field it needs to compare happens to be gone."""
    dep = mktask(question="dep", status="done")
    mktask(question="dependent", depends_on=[dep["id"]])
    _strip_line(mem.path_for(dep["id"]), "status:")
    assert Graph(mem).frontier() == []


def test_find_cycle_ignores_a_malformed_task_instead_of_crashing(mem, mktask):
    mktask(question="good")
    bad = mktask(question="bad")
    _strip_line(mem.path_for(bad["id"]), "depends_on:")
    assert Graph(mem).find_cycle() is None


# --- round 2 CRITICAL fix: a malformed task must not exempt its subtree -
#
# Graph.tasks used to skip any task that failed its own schema. That made
# a malformed task invisible to children_map() and the depends_on closure
# alike -- not just unindexable, but a hole in the graph. A task resting
# on a premise the cascade just proved false, reachable from the refuted
# assumption only *through* the malformed node, silently kept its stale
# output. That is under-invalidation, the one failure direction this
# whole module exists to prevent -- worse than the crash it replaced.
#
# The fix splits topology from validity: Graph.tasks keeps every task
# file that merely parses, valid or not, so topology walks route THROUGH
# a malformed task instead of stopping at it. valid_task_ids() is the
# separate, explicit schema check that dispatch (frontier()) and the
# cascade's write loop consult before indexing anything beyond
# parent/depends_on.


def test_a_malformed_task_does_not_exempt_its_healthy_subtree_via_parent(
    mem, mktask, mkassumption, mkfact
):
    """Reviewer probe. T-002 is done but malformed (`depth` stripped); its
    healthy child T-003 is also done, and F-001 comes from T-003. Refuting
    the assumption raised by root T-001 must still stale T-003 and
    quarantine F-001 -- T-002 is a pass-through node in the parent
    topology, not a wall that hides its own subtree from the cascade."""
    root = mktask(question="root")                                 # T-001
    child = mktask(question="child", parent=root["id"],
                   status="done")                                  # T-002
    grandchild = mktask(question="grandchild", parent=child["id"],
                        status="done")                              # T-003
    _strip_line(mem.path_for(child["id"]), "depth:")
    mkfact(statement="e", task=grandchild["id"])                   # F-001
    assumption = mkassumption(raised_by=root["id"], status="refuted")

    result = Graph(mem).cascade(assumption["id"])  # must not raise

    assert grandchild["id"] in result.stale_tasks
    assert mem.read(grandchild["id"])["status"] == "stale"
    assert mem.read("F-001")["status"] == "quarantined"
    # The malformed task itself cannot be written -- memory.update() would
    # re-validate the merged record and raise on the field already
    # missing -- so it is recorded rather than silently dropped or
    # crashed on.
    assert child["id"] in result.skipped_tasks
    assert mem.read(child["id"])["status"] == "done"  # untouched


def test_a_malformed_task_does_not_exempt_its_healthy_dependents_via_depends_on(
    mem, mktask, mkassumption
):
    """Reviewer probe. T-003 depends on the affected T-002 and is itself
    malformed (`depth` stripped); T-004 depends on T-003. T-004 must still
    be staled -- T-003 must be a pass-through node in the depends_on
    closure, not a wall that stops the taint two hops before a healthy
    dependent."""
    root = mktask(question="root")                                  # T-001
    t2 = mktask(question="t2", parent=root["id"], status="done")     # T-002
    t3 = mktask(question="t3", depends_on=[t2["id"]],
                status="done")                                       # T-003
    t4 = mktask(question="t4", depends_on=[t3["id"]],
                status="done")                                       # T-004
    _strip_line(mem.path_for(t3["id"]), "depth:")
    assumption = mkassumption(raised_by=root["id"], status="refuted")

    result = Graph(mem).cascade(assumption["id"])  # must not raise

    assert t4["id"] in result.stale_tasks
    assert mem.read(t4["id"])["status"] == "stale"
    assert t3["id"] in result.skipped_tasks


# --- round 2 fix: skipped_tasks covers the whole store, not just the
# affected set --------------------------------------------------------
#
# A task's own corruption can be exactly what hides it (and its subtree)
# from the affected-set walk in the first place -- most visibly when the
# stripped field is `parent` itself, since a task's OWN `parent` field is
# the only thing that registers it in its true parent's children list.
# Restricting skipped_tasks to members of `affected` would silently miss
# precisely these tasks. This is not a case the cascade can route around
# -- the edge is genuinely gone, so the exemption of T-002's subtree here
# is inherent, not a bug -- but the malformed task itself must not vanish
# from view the way it used to.


def test_a_task_missing_its_own_parent_field_is_recorded_in_skipped_tasks(
    mem, mktask, mkassumption, mkfact
):
    """T-002's own `parent` field is stripped. It is genuinely unreachable
    from root's subtree -- there is no data left connecting it to root --
    so T-002 and its healthy child T-003 are NOT staled; that part is
    inherent, not a defect. What must not happen is T-002 vanishing from
    the result altogether: it still has to appear in skipped_tasks,
    because that field means "every malformed task in the store", not
    "malformed members of the affected set"."""
    root = mktask(question="root")                                  # T-001
    child = mktask(question="child", parent=root["id"],
                   status="done")                                   # T-002
    grandchild = mktask(question="grandchild", parent=child["id"],
                        status="done")                               # T-003
    _strip_line(mem.path_for(child["id"]), "parent:")
    mkfact(statement="e", task=grandchild["id"])                    # F-001
    assumption = mkassumption(raised_by=root["id"], status="refuted")

    result = Graph(mem).cascade(assumption["id"])  # must not raise

    # Inherent: the parent edge is genuinely gone, so T-002 and T-003 are
    # not discoverable from root's subtree at all.
    assert result.stale_tasks == []
    assert mem.read(grandchild["id"])["status"] == "done"
    assert mem.read("F-001")["status"] == "active"
    # Not inherent, and the actual point of this test.
    assert child["id"] in result.skipped_tasks


def test_an_unparseable_mid_tree_task_is_recorded_in_skipped_tasks(
    mem, mktask, mkassumption
):
    """Same shape as the parent-stripped case, for a task that does not
    parse at all rather than one that merely fails its schema. T-002 is
    invisible to self.tasks entirely, so it cannot register itself as
    root's child either -- inherent exemption again -- but it must still
    surface in skipped_tasks: it is a real file, on a real path the
    cascade was asked to reason about, and it could not."""
    root = mktask(question="root")                                  # T-001
    child = mktask(question="child", parent=root["id"],
                   status="done")                                   # T-002
    mem.path_for(child["id"]).write_text("garbage\n", encoding="utf-8")
    assumption = mkassumption(raised_by=root["id"], status="refuted")

    result = Graph(mem).cascade(assumption["id"])  # must not raise

    assert result.stale_tasks == []
    assert child["id"] in result.skipped_tasks


def test_a_dangling_blocks_entry_is_recorded_in_skipped_tasks(
    mem, mktask, mkassumption
):
    """assumption.blocks can name a task id with no file behind it at all
    -- fsck already reports that as a dangling reference, but the cascade
    used to drop it from `affected` with no trace of its own. It must now
    show up in skipped_tasks: the cascade was told to act on it and
    could not, which is exactly what that field exists to record."""
    root = mktask(question="root")
    assumption = mkassumption(raised_by=root["id"], blocks=["T-888"],
                              status="refuted")

    result = Graph(mem).cascade(assumption["id"])  # must not raise

    assert "T-888" in result.skipped_tasks


# --- carry-forward (f): which statuses the cascade stales -------------

@pytest.mark.parametrize("status,staled", [
    ("done", True),
    ("running", True),
    ("blocked", True),
    ("pending", False),
    ("ready", False),
    ("stale", False),
    ("abandoned", False),
])
def test_which_task_statuses_the_cascade_stales(mem, mktask, mkassumption,
                                                status, staled):
    root = mktask(question="root")
    child = mktask(question="child", parent=root["id"], status=status)
    assumption = mkassumption(raised_by=root["id"], status="refuted")
    result = Graph(mem).cascade(assumption["id"])
    assert (child["id"] in result.stale_tasks) is staled
    expected = "stale" if staled else status
    assert mem.read(child["id"])["status"] == expected


def test_staling_resets_attempts_so_the_task_gets_a_full_retry_budget(
    mem, mktask, mkassumption
):
    root = mktask(question="root")
    child = mktask(question="child", parent=root["id"], status="done",
                   attempts=2)
    assumption = mkassumption(raised_by=root["id"], status="refuted")
    Graph(mem).cascade(assumption["id"])
    assert mem.read(child["id"])["attempts"] == 0


def test_staleable_statuses_are_disjoint_from_open_statuses():
    """A task the cascade stales must not already be open, or the cascade
    would churn updated_at on tasks it is not actually requeueing."""
    assert not (set(graph_mod.STALEABLE_TASK_STATUSES)
                & set(graph_mod.OPEN_TASK_STATUSES))
