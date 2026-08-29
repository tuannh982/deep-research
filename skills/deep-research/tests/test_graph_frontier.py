import pytest

from graph import Graph


@pytest.fixture
def g(mem):
    return Graph(mem, max_depth=4)


def test_a_task_with_no_dependencies_is_on_the_frontier(g, mktask):
    task = mktask()
    assert g.frontier() == [task["id"]]


def test_a_task_waits_for_an_unfinished_dependency(g, mktask):
    first = mktask()
    mktask(depends_on=[first["id"]])
    assert g.frontier() == ["T-001"]


def test_a_task_becomes_eligible_once_its_dependency_is_done(mem, mktask):
    first = mktask()
    second = mktask(depends_on=[first["id"]])
    mem.update(first["id"], status="done")
    assert Graph(mem).frontier() == [second["id"]]


def test_a_task_needs_all_dependencies_done(mem, mktask):
    first, second = mktask(), mktask()
    third = mktask(depends_on=[first["id"], second["id"]])
    mem.update(first["id"], status="done")
    assert third["id"] not in Graph(mem).frontier()
    mem.update(second["id"], status="done")
    assert third["id"] in Graph(mem).frontier()


@pytest.mark.parametrize("status", ["done", "running", "blocked", "abandoned"])
def test_closed_statuses_are_off_the_frontier(mem, mktask, status):
    task = mktask()
    mem.update(task["id"], status=status)
    assert Graph(mem).frontier() == []


@pytest.mark.parametrize("status", ["pending", "ready", "stale"])
def test_open_statuses_are_on_the_frontier(mem, mktask, status):
    task = mktask()
    mem.update(task["id"], status=status)
    assert Graph(mem).frontier() == [task["id"]]


def test_a_task_past_the_depth_cap_is_excluded(mem, mktask):
    mktask(depth=5)
    assert Graph(mem, max_depth=4).frontier() == []


def test_a_task_at_the_depth_cap_is_included(mem, mktask):
    task = mktask(depth=4)
    assert Graph(mem, max_depth=4).frontier() == [task["id"]]


def test_a_dangling_dependency_makes_a_task_ineligible(mem, mktask):
    mktask(depends_on=["T-999"])
    assert Graph(mem).frontier() == []


def test_frontier_is_sorted(mem, mktask):
    for _ in range(3):
        mktask()
    assert Graph(mem).frontier() == ["T-001", "T-002", "T-003"]


def test_frontier_is_stable_across_repeated_calls(mem, mktask):
    for _ in range(4):
        mktask()
    graph = Graph(mem)
    assert graph.frontier() == graph.frontier()


# --- final review 3: the filename is the only trustworthy id -------------
#
# fsck round 2 already established this and carries a docstring on it, but
# Graph.tasks was still built as {t["id"]: t for t in memory.list("task")}.
# A file T-001.md whose frontmatter says `id: T-777` therefore made
# frontier() emit the phantom T-777 -- an id the scheduler would then
# dispatch and try to memory.update(), which raises KeyError because no
# T-777.md exists. Keying the DAG by filename makes the divergence a
# reporting problem for fsck instead of a crash in the loop.


def _diverge(mem, task_id, fake_id):
    path = mem.path_for(task_id)
    path.write_text(path.read_text().replace(f"id: {task_id}", f"id: {fake_id}"))


def test_a_task_whose_frontmatter_id_diverges_is_keyed_by_its_filename(
    mem, mktask
):
    task = mktask()
    _diverge(mem, task["id"], "T-777")
    graph = Graph(mem)
    assert graph.frontier() == ["T-001"]
    assert "T-777" not in graph.tasks


def test_a_task_dispatched_off_the_frontier_is_always_updatable(mem, mktask):
    """The frontier is the scheduler's dispatch list, so every id on it
    must be an id memory.update() accepts. A phantom id is not."""
    task = mktask()
    _diverge(mem, task["id"], "T-777")
    for task_id in Graph(mem).frontier():
        mem.update(task_id, status="running")  # must not raise
    assert mem.read("T-001")["status"] == "running"


# --- final review 4: memory.update must not preserve a divergence --------
#
# update() pinned identity with current["id"], reading the id back out of
# the file's own content. Given T-001.md containing `id: T-777`, updating
# T-001 wrote `id: T-777` straight back and refreshed updated_at -- the
# sole writer of the store faithfully preserving, and freshly re-stamping,
# exactly the divergence fsck exists to report. The argument is the
# authority: it is what chose the path being written.


def test_update_repairs_a_frontmatter_id_that_diverges_from_the_filename(
    mem, mktask
):
    task = mktask()
    _diverge(mem, task["id"], "T-777")
    assert mem.read("T-001")["id"] == "T-777"  # the corruption is in place

    updated = mem.update("T-001", status="done")

    assert updated["id"] == "T-001"
    assert mem.read("T-001")["id"] == "T-001"
    assert not mem.exists("T-777")
