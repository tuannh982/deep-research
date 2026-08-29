"""The ./research/ workspace: layout, toolchain preflight, and init.

`research init` is phase 0 of spec section 3. Its whole job is to fail
now instead of later: a missing toolchain discovered on day three has
already cost three days of research.
"""
import shutil
from pathlib import Path

import atomicio
import journal
import memory as memory_mod
import runconfig

HELP = "create ./research/ and verify the toolchain"

# Derived from the store's own mapping rather than retyped, so adding a
# node type cannot leave init building a tree memory.py does not use.
DIRS = tuple(
    [f"memory/{name}" for name in memory_mod.DIRNAME.values()]
    + ["inbox", "inbox/applied", "inbox/rejected", "out", "sections"]
)

# Spec section 10. `uv` runs everything; `tectonic` builds the PDF in
# plan 3. Both are checked here because both are needed eventually and
# neither is worth discovering late.
REQUIRED_TOOLS = ("uv", "tectonic")

# Single source of truth for the journal filename.
JOURNAL_FILENAME = journal.FILENAME


class WorkspaceError(RuntimeError):
    """The workspace is missing, already present, or unusable."""


def skill_dir():
    """The installed skill directory — the parent of scripts/.

    Spec section 8: there is no binary on PATH, so paths printed in the
    step packet have to be absolute and derived from this file's location.
    """
    return Path(__file__).resolve().parent.parent


def preflight(*, which=None):
    """Which of the required external tools are on PATH.

    `which` is a parameter so tests can describe a machine without
    having one. The default is resolved inside the body, not bound in
    the signature: `def preflight(*, which=shutil.which)` captures
    `shutil.which` once at import time, so a test that does
    `monkeypatch.setattr(workspace.shutil, "which", ...)` has no effect
    on a call that omits `which` — the parameter still holds the
    original function object.
    """
    which = which or shutil.which
    return {
        tool: "present" if which(tool) else "missing"
        for tool in REQUIRED_TOOLS
    }


def init(root, question, *, allow_missing_tectonic=False, models=None,
         which=None):
    """Create the workspace. Refuses to touch an existing run."""
    which = which or shutil.which
    root = Path(root)
    if runconfig.path_for(root).exists():
        raise WorkspaceError(
            f"{runconfig.path_for(root)} already exists; a run's state is "
            "irreplaceable, so init will not overwrite it"
        )

    # Preflight before any write. A half-initialised workspace would then
    # block the retry, because init refuses to clobber a run.yaml.
    tools = preflight(which=which)
    if tools["uv"] == "missing":
        raise WorkspaceError(
            "preflight failed: `uv` is not on PATH. Every script in this "
            "skill runs through it. Install it and re-run init."
        )
    if tools["tectonic"] == "missing" and not allow_missing_tectonic:
        raise WorkspaceError(
            "preflight failed: `tectonic` is not on PATH, and it builds the "
            "report PDF. Install it (`brew install tectonic`), or pass "
            "--allow-missing-tectonic to start research now and be refused "
            "at the render step."
        )

    for name in DIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    atomicio.write_text(root / JOURNAL_FILENAME, "")

    cfg = runconfig.default(question, models=models)
    cfg["preflight"] = tools
    runconfig.save(root, cfg)
    return cfg


def seed_root_task(root, question):
    """Create the one task a fresh run needs to have anything to do:
    T-001, `kind="decompose"`, depth 0, over the question itself.

    Round-1 review finding: nothing else in the store creates a task from
    nothing. `apply_decompose` (apply.py) only ever creates a decompose
    task's CHILDREN, from an artifact that answers an ALREADY-dispatched
    decompose task — so without this, a fresh `init` leaves the graph
    with zero tasks, `research next` finds nothing dispatchable and
    nothing under-evidenced, and `coverage_halt` fires immediately. Spec
    section 3 phase 2 ("decomposer subagent -> initial DAG") presumes a
    task exists to dispatch the decomposer against; this is what makes
    that presumption true.

    Deliberately NOT called from `init()`. `init()`'s job is directories
    and run.yaml, full stop, and the `workspace_root` fixture — used from
    test_halt.py, test_scheduler.py and test_predicates.py onward — and
    every plain `workspace.init()` test in this file assert init() writes
    no nodes. Seeding inside init() would shift every hand-built
    fixture's task ids by one, breaking roughly two dozen assertions
    across those files for a behaviour they do not exercise. Called
    instead from `run()` below, the CLI's own `init` command, so the
    library call stays exactly as every existing test expects it.

    provenance is (task=None, agent=None): this task was not produced by
    any other task or any subagent's artifact — it is the run's own
    root, written on the operator's behalf before there is a graph to
    derive it from. That matches the schema (both fields are nullable)
    and mktask/mkcitation/mkhypothesis's own `task=None` default,
    established in plan 2 task 1 for the same reason: a placeholder id
    with nothing behind it is exactly the dangling reference fsck exists
    to report.

    Idempotent in the sense that matters: `init()` (called first, always,
    by `run()` below) refuses to touch a workspace whose run.yaml already
    exists, and raises before this function is ever reached — so this
    cannot run a second time against one run and cannot double-seed.
    """
    memory = memory_mod.Memory(root)
    return memory.create("task", {
        "question": question, "kind": "decompose", "parent": None,
        "depth": 0, "status": "pending", "depends_on": [], "attempts": 0,
        "inputs": {}, "provenance": {"task": None, "agent": None},
    })


def require(root):
    """The workspace root, or a clear error. Every command starts here."""
    root = Path(root)
    if not runconfig.path_for(root).is_file():
        raise WorkspaceError(
            f"no run at {root}: expected {runconfig.path_for(root)}. "
            "Run `research init \"<question>\"` first."
        )
    return root


def add_arguments(parser):
    parser.add_argument("question", help="the research question")
    parser.add_argument(
        "--allow-missing-tectonic", action="store_true",
        help="start research without the PDF toolchain; the render step "
             "will refuse until it is installed",
    )


def run(args):
    cfg = init(args.root, args.question,
               allow_missing_tectonic=args.allow_missing_tectonic)
    root_task = seed_root_task(args.root, args.question)
    print(f"initialised {args.root}")
    print(f"  question   {cfg['question']}")
    print(f"  preflight  uv={cfg['preflight']['uv']} "
          f"tectonic={cfg['preflight']['tectonic']}")
    print(f"  seeded     {root_task['id']} (decompose, depth 0)")
    for warning in runconfig.warnings(cfg):
        print(f"  warning    {warning}")
    print()
    print("Next: run the deep-research:research-brainstorming skill against "
          "the question, then write the agreed scope into run.yaml.")
    return 0
