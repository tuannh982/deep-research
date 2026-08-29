"""Phase 0. The expensive failure this prevents is discovering on day
three that the toolchain was never there."""
import json
from pathlib import Path

import jsonschema
import pytest

import memory as memory_mod
import research
import runconfig
import workspace

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


def _which(present):
    return lambda name: f"/usr/bin/{name}" if name in present else None


# --- preflight --------------------------------------------------------

def test_preflight_reports_both_tools_present():
    assert workspace.preflight(which=_which({"uv", "tectonic"})) == {
        "uv": "present", "tectonic": "present"}


def test_preflight_reports_a_missing_tectonic():
    assert workspace.preflight(which=_which({"uv"})) == {
        "uv": "present", "tectonic": "missing"}


def test_preflight_checks_exactly_the_two_tools_the_spec_names():
    seen = []

    def spy(name):
        seen.append(name)
        return "/usr/bin/x"

    workspace.preflight(which=spy)
    assert sorted(seen) == ["tectonic", "uv"]


# --- init -------------------------------------------------------------

def test_init_creates_every_directory(tmp_path):
    workspace.init(tmp_path / "research", "q", which=_which({"uv", "tectonic"}))
    for name in workspace.DIRS:
        assert (tmp_path / "research" / name).is_dir(), name


def test_the_memory_directories_match_what_the_store_uses(tmp_path):
    """DIRS is derived from memory.DIRNAME rather than retyped, so a new
    node type cannot leave init writing a tree the store does not use."""
    import memory
    root = tmp_path / "research"
    workspace.init(root, "q", which=_which({"uv", "tectonic"}))
    store = memory.Memory(root)
    for node_type in memory.DIRNAME:
        assert store.dir_for(node_type).is_dir(), node_type


def test_init_writes_a_loadable_run_yaml(tmp_path):
    root = tmp_path / "research"
    workspace.init(root, "Why is the sky blue?",
                   which=_which({"uv", "tectonic"}))
    cfg = runconfig.load(root)
    assert cfg["question"] == "Why is the sky blue?"
    assert cfg["status"]["phase"] == "scope"
    assert cfg["preflight"] == {"uv": "present", "tectonic": "present"}


def test_init_creates_an_empty_journal(tmp_path):
    root = tmp_path / "research"
    workspace.init(root, "q", which=_which({"uv", "tectonic"}))
    assert (root / "journal.jsonl").is_file()
    assert (root / "journal.jsonl").read_text(encoding="utf-8") == ""


def test_init_fails_when_uv_is_missing(tmp_path):
    with pytest.raises(workspace.WorkspaceError, match="uv"):
        workspace.init(tmp_path / "research", "q", which=_which({"tectonic"}))


def test_a_failed_preflight_writes_no_run_yaml(tmp_path):
    """Otherwise a half-initialised workspace blocks the retry, because
    init refuses to overwrite an existing run.yaml."""
    root = tmp_path / "research"
    with pytest.raises(workspace.WorkspaceError):
        workspace.init(root, "q", which=_which({"tectonic"}))
    assert not (root / "run.yaml").exists()


def test_init_fails_when_tectonic_is_missing(tmp_path):
    with pytest.raises(workspace.WorkspaceError, match="tectonic"):
        workspace.init(tmp_path / "research", "q", which=_which({"uv"}))


def test_a_missing_tectonic_can_be_accepted_explicitly(tmp_path):
    """Spec section 3 wants a day-zero failure; spec section 10 says
    tectonic is not installed on the target machine and plan 2 excludes
    it. The flag records the gap so plan 3's synthesize can refuse
    early with a precise message instead of failing in LaTeX."""
    root = tmp_path / "research"
    workspace.init(root, "q", allow_missing_tectonic=True,
                   which=_which({"uv"}))
    assert runconfig.load(root)["preflight"]["tectonic"] == "missing"


def test_init_refuses_to_clobber_an_existing_run(tmp_path):
    """A multi-day run's state is irreplaceable."""
    root = tmp_path / "research"
    workspace.init(root, "first", which=_which({"uv", "tectonic"}))
    with pytest.raises(workspace.WorkspaceError, match="already"):
        workspace.init(root, "second", which=_which({"uv", "tectonic"}))
    assert runconfig.load(root)["question"] == "first"


def test_init_accepts_a_model_override(tmp_path):
    root = tmp_path / "research"
    models = dict(runconfig.DEFAULT_MODELS, searcher="opus")
    workspace.init(root, "q", models=models, which=_which({"uv", "tectonic"}))
    assert runconfig.load(root)["models"]["searcher"] == "opus"


# --- seed_root_task -----------------------------------------------------

def test_init_alone_creates_no_task(tmp_path):
    """Protects the `workspace_root` fixture (conftest.py) and every test
    built on it, from test_halt.py through test_predicates.py onward, all
    of which assume a bare `init()` leaves the task store empty. Seeding
    happens in `run()` — the CLI's `init` command — not here; see
    `seed_root_task`'s own docstring for why it is not folded into
    `init()` itself.
    """
    root = tmp_path / "research"
    workspace.init(root, "q", which=_which({"uv", "tectonic"}))
    assert memory_mod.Memory(root).ids("task") == []


def test_seed_root_task_creates_one_pending_decompose_task_at_depth_zero(
    tmp_path,
):
    root = tmp_path / "research"
    workspace.init(root, "q", which=_which({"uv", "tectonic"}))
    task = workspace.seed_root_task(root, "why is the sky blue?")
    assert task["id"] == "T-001"
    assert task["kind"] == "decompose"
    assert task["depth"] == 0
    assert task["parent"] is None
    assert task["status"] == "pending"
    assert task["question"] == "why is the sky blue?"


def test_seed_root_task_validates_against_the_task_schema(tmp_path):
    root = tmp_path / "research"
    workspace.init(root, "q", which=_which({"uv", "tectonic"}))
    task = workspace.seed_root_task(root, "q")
    schema = json.loads(
        (SCHEMA_DIR / "task.json").read_text(encoding="utf-8"))
    jsonschema.validate(task, schema)


def test_seed_root_task_is_readable_back_from_the_store(tmp_path):
    root = tmp_path / "research"
    workspace.init(root, "q", which=_which({"uv", "tectonic"}))
    workspace.seed_root_task(root, "q")
    assert memory_mod.Memory(root).read("T-001")["kind"] == "decompose"


# --- require ----------------------------------------------------------

def test_require_returns_the_root_of_an_initialised_run(workspace_root):
    assert workspace.require(workspace_root) == workspace_root


def test_require_rejects_a_directory_with_no_run_yaml(tmp_path):
    with pytest.raises(workspace.WorkspaceError, match="research init"):
        workspace.require(tmp_path / "nope")


# --- the CLI face -----------------------------------------------------

def test_init_is_registered_as_a_command():
    assert research.COMMANDS["init"] is workspace


def test_every_registered_command_has_the_module_interface():
    """The dispatch table is the one place commands are wired up, and
    this is what stops a half-wired entry reaching a user."""
    for name, module in research.COMMANDS.items():
        assert isinstance(module.HELP, str) and module.HELP, name
        assert callable(module.add_arguments), name
        assert callable(module.run), name


def test_every_command_takes_a_root():
    """Every command's subparser must accept --root.

    This does NOT parse an argv, because a command's own grammar can
    reject a generic argv for reasons that have nothing to do with
    --root: a required positional (init's "question"), or — starting at
    task 13 — a required subcommand (signal's stop/stop-when/checkpoint),
    or — starting at task 16 — a required option (submit's --tick). An
    argv built for one command's shape does not fit another's, so a test
    that parses argv here would pass today and then break, silently
    turning into a landmine, the moment a later task's command adds a
    required argument of its own.

    Instead this inspects each registered subparser's own actions —
    build_parser() stashes them on parser.subcommands exactly so tests
    can do this — and checks that --root is one of its option strings.
    That is robust to whatever else the command's grammar requires.
    """
    parser = research.build_parser()
    assert set(parser.subcommands) == set(research.COMMANDS)
    for name, sub in parser.subcommands.items():
        options = {opt for action in sub._actions
                   for opt in action.option_strings}
        assert "--root" in options, name


def test_no_subcommand_prints_help_and_fails(capsys):
    assert research.main([]) == 1
    assert "usage" in capsys.readouterr().out


def test_the_cli_runs_init_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace.shutil, "which",
                        _which({"uv", "tectonic"}))
    root = tmp_path / "research"
    assert research.main(["init", "--root", str(root), "a question?"]) == 0
    assert runconfig.load(root)["question"] == "a question?"


def test_the_cli_seeds_and_dispatches_a_decompose_task(
    tmp_path, monkeypatch, capsys,
):
    """Round-1 review finding: `init()` writes no nodes, and nothing else
    in the store creates a task from scratch — `apply_decompose` only
    ever creates a decompose task's CHILDREN, from an artifact answering
    an already-dispatched decompose task. Without a seed, a fresh
    `research init` left the graph empty, and `research next` halted with
    `HALT(coverage)` before a single subagent ever ran — the loop had no
    work to start from. This drives `init` and `next` through the real
    CLI, the way an operator actually would, and checks the very first
    tick has the question itself to dispatch.
    """
    monkeypatch.setattr(workspace.shutil, "which",
                        _which({"uv", "tectonic"}))
    root = tmp_path / "research"
    question = "what drives p99 latency in service X?"
    assert research.main(["init", "--root", str(root), question]) == 0
    capsys.readouterr()
    # The scoping step between `init` and the first tick, which is what
    # SKILL.md's "Before the loop" prescribes and what scheduler.run now
    # refuses tick 1 without. Written here rather than passing
    # --allow-empty-scope, because this test is the operator path and the
    # operator path includes this.
    cfg = runconfig.load(root)
    cfg["scope"]["in_scope"] = ["where p99 latency is measured"]
    runconfig.save(root, cfg)
    assert research.main(["next", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "HALT" not in out
    assert "T-001" in out
    assert "decompose" in out
    assert question in out


def test_refusing_to_overwrite_a_run_cannot_double_seed(tmp_path, monkeypatch):
    """`seed_root_task` has no guard of its own against running twice —
    it relies entirely on `init()` refusing to touch an existing
    run.yaml, which happens first and raises before `run()` ever reaches
    the seed call. Pin that here rather than trust it by inspection.
    """
    monkeypatch.setattr(workspace.shutil, "which",
                        _which({"uv", "tectonic"}))
    root = tmp_path / "research"
    assert research.main(["init", "--root", str(root), "first"]) == 0
    assert research.main(["init", "--root", str(root), "second"]) == 1
    assert memory_mod.Memory(root).ids("task") == ["T-001"]
    assert memory_mod.Memory(root).read("T-001")["question"] == "first"


def test_the_cli_reports_a_failed_preflight_without_a_traceback(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(workspace.shutil, "which", _which(set()))
    code = research.main(["init", "--root", str(tmp_path / "research"), "q"])
    assert code == 1
    assert "uv" in capsys.readouterr().err


def test_the_declared_version_matches_pyproject():
    """Closes a plan-1 deferred minor: two copies of the version with no
    test pinning them together."""
    import tomllib
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(
        encoding="utf-8"))
    assert research.VERSION == project["project"]["version"]
