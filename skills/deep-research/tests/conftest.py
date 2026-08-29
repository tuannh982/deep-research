from pathlib import Path

import pytest

from memory import Memory

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


@pytest.fixture
def mem(tmp_path):
    return Memory(tmp_path / "research", schema_dir=SCHEMA_DIR)


@pytest.fixture
def mktask(mem):
    def make(question="q", status="pending", depends_on=None, parent=None,
             depth=0, kind="search", attempts=0, agent="decomposer", task=None):
        return mem.create("task", {
            "question": question, "status": status,
            "depends_on": depends_on or [], "parent": parent, "depth": depth,
            "kind": kind, "attempts": attempts,
            "provenance": {"task": task, "agent": agent},
        })
    return make


@pytest.fixture
def mkcitation(mem):
    # provenance task defaults to None, matching mktask. The old default of
    # "T-001" manufactured a dangling reference in any store that did not
    # happen to also create a task, which fsck now (correctly) reports.
    # Tests that care about citation provenance pass it explicitly.
    def make(url="https://example.com/a", domain="example.com", quote="a quoted span",
             status="verified", task=None):
        return mem.create("citation", {
            "url": url, "domain": domain, "title": "t", "quote": quote,
            "quote_sha256": "0" * 64, "fetched_at": "2026-08-20T10:00:00Z",
            "http_status": 200, "status": status,
            "provenance": {"task": task, "agent": "extractor"},
        })
    return make


@pytest.fixture
def mkfact(mem):
    # provenance task defaults to None, matching mktask and mkcitation. A
    # "T-001" default manufactured a dangling reference in any store that
    # did not happen to also create a task, which fsck now (correctly)
    # reports. Tests that care about provenance pass it explicitly.
    def make(statement="s", citations=None, status="active", task=None):
        return mem.create("fact", {
            "statement": statement, "citations": citations or [],
            "status": status,
            "provenance": {"task": task, "agent": "extractor"},
        })
    return make


@pytest.fixture
def mkassumption(mem):
    def make(statement="s", raised_by="T-001", status="open", blocks=None,
             refuted_by=None):
        return mem.create("assumption", {
            "statement": statement, "raised_by": raised_by, "status": status,
            "blocks": blocks or [], "refuted_by": refuted_by,
            "provenance": {"task": raised_by, "agent": "decomposer"},
        })
    return make


@pytest.fixture
def mkhypothesis(mem):
    # provenance task defaults to None, matching mktask and mkcitation. A
    # "T-001" default manufactured a dangling reference in any store that
    # did not happen to also create a task, which fsck now (correctly)
    # reports. Tests that care about provenance pass it explicitly.
    def make(claim="c", supporting=None, counter=None, status="proposed",
             confidence=0.0, verdict=None, task=None):
        return mem.create("hypothesis", {
            "claim": claim, "supporting": supporting or [],
            "counter": counter or [], "status": status,
            "confidence": confidence, "verdict": verdict,
            "provenance": {"task": task, "agent": "hypothesizer"},
        })
    return make


@pytest.fixture
def workspace_root(tmp_path):
    """An initialised ./research/ with a stubbed-present toolchain.

    Named workspace_root rather than workspace so it cannot shadow the
    `workspace` module inside a test that imports it.

    The scope is filled in, which `research init` does NOT do — it writes
    three empty lists and the scoping skill fills them before the first
    tick. This fixture represents a workspace at the point work actually
    starts, which is after that. `scheduler.run` refuses tick 1 on an
    empty `in_scope`, so leaving it empty here would make every test that
    drives a first tick fail on a precondition it never meant to assert.
    The tests that DO mean to assert it clear the scope themselves — see
    test_scheduler.py's `_unscope`.
    """
    import runconfig as runconfig_mod
    import workspace as workspace_mod
    root = tmp_path / "research"
    workspace_mod.init(root, "why is the sky blue?",
                       which=lambda name: f"/usr/bin/{name}")
    cfg = runconfig_mod.load(root)
    cfg["scope"]["in_scope"] = ["how sunlight scatters in the atmosphere"]
    runconfig_mod.save(root, cfg)
    return root


# --- tectonic ------------------------------------------------------------
# There is no HTTP client anywhere in this codebase any more — gate 2 is a
# subagent using the harness's own WebFetch, outside this process entirely
# — which is a stronger guarantee against a stray live request than a
# fixture that patched two transports ever was. `render` still shells out
# to a real `tectonic` binary, so that guard remains below.

@pytest.fixture(autouse=True)
def no_tectonic(monkeypatch, request):
    """Make a real tectonic invocation a failing test, not a slow one.

    Opt out with @pytest.mark.usefixtures on the one skipif-guarded test
    that shells out for real, via the marker below.
    """
    if request.node.get_closest_marker("real_tectonic"):
        return

    import render

    def blocked(*args, **kwargs):
        raise AssertionError(
            f"test attempted to run tectonic for real: {args!r}")

    # Patch the SEAM, not the module. `render.subprocess` IS the global
    # subprocess module object, so setattr on its `run` would blind every
    # other caller in the suite — tests/test_deps_in_sync.py and
    # tests/test_memory.py both shell out for real and would start failing
    # with this AssertionError.
    monkeypatch.setattr(render, "_tectonic_run", blocked)
