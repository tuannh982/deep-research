import pytest

from graph import CycleError, Graph


def test_a_self_dependency_is_a_cycle(mem, mktask):
    task = mktask()
    assert Graph(mem).would_cycle(task["id"], task["id"]) is True


def test_a_direct_back_edge_is_a_cycle(mem, mktask):
    first = mktask()
    second = mktask(depends_on=[first["id"]])
    assert Graph(mem).would_cycle(first["id"], second["id"]) is True


def test_a_transitive_back_edge_is_a_cycle(mem, mktask):
    first = mktask()
    second = mktask(depends_on=[first["id"]])
    third = mktask(depends_on=[second["id"]])
    assert Graph(mem).would_cycle(first["id"], third["id"]) is True


def test_a_forward_edge_is_not_a_cycle(mem, mktask):
    first, second = mktask(), mktask()
    assert Graph(mem).would_cycle(second["id"], first["id"]) is False


def test_a_diamond_is_not_a_cycle(mem, mktask):
    root = mktask()
    left = mktask(depends_on=[root["id"]])
    right = mktask(depends_on=[root["id"]])
    bottom = mktask(depends_on=[left["id"]])
    assert Graph(mem).would_cycle(bottom["id"], right["id"]) is False


def test_add_dependency_persists_the_edge_sorted(mem, mktask):
    first, second = mktask(), mktask()
    third = mktask(depends_on=[second["id"]])
    Graph(mem).add_dependency(third["id"], first["id"])
    assert mem.read(third["id"])["depends_on"] == ["T-001", "T-002"]


def test_add_dependency_is_idempotent(mem, mktask):
    first = mktask()
    second = mktask(depends_on=[first["id"]])
    Graph(mem).add_dependency(second["id"], first["id"])
    assert mem.read(second["id"])["depends_on"] == [first["id"]]


def test_add_dependency_refuses_a_cycle(mem, mktask):
    first = mktask()
    second = mktask(depends_on=[first["id"]])
    with pytest.raises(CycleError):
        Graph(mem).add_dependency(first["id"], second["id"])
    assert mem.read(first["id"])["depends_on"] == []


def test_find_cycle_returns_none_for_an_acyclic_graph(mem, mktask):
    first = mktask()
    mktask(depends_on=[first["id"]])
    assert Graph(mem).find_cycle() is None


def test_find_cycle_reports_a_cycle_written_behind_the_guard(mem, mktask):
    first = mktask()
    second = mktask(depends_on=[first["id"]])
    mem.update(first["id"], depends_on=[second["id"]])
    cycle = Graph(mem).find_cycle()
    assert cycle is not None
    assert set(cycle) == {"T-001", "T-002"}


def test_children_map_returns_children_sorted(mem, mktask):
    parent = mktask()
    third = mktask(parent=parent["id"], depth=1)
    first = mktask(parent=parent["id"], depth=1)
    second = mktask(parent=parent["id"], depth=1)
    children = Graph(mem).children_map()
    assert children[parent["id"]] == sorted([third["id"], first["id"], second["id"]])


def test_subtree_collects_descendants_and_excludes_the_root(mem, mktask):
    root = mktask()
    child = mktask(parent=root["id"], depth=1)
    grandchild = mktask(parent=child["id"], depth=2)
    mktask()  # unrelated sibling of root
    assert Graph(mem).subtree(root["id"]) == [child["id"], grandchild["id"]]


def test_subtree_of_a_leaf_is_empty(mem, mktask):
    leaf = mktask()
    assert Graph(mem).subtree(leaf["id"]) == []


def test_subtree_raises_on_a_parent_cycle(mem, mktask):
    first = mktask()
    second = mktask(parent=first["id"], depth=1)
    mem.update(first["id"], parent=second["id"], depth=1)
    with pytest.raises(CycleError):
        Graph(mem).subtree(first["id"])


def test_root_branch_walks_up_to_the_top(mem, mktask):
    root = mktask()
    child = mktask(parent=root["id"], depth=1)
    grandchild = mktask(parent=child["id"], depth=2)
    assert Graph(mem).root_branch(grandchild["id"]) == root["id"]


def test_root_branch_of_a_root_is_itself(mem, mktask):
    root = mktask()
    assert Graph(mem).root_branch(root["id"]) == root["id"]


def test_root_branch_raises_on_a_parent_cycle(mem, mktask):
    first = mktask()
    second = mktask(parent=first["id"], depth=1)
    mem.update(first["id"], parent=second["id"], depth=1)
    with pytest.raises(CycleError):
        Graph(mem).root_branch(first["id"])


def test_add_dependency_invalidates_the_cached_task_view(mem, mktask):
    first, second = mktask(), mktask()
    graph = Graph(mem)
    graph.frontier()  # prime the cache
    graph.add_dependency(second["id"], first["id"])
    # Same instance, after a write: the cache must have been dropped.
    assert graph.tasks[second["id"]]["depends_on"] == [first["id"]]
    # And the frontier must reflect it — second is now blocked on an unfinished dep.
    assert graph.frontier() == [first["id"]]


# --- theme_of ---------------------------------------------------------

def test_theme_of_is_the_depth_one_ancestor_not_the_root(mem, mktask):
    """`research init` seeds ONE task with parent None and everything
    descends from it, so root_branch is a constant function over any real
    run — which made `saturation_branches: 2` unsatisfiable and saturation
    unreachable. The themes are the seeded root's own children: spec
    section 7's "root task branches become top-level sections"."""
    root = mktask(question="the whole question")
    theme = mktask(question="theme", parent=root["id"], depth=1)
    leaf = mktask(question="leaf", parent=theme["id"], depth=2)
    deeper = mktask(question="deeper", parent=leaf["id"], depth=3)
    graph = Graph(mem)
    assert graph.theme_of(deeper["id"]) == theme["id"]
    assert graph.theme_of(leaf["id"]) == theme["id"]
    assert graph.theme_of(theme["id"]) == theme["id"]


def test_two_themes_under_one_root_are_two_distinct_values(mem, mktask):
    """The property saturation_branches actually needs, and the one
    root_branch could never provide."""
    root = mktask(question="the whole question")
    first = mktask(question="theme one", parent=root["id"], depth=1)
    second = mktask(question="theme two", parent=root["id"], depth=1)
    under_first = mktask(question="leaf", parent=first["id"], depth=2)
    graph = Graph(mem)
    assert graph.root_branch(under_first["id"]) == graph.root_branch(
        second["id"]) == root["id"]
    assert graph.theme_of(under_first["id"]) != graph.theme_of(second["id"])


def test_the_root_task_is_its_own_theme(mem, mktask):
    """It has no depth-1 ancestor, and the function has to stay total."""
    root = mktask(question="the whole question")
    assert Graph(mem).theme_of(root["id"]) == root["id"]


def test_theme_of_raises_on_a_parent_cycle(mem, mktask):
    """Same contract as root_branch: submit catches CycleError and
    journals a null branch rather than dying inside the tick."""
    first = mktask(question="a")
    second = mktask(question="b", parent=first["id"])
    mem.update(first["id"], parent=second["id"])
    with pytest.raises(CycleError):
        Graph(mem).theme_of(second["id"])


def test_theme_of_an_unknown_task_is_itself(mem):
    """Total, like root_branch: a dangling id is fsck's finding to
    report, not this query's to raise on."""
    assert Graph(mem).theme_of("T-404") == "T-404"
