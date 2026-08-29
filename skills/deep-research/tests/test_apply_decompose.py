"""Applying a decompose or search artifact.

Every test that runs the same apply twice is testing spec section 8's
recovery promise: re-running submit after a crash must converge, not
duplicate."""
import pytest

import apply
import runconfig
from graph import Graph


@pytest.fixture
def cfg():
    return runconfig.default("why is the sky blue?")


@pytest.fixture
def parent(mktask):
    return mktask(question="What drives p99 latency?", kind="decompose")


def decompose_artifact(task_id, **overrides):
    artifact = {
        "task_id": task_id,
        "children": [
            {"question": "What is the p99 today?", "kind": "search",
             "rationale": "baseline", "depends_on_index": []},
            {"question": "What changed last release?", "kind": "search",
             "rationale": "needs the baseline first", "depends_on_index": [0]},
        ],
        "assumptions": [
            {"statement": "v3 is the current release", "blocks_index": [1]},
        ],
    }
    artifact.update(overrides)
    return artifact


def run_decompose(mem, cfg, parent):
    return apply.apply_decompose(
        mem, Graph(mem, max_depth=cfg["config"]["max_depth"]), cfg,
        parent["id"], parent, decompose_artifact(parent["id"]))


# --- children ---------------------------------------------------------

def test_children_are_created_under_the_parent(mem, cfg, parent):
    run_decompose(mem, cfg, parent)
    children = [t for t in mem.list("task") if t["parent"] == parent["id"]]
    assert sorted(c["question"] for c in children) == [
        "What changed last release?", "What is the p99 today?"]


def test_a_child_is_one_level_deeper_than_its_parent(mem, cfg, parent):
    run_decompose(mem, cfg, parent)
    child = [t for t in mem.list("task") if t["parent"] == parent["id"]][0]
    assert child["depth"] == parent["depth"] + 1


def test_a_child_carries_the_parent_task_as_its_provenance(mem, cfg, parent):
    """Without this the invalidation cascade cannot reach it: it matches
    on provenance.task."""
    run_decompose(mem, cfg, parent)
    child = [t for t in mem.list("task") if t["parent"] == parent["id"]][0]
    assert child["provenance"] == {"task": parent["id"],
                                   "agent": "decomposer"}


def test_children_start_pending(mem, cfg, parent):
    run_decompose(mem, cfg, parent)
    child = [t for t in mem.list("task") if t["parent"] == parent["id"]][0]
    assert child["status"] == "pending"


def test_the_result_lists_what_it_created(mem, cfg, parent):
    result = run_decompose(mem, cfg, parent)
    assert len(result.created) == 3  # two tasks and one assumption
    assert result.created == sorted(result.created)


# --- sibling dependencies by index -----------------------------------

def test_a_sibling_index_becomes_a_real_dependency(mem, cfg, parent):
    """The decomposer has no graph access, so it names siblings by
    position and code resolves them."""
    run_decompose(mem, cfg, parent)
    later = next(t for t in mem.list("task")
                 if t["question"] == "What changed last release?")
    earlier = next(t for t in mem.list("task")
                   if t["question"] == "What is the p99 today?")
    assert later["depends_on"] == [earlier["id"]]


def test_the_graph_cache_is_invalidated_before_dependencies_are_added(
    mem, cfg, parent
):
    """add_dependency indexes graph.tasks. Inside submit, the scheduler
    has typically already populated that cache (e.g. via frontier())
    before apply runs, so this test populates it too by touching
    `graph.tasks` before calling apply_decompose. Without an invalidate
    between task creation and dependency resolution, that stale cache
    raises KeyError on a task that is right there on disk -- the exact
    bug class plan 1's reviews kept finding.

    Do NOT "simplify" this back to a fresh `Graph` passed straight to
    apply_decompose (e.g. via run_decompose): a never-touched cache is
    `None` until first read, so invalidate_cache() on it is a no-op and
    this test would pass whether or not the real call is even there --
    which is exactly what happened the first time this test was written.
    """
    graph = Graph(mem, max_depth=cfg["config"]["max_depth"])
    graph.tasks  # populate the cache before the children exist
    apply.apply_decompose(mem, graph, cfg, parent["id"], parent,
                          decompose_artifact(parent["id"]))  # must not raise
    later = next(t for t in mem.list("task")
                 if t["question"] == "What changed last release?")
    assert later["depends_on"]


def test_an_out_of_range_sibling_index_is_an_apply_error(mem, cfg, parent):
    artifact = decompose_artifact(parent["id"])
    artifact["children"][1]["depends_on_index"] = [7]
    with pytest.raises(apply.ApplyError, match="7"):
        apply.apply_decompose(mem, Graph(mem), cfg, parent["id"], parent,
                              artifact)


def test_a_self_dependency_is_dropped_not_fatal(mem, cfg, parent):
    """Reachable through dedup: two identical children collapse to one
    id, and a dependency between them becomes self-referential. Burning
    the task's attempts over that would be wrong."""
    artifact = decompose_artifact(parent["id"])
    artifact["children"][1] = dict(artifact["children"][0],
                                   depends_on_index=[0])
    result = apply.apply_decompose(mem, Graph(mem), cfg, parent["id"],
                                   parent, artifact)
    assert any("self-depend" in reason for _, reason in result.dropped)


# --- assumptions ------------------------------------------------------

def test_an_assumption_is_created_and_blocks_the_named_children(
    mem, cfg, parent
):
    run_decompose(mem, cfg, parent)
    assumption = mem.list("assumption")[0]
    later = next(t for t in mem.list("task")
                 if t["question"] == "What changed last release?")
    assert assumption["blocks"] == [later["id"]]
    assert assumption["status"] == "open"


def test_an_assumption_is_raised_by_the_decomposed_task(mem, cfg, parent):
    """`blocks` extends the cascade's affected set past the raiser's own
    subtree, and `raised_by` is where that subtree starts."""
    run_decompose(mem, cfg, parent)
    assert mem.list("assumption")[0]["raised_by"] == parent["id"]


def test_an_assumption_with_no_blocks_is_still_created(mem, cfg, parent):
    artifact = decompose_artifact(parent["id"])
    artifact["assumptions"][0]["blocks_index"] = []
    apply.apply_decompose(mem, Graph(mem), cfg, parent["id"], parent,
                          artifact)
    assert mem.list("assumption")[0]["blocks"] == []


def test_an_out_of_range_blocks_index_is_dropped_not_fatal(mem, cfg, parent):
    artifact = decompose_artifact(parent["id"])
    artifact["assumptions"][0]["blocks_index"] = [0, 99]
    result = apply.apply_decompose(mem, Graph(mem), cfg, parent["id"],
                                   parent, artifact)
    assert mem.list("assumption")[0]["blocks"] == ["T-002"]
    assert any("99" in reason for _, reason in result.dropped)


# --- the depth cap ----------------------------------------------------

def test_a_child_that_would_exceed_the_depth_cap_is_never_created(
    mem, cfg, mktask
):
    """Carry-forward (e), prevention half. An over-cap pending task would
    block the coverage halt predicate forever, so it must not come into
    existence. Graph.over_cap reports any that appear anyway."""
    cfg["config"]["max_depth"] = 2
    deep = mktask(question="deep", depth=2, kind="decompose")
    result = apply.apply_decompose(
        mem, Graph(mem, max_depth=2), cfg, deep["id"], deep,
        decompose_artifact(deep["id"]))
    assert [t for t in mem.list("task") if t["parent"] == deep["id"]] == []
    # Scoped to the task drops on purpose. The artifact's assumption
    # blocks child 1, which was not created, so `dropped` also carries a
    # ("blocks", ...) entry that says nothing about depth.
    task_drops = [reason for what, reason in result.dropped if what == "task"]
    assert len(task_drops) == 2
    assert all("depth" in reason for reason in task_drops)


def test_a_dropped_child_leaves_the_graph_within_the_cap(mem, cfg, mktask):
    cfg["config"]["max_depth"] = 2
    deep = mktask(question="deep", depth=2, kind="decompose")
    apply.apply_decompose(mem, Graph(mem, max_depth=2), cfg, deep["id"],
                          deep, decompose_artifact(deep["id"]))
    assert Graph(mem, max_depth=2).over_cap() == []


def test_a_child_exactly_at_the_cap_is_created(mem, cfg, mktask):
    cfg["config"]["max_depth"] = 3
    parent = mktask(question="p", depth=2, kind="decompose")
    apply.apply_decompose(mem, Graph(mem, max_depth=3), cfg, parent["id"],
                          parent, decompose_artifact(parent["id"]))
    children = [t for t in mem.list("task") if t["parent"] == parent["id"]]
    assert len(children) == 2
    assert all(c["depth"] == 3 for c in children)


# --- idempotence ------------------------------------------------------

def test_applying_the_same_decompose_twice_creates_nothing_new(
    mem, cfg, parent
):
    """Spec section 8: re-running the same submit is the crash recovery
    path. It has to converge."""
    run_decompose(mem, cfg, parent)
    before = mem.all_ids()
    second = run_decompose(mem, cfg, parent)
    assert mem.all_ids() == before
    assert second.created == []
    assert len(second.reused) == 3


def test_a_reapplied_dependency_is_not_duplicated(mem, cfg, parent):
    run_decompose(mem, cfg, parent)
    run_decompose(mem, cfg, parent)
    later = next(t for t in mem.list("task")
                 if t["question"] == "What changed last release?")
    assert len(later["depends_on"]) == 1


def test_a_second_apply_does_not_reset_a_completed_child(mem, cfg, parent):
    """The dangerous version of a non-idempotent apply: the recovery
    re-run undoes work the first run finished."""
    run_decompose(mem, cfg, parent)
    child = next(t for t in mem.list("task")
                 if t["question"] == "What is the p99 today?")
    mem.update(child["id"], status="done")
    run_decompose(mem, cfg, parent)
    assert mem.read(child["id"])["status"] == "done"


def test_two_different_parents_may_ask_the_same_question(mem, cfg, mktask):
    """The natural key includes the parent, so an identical sub-question
    under two branches is two tasks, not one shared node."""
    first = mktask(question="a", kind="decompose")
    second = mktask(question="b", kind="decompose")
    for task in (first, second):
        apply.apply_decompose(mem, Graph(mem), cfg, task["id"], task,
                              decompose_artifact(task["id"]))
    questions = [t["question"] for t in mem.list("task")]
    assert questions.count("What is the p99 today?") == 2


# --- search -----------------------------------------------------------

def search_artifact(task_id, urls=("https://a-example.com/x",
                                   "https://b-example.com/y")):
    return {
        "task_id": task_id,
        "sources": [{"url": url, "title": f"T{i}", "relevance": 0.8,
                     "why": "relevant"} for i, url in enumerate(urls)],
        "queries": ["a search query"],
        "no_sources_reason": None,
    }


@pytest.fixture
def searcher(mktask):
    return mktask(question="Find sources on p99 latency", kind="search",
                  depth=1)


def test_each_source_becomes_an_extract_task(mem, cfg, searcher):
    apply.apply_search(mem, Graph(mem), cfg, searcher["id"], searcher,
                       search_artifact(searcher["id"]))
    extracts = [t for t in mem.list("task") if t["kind"] == "extract"]
    assert len(extracts) == 2


def test_an_extract_task_carries_its_url_in_inputs(mem, cfg, searcher):
    """There is nowhere else to put it. The task question is shared; the
    URL is what distinguishes one extract task from the next."""
    apply.apply_search(mem, Graph(mem), cfg, searcher["id"], searcher,
                       search_artifact(searcher["id"]))
    urls = sorted(t["inputs"]["url"] for t in mem.list("task")
                  if t["kind"] == "extract")
    assert urls == ["https://a-example.com/x", "https://b-example.com/y"]


def test_an_extract_task_carries_the_registrable_domain(mem, cfg, searcher):
    """domains.registrable is called here, at creation, so the eTLD+1 is
    computed once by the one function that knows how -- rather than by
    whatever reads the URL later."""
    apply.apply_search(mem, Graph(mem), cfg, searcher["id"], searcher,
                       search_artifact(searcher["id"],
                                       urls=("https://blog.foo.com/a",)))
    extract = next(t for t in mem.list("task") if t["kind"] == "extract")
    assert extract["inputs"]["domain"] == "foo.com"


def test_an_extract_task_inherits_the_search_task_s_depth(mem, cfg, searcher):
    """Depth counts decomposition, not pipeline steps. Otherwise a search
    task at max_depth could never extract anything and the pipeline
    dead-ends exactly at the cap."""
    apply.apply_search(mem, Graph(mem), cfg, searcher["id"], searcher,
                       search_artifact(searcher["id"]))
    extract = next(t for t in mem.list("task") if t["kind"] == "extract")
    assert extract["depth"] == searcher["depth"]


def test_a_search_task_at_the_depth_cap_still_spawns_extract_tasks(
    mem, cfg, mktask
):
    cfg["config"]["max_depth"] = 2
    searcher = mktask(question="find", kind="search", depth=2)
    apply.apply_search(mem, Graph(mem, max_depth=2), cfg, searcher["id"],
                       searcher, search_artifact(searcher["id"]))
    assert len([t for t in mem.list("task") if t["kind"] == "extract"]) == 2
    assert Graph(mem, max_depth=2).over_cap() == []


def test_a_url_with_no_registrable_domain_is_dropped_not_fatal(
    mem, cfg, searcher
):
    """A searcher can return http://localhost/ or an IDN we cannot
    reduce. One bad source must not fail the whole artifact."""
    result = apply.apply_search(
        mem, Graph(mem), cfg, searcher["id"], searcher,
        search_artifact(searcher["id"],
                        urls=("https://localhost/x", "https://ok-example.com/y")))
    assert len([t for t in mem.list("task") if t["kind"] == "extract"]) == 1
    assert any("localhost" in reason for _, reason in result.dropped)


def test_two_sources_from_one_site_still_become_two_tasks(mem, cfg, searcher):
    """Pins that two sources sharing one registrable domain still become
    two extract tasks. This does not, on its own, prove TASK_KEY needs
    `inputs`: apply_search bakes each source's URL into the generated
    question text, so these two tasks already differ on `question` alone
    -- see test_task_key_distinguishes_tasks_that_differ_only_by_inputs
    for the direct proof that `inputs` is what does that work inside
    TASK_KEY itself."""
    apply.apply_search(
        mem, Graph(mem), cfg, searcher["id"], searcher,
        search_artifact(searcher["id"],
                        urls=("https://foo.com/a", "https://foo.com/b")))
    assert len([t for t in mem.list("task") if t["kind"] == "extract"]) == 2


def test_applying_the_same_search_twice_creates_nothing_new(
    mem, cfg, searcher
):
    apply.apply_search(mem, Graph(mem), cfg, searcher["id"], searcher,
                       search_artifact(searcher["id"]))
    before = mem.all_ids()
    second = apply.apply_search(mem, Graph(mem), cfg, searcher["id"],
                                searcher, search_artifact(searcher["id"]))
    assert mem.all_ids() == before
    assert second.created == []
    assert len(second.reused) == 2


def test_an_empty_source_list_creates_nothing_and_does_not_raise(
    mem, cfg, searcher
):
    result = apply.apply_search(
        mem, Graph(mem), cfg, searcher["id"], searcher,
        {"task_id": searcher["id"], "sources": [],
         "queries": ["a search query"],
         "no_sources_reason": "every result was a vendor blog"})
    assert result.created == []
    assert [t for t in mem.list("task") if t["kind"] == "extract"] == []


# --- shared machinery -------------------------------------------------

def test_index_of_skips_a_corrupt_node(mem, mktask):
    """apply.py reads the whole store to dedup. One bad file must not
    take submit down."""
    good = mktask(question="good")
    bad = mktask(question="bad")
    mem.path_for(bad["id"]).write_text("garbage\n", encoding="utf-8")
    index = apply.index_of(mem, "task", apply.TASK_KEY)
    assert good["id"] in index.values()
    assert bad["id"] not in index.values()


def test_index_of_keeps_the_lowest_id_on_a_duplicate_key(mem, mktask):
    """Determinism: two nodes with one natural key must always resolve to
    the same one, or a re-applied artifact points somewhere new."""
    mktask(question="same")
    mem.create("task", {
        "question": "same", "status": "pending", "depends_on": [],
        "parent": None, "depth": 0, "kind": "search", "attempts": 0,
        "provenance": {"task": None, "agent": "decomposer"},
    })
    index = apply.index_of(mem, "task", apply.TASK_KEY)
    assert index[apply.TASK_KEY(mem.read("T-001"))] == "T-001"


def test_task_key_distinguishes_tasks_that_differ_only_by_inputs():
    """The direct proof that `inputs` does work inside TASK_KEY itself,
    independent of any applier's behaviour. `test_two_sources_from_one_
    site_still_become_two_tasks` exercises apply_search end to end, but
    that test would pass even with `inputs` removed from TASK_KEY,
    because apply_search happens to bake the URL into the generated
    question text too. This test cannot be fooled that way: `question`
    is held fixed and only `inputs` varies."""
    base = {"parent": "T-001", "kind": "extract", "question": "q"}
    a = apply.TASK_KEY({**base, "inputs": {"url": "https://a-example.com/x"}})
    b = apply.TASK_KEY({**base, "inputs": {"url": "https://b-example.com/y"}})
    assert a != b


def test_task_key_treats_missing_none_and_empty_inputs_the_same():
    """The schema-optional boundary: a plan-1-shaped task has no `inputs`
    key at all. That must key identically to a task explicitly written
    with `inputs=None` or `inputs={}` (what create_task actually stores),
    or re-applying an artifact against an old-shaped store would look
    like a brand-new task every time."""
    base = {"parent": "T-001", "kind": "search", "question": "q"}
    missing = apply.TASK_KEY(dict(base))
    none_valued = apply.TASK_KEY({**base, "inputs": None})
    empty = apply.TASK_KEY({**base, "inputs": {}})
    assert missing == none_valued == empty


def test_canonical_is_order_independent():
    assert apply.canonical({"b": 1, "a": 2}) == apply.canonical({"a": 2, "b": 1})


def test_canonical_distinguishes_different_values():
    assert apply.canonical({"url": "a"}) != apply.canonical({"url": "b"})


def test_canonical_of_none_and_of_empty_agree():
    """A task created with inputs=None and one created with inputs={}
    are the same task; the schema stores {} for both."""
    assert apply.canonical(None) == apply.canonical({})


def test_every_artifact_kind_has_an_applier():
    """Derived, not restated. A kind must appear in gates.ARTIFACT_KINDS,
    apply.APPLIERS and submit.KIND_ORDER together — artifact_schema raises
    on a kind it does not know and submit does not catch it, so a kind
    wired into one table but not the others takes down the whole tick."""
    import gates
    assert sorted(apply.APPLIERS) == sorted(gates.ARTIFACT_KINDS)
