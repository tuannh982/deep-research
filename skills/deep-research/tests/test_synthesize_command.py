import pytest

import research
import runconfig
import scheduler
import synthesis
import workspace


@pytest.fixture
def halted(tmp_path, mem, mktask, mkhypothesis):
    root = tmp_path / "research"
    workspace.init(root, "why is the sky blue?",
                   which=lambda name: f"/usr/bin/{name}")
    # `init` leaves `scope` empty and scheduler.run refuses tick 1 that
    # way. This fixture represents a run already well past its first
    # tick, so it has been scoped.
    import runconfig as runconfig_mod
    scoped = runconfig_mod.load(root)
    scoped["scope"]["in_scope"] = ["how sunlight scatters in the atmosphere"]
    runconfig_mod.save(root, scoped)
    import memory as memory_mod
    store = memory_mod.Memory(root)

    def task(question, parent=None, depth=0, kind="search", status="done"):
        return store.create("task", {
            "question": question, "kind": kind, "parent": parent,
            "depth": depth, "status": status, "depends_on": [], "attempts": 0,
            "inputs": {}, "provenance": {"task": None, "agent": None}})["id"]

    root_task = task("why is the sky blue?", kind="decompose")
    theme = task("optical scattering", parent=root_task, depth=1)
    store.create("hypothesis", {
        "claim": "Rayleigh explains it", "supporting": [], "counter": [],
        "status": "supported", "confidence": 0.75, "verdict": "supported",
        "provenance": {"task": theme, "agent": "hypothesizer"}})
    return root, store


def _run(root, *argv):
    parser = research.build_parser()
    args = parser.parse_args(["synthesize", "--root", str(root), *argv])
    return synthesis.run(args)


def test_synthesize_seeds_exactly_one_outline_task(halted, capsys):
    root, store = halted
    assert _run(root) == 0
    outline_tasks = [t for t in store.ids("task")
                     if store.read(t)["kind"] == "outline"]
    assert len(outline_tasks) == 1


def test_the_seeded_task_carries_the_computed_outline(halted):
    root, store = halted
    _run(root)
    task = next(store.read(t) for t in store.ids("task")
                if store.read(t)["kind"] == "outline")
    assert task["inputs"]["outline"]["sections"][0]["title"] == \
        "optical scattering"


def test_synthesize_moves_the_run_into_the_synthesize_phase(halted):
    root, _ = halted
    _run(root)
    assert runconfig.load(root)["status"]["phase"] == "synthesize"


def test_next_after_synthesize_does_not_clobber_the_phase(halted):
    """`next` is the very command `synthesize` tells the operator to run, and
    it wrote `phase = "research"` unconditionally — so the synthesize phase
    never survived one tick. Once the writers finish and the loop halts,
    `research status` then reached its `elif halted:` branch and offered
    `research continue` or `research synthesize`, never `research render`:
    exactly the omission Task 16 existed to fix.

    Drives the real `next`, not a hand-set phase — the pre-existing
    test_status_points_at_render_during_the_synthesize_phase sets the field
    by hand and never calls `next`, which is why nothing caught this."""
    root, _ = halted
    _run(root)
    assert runconfig.load(root)["status"]["phase"] == "synthesize"

    parser = research.build_parser()
    assert scheduler.run(parser.parse_args(["next", "--root", str(root)])) == 0
    assert runconfig.load(root)["status"]["phase"] == "synthesize"


def test_next_still_sets_the_research_phase_during_research(tmp_path):
    """The guard must not stop `next` writing the phase it is responsible
    for. Without this, "never overwrite" would pass the test above while
    silently leaving every research-phase run's phase at whatever `init`
    happened to write."""
    root = tmp_path / "research"
    workspace.init(root, "q", which=lambda name: f"/usr/bin/{name}")
    workspace.seed_root_task(root, "q")
    cfg = runconfig.load(root)
    cfg["status"]["phase"] = "scope"
    # This test drives a real first tick, which scheduler.run refuses on
    # an empty `in_scope`. Scoped rather than waved through with
    # --allow-empty-scope: the assertion is about which phase `next`
    # writes, and a flag in the argv would put an unrelated code path
    # between the test and the thing it checks.
    cfg["scope"]["in_scope"] = ["what the question is actually asking"]
    runconfig.save(root, cfg)

    parser = research.build_parser()
    assert scheduler.run(parser.parse_args(["next", "--root", str(root)])) == 0
    assert runconfig.load(root)["status"]["phase"] == "research"


def test_synthesize_clears_a_recorded_halt(halted):
    """The run halted on coverage; synthesis is the way forward from that.
    Leaving the halt set would make the very next `next` print HALT again
    and refuse to dispatch the outline task."""
    root, _ = halted
    cfg = runconfig.load(root)
    # run.json requires at_tick/at alongside reason/detail (see
    # halt.record); a literal missing them would fail runconfig.save's own
    # schema validation before synthesize is ever exercised.
    cfg["status"]["halted"] = {"reason": "coverage", "detail": "done",
                               "at_tick": 0, "at": "2026-08-22T10:00:00Z"}
    runconfig.save(root, cfg)
    _run(root)
    assert runconfig.load(root)["status"]["halted"] is None


def test_running_synthesize_twice_does_not_seed_a_second_task(halted):
    root, store = halted
    _run(root)
    _run(root)
    outline_tasks = [t for t in store.ids("task")
                     if store.read(t)["kind"] == "outline"]
    assert len(outline_tasks) == 1


def test_synthesize_refuses_while_research_is_still_dispatchable(halted,
                                                                 capsys):
    """Computing an outline from a half-finished graph produces a report
    that silently omits whatever the outstanding tasks would have found."""
    root, store = halted
    store.create("task", {
        "question": "still to do", "kind": "search", "parent": None,
        "depth": 0, "status": "pending", "depends_on": [], "attempts": 0,
        "inputs": {}, "provenance": {"task": None, "agent": None}})
    assert _run(root) == 1
    assert "still dispatchable" in capsys.readouterr().err


def test_re_running_synthesize_does_not_call_the_outliner_a_research_task(
        halted, capsys):
    """After `synthesize`, the only dispatchable task left is the outline
    task this command just seeded. Re-running it then reported "1 research
    task(s) are still dispatchable, so an outline computed now would omit
    whatever they find" — but that task IS the outline, it finds nothing,
    and the operator is told to run a loop they have already been told to
    run. Naming it accurately is the difference between an actionable
    message and a confusing one."""
    root, _ = halted
    _run(root)
    capsys.readouterr()
    assert _run(root) == 1
    err = capsys.readouterr().err
    assert "research task(s)" not in err
    assert "synthesis is already under way" in err


def test_outstanding_research_work_is_still_called_research(halted, capsys):
    """The reword must not swallow the real case. A pending SEARCH task is
    research, and computing an outline while one is open silently omits
    whatever it would have found — which is the whole reason for the
    guard."""
    root, store = halted
    store.create("task", {
        "question": "still to do", "kind": "search", "parent": None,
        "depth": 0, "status": "pending", "depends_on": [], "attempts": 0,
        "inputs": {}, "provenance": {"task": None, "agent": None}})
    _run(root)
    err = capsys.readouterr().err
    assert "research task(s)" in err


def test_force_overrides_the_outstanding_work_check(halted):
    root, store = halted
    store.create("task", {
        "question": "still to do", "kind": "search", "parent": None,
        "depth": 0, "status": "pending", "depends_on": [], "attempts": 0,
        "inputs": {}, "provenance": {"task": None, "agent": None}})
    assert _run(root, "--force") == 0


def test_synthesize_refuses_when_there_is_nothing_to_report(tmp_path, capsys):
    root = tmp_path / "research"
    workspace.init(root, "q", which=lambda name: f"/usr/bin/{name}")
    assert _run(root) == 1
    assert "no findings" in capsys.readouterr().err


def test_synthesize_prints_the_outline_it_seeded(halted, capsys):
    root, _ = halted
    _run(root)
    out = capsys.readouterr().out
    assert "S-001" in out and "optical scattering" in out
    assert "research next" in out


def test_synthesize_is_registered_as_a_command():
    assert research.COMMANDS["synthesize"] is synthesis
