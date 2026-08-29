"""The four operations a human runs between ticks."""
import re
import shlex
from pathlib import Path

import pytest

import halt
import journal
import research
import runconfig
from graph import Graph
from memory import Memory


@pytest.fixture
def run(workspace_root):
    return workspace_root


@pytest.fixture
def mem(run):
    return Memory(run)


# --- status -----------------------------------------------------------

def test_status_prints_the_question_and_the_tick(run, mem, mktask, capsys):
    mktask(question="some work")
    assert research.main(["status", "--root", str(run)]) == 0
    out = capsys.readouterr().out
    assert "why is the sky blue?" in out
    assert "tick 0" in out.lower()


def test_status_uses_the_same_renderer_as_the_halt_path(run, mem, mktask,
                                                        capsys):
    """One renderer, two sinks. If they diverge, out/status.md stops
    matching what the user was told."""
    dead = mktask(question="could not answer")
    mem.update(dead["id"], status="abandoned", abandoned_reason="gave up")
    research.main(["status", "--root", str(run)])
    printed = capsys.readouterr().out
    rendered = halt.render_status(mem, Graph(mem), runconfig.load(run),
                                 journal.read(run), None)
    assert rendered.strip() in printed


def test_status_shows_a_stored_halt(run, mem, mktask, capsys):
    task = mktask(question="q")
    mem.update(task["id"], status="done")
    research.main(["next", "--root", str(run)])
    capsys.readouterr()
    research.main(["status", "--root", str(run)])
    assert "HALTED(coverage)" in capsys.readouterr().out


def test_status_shows_a_pending_checkpoint(run, mem, capsys):
    research.main(["signal", "--root", str(run), "checkpoint",
                   "--note", "talk to me"])
    capsys.readouterr()
    research.main(["status", "--root", str(run)])
    assert "talk to me" in capsys.readouterr().out


def test_status_works_on_an_empty_run(run, capsys):
    assert research.main(["status", "--root", str(run)]) == 0


# --- resume -----------------------------------------------------------

def test_resume_reprints_the_loop_protocol(run, capsys):
    """Spec section 4: 'research resume reprints the protocol if even
    that is lost.' The recovery of last resort."""
    assert research.main(["resume", "--root", str(run)]) == 0
    out = capsys.readouterr().out
    assert "research next" in out
    assert "research submit" in out
    assert "research signal" in out


def test_resume_names_the_current_tick(run, mem, mktask, capsys):
    mktask(question="q", kind="search")
    research.main(["next", "--root", str(run)])
    capsys.readouterr()
    research.main(["resume", "--root", str(run)])
    assert "tick 1" in capsys.readouterr().out.lower()


def test_resume_gives_absolute_paths(run, capsys):
    """After a compaction the agent may not know where it is. A relative
    path is useless in that state."""
    research.main(["resume", "--root", str(run)])
    out = capsys.readouterr().out
    assert str(run) in out
    assert "scripts/research.py" in out


def test_resume_says_a_tick_is_in_flight_when_one_is(run, mem, mktask,
                                                     capsys):
    mktask(question="q", kind="search")
    research.main(["next", "--root", str(run)])
    capsys.readouterr()
    research.main(["resume", "--root", str(run)])
    assert "submit --tick 1" in capsys.readouterr().out


def test_resume_does_not_dispatch_anything(run, mem, mktask):
    """It is a print command. Anything else would make the recovery path
    change the state it is recovering."""
    task = mktask(question="q", kind="search")
    research.main(["resume", "--root", str(run)])
    assert mem.read(task["id"])["status"] == "pending"
    assert journal.read(run) == []


def test_resume_resolves_a_relative_root(run, monkeypatch, capsys):
    """resume exists for exactly the moment a context compaction has
    wiped the conversation and the agent no longer knows where it is. A
    cwd-relative --root in the printed recovery command is unusable in
    precisely that state, even though `--root research` (argparse's own
    default) is a perfectly normal way to invoke it."""
    monkeypatch.chdir(run.parent)
    assert research.main(["resume", "--root", "research"]) == 0
    out = capsys.readouterr().out
    assert str(run) in out


def test_resume_prints_a_command_form_that_actually_parses(run, capsys):
    """Round-1 review finding: `--root` is registered on each subcommand's
    own subparser (research.build_parser adds it after the subparsers
    object exists), not on the top-level parser, so a printed form of
    `research.py --root <root> <command>` is rejected by argparse before
    a command ever runs. The prior test here (test_resume_gives_
    absolute_paths) only checked that the path was absolute and that
    'scripts/research.py' appeared somewhere in the text — it never
    checked the printed command was valid syntax, which is why a broken
    order survived. This extracts the template resume actually prints,
    substitutes a real command into it, and feeds the result to the real
    parser, so a future regression in argument order fails here instead
    of surviving to a user recovering from a compaction.
    """
    research.main(["resume", "--root", str(run)])
    out = capsys.readouterr().out
    match = re.search(
        r"research <command> \[args\]\s+=\s+(.+)$", out, re.MULTILINE)
    assert match, out
    template = match.group(1)
    concrete = (template.replace("<command>", "next")
                        .replace("[args]", "").strip())
    tokens = shlex.split(concrete)
    assert tokens[0:2] == ["uv", "run"]
    # tokens[2] is the script path; everything after it is the argv a
    # real invocation would pass to research.py.
    argv = tokens[3:]
    parsed = research.build_parser().parse_args(argv)
    assert parsed.command == "next"
    assert Path(parsed.root).resolve() == Path(run).resolve()


def test_resume_survives_a_dispatched_record_missing_task_ids(run, capsys):
    """journal.read() only guarantees a surviving record is valid-JSON and
    a dict, per its own docstring — not that it has any particular shape.
    A hand-edited or older-format journal.jsonl can lack task_ids, and the
    recovery-of-last-resort command must degrade, not crash."""
    cfg = runconfig.load(run)
    cfg["status"]["tick"] = 1
    runconfig.save(run, cfg)
    journal.append(run, "dispatched", tick=1)
    assert research.main(["resume", "--root", str(run)]) == 0


# --- continue ---------------------------------------------------------

def test_continue_clears_a_stored_halt(run, mem, mktask, capsys):
    task = mktask(question="q")
    mem.update(task["id"], status="done")
    research.main(["next", "--root", str(run)])
    assert runconfig.load(run)["status"]["halted"] is not None
    assert research.main(["continue", "--root", str(run)]) == 0
    assert runconfig.load(run)["status"]["halted"] is None


def test_continue_clears_the_stop_request(run, capsys):
    """Otherwise the loop halts again on the next tick and `continue`
    looks broken."""
    research.main(["signal", "--root", str(run), "stop"])
    research.main(["continue", "--root", str(run)])
    assert runconfig.load(run)["signals"]["stop_requested"] is False


def test_continue_resolves_pending_checkpoints(run, capsys):
    research.main(["signal", "--root", str(run), "checkpoint",
                   "--note", "ask me"])
    research.main(["continue", "--root", str(run)])
    cfg = runconfig.load(run)
    assert all(c["resolved"] for c in cfg["signals"]["checkpoints"])


def test_continue_keeps_a_conditional_stop_unless_asked(run, capsys):
    """The user set a condition. Clearing a halt is not the same as
    withdrawing it."""
    research.main(["signal", "--root", str(run), "stop-when",
                   "--json", '{"all": [{"min_facts": 500}]}'])
    research.main(["continue", "--root", str(run)])
    assert runconfig.load(run)["signals"]["stop_when"] is not None


def test_continue_can_withdraw_a_conditional_stop(run, capsys):
    research.main(["signal", "--root", str(run), "stop-when",
                   "--json", '{"all": [{"min_facts": 500}]}'])
    research.main(["continue", "--root", str(run), "--clear-stop-when"])
    assert runconfig.load(run)["signals"]["stop_when"] is None


def test_continue_lets_next_dispatch_again(run, mem, mktask, capsys):
    """The whole point. Spec section 4: 'the user returns days later, runs
    research status, then either research continue or research
    synthesize.'"""
    task = mktask(question="q")
    mem.update(task["id"], status="done")
    research.main(["next", "--root", str(run)])
    research.main(["continue", "--root", str(run)])
    mktask(question="new work", kind="search")
    capsys.readouterr()
    research.main(["next", "--root", str(run)])
    assert "TICK 1" in capsys.readouterr().out


def test_continue_on_a_run_that_never_halted_is_harmless(run, capsys):
    assert research.main(["continue", "--root", str(run)]) == 0


def _set_phase(run, phase):
    cfg = runconfig.load(run)
    cfg["status"]["phase"] = phase
    runconfig.save(run, cfg)


def test_continue_reopens_research_after_synthesis(run, capsys):
    """Deciding to write the report IS the decision to stop gathering, but
    it must be reversible: `submit` skips its follow-on scheduling while
    the phase is `synthesize`, and nothing else ever wrote the phase back.
    Both SKILL.md and references/loop-protocol.md told the operator this
    recovery existed."""
    _set_phase(run, "synthesize")
    assert research.main(["continue", "--root", str(run)]) == 0
    assert runconfig.load(run)["status"]["phase"] == "research"


def test_continue_says_it_reopened_research(run, capsys):
    """Named in the `cleared:` line, not done silently. Reopening research
    is the largest thing this command does -- it un-freezes the outline
    the operator deliberately froze -- and an operator who did not want it
    has to be able to see that it happened."""
    _set_phase(run, "synthesize")
    research.main(["continue", "--root", str(run)])
    out = capsys.readouterr().out
    assert "cleared:" in out
    assert "synthesis freeze" in out


def test_continue_does_not_reopen_a_finished_run(run, capsys):
    """`done` is written by `render`. A finished run silently reopened by
    a `continue` is a different surprise, and the report is already
    written."""
    _set_phase(run, "done")
    research.main(["continue", "--root", str(run)])
    assert runconfig.load(run)["status"]["phase"] == "done"
    assert "synthesis freeze" not in capsys.readouterr().out


def test_continue_during_research_says_nothing_about_the_phase(run, capsys):
    """The `cleared:` line lists what actually changed. A run already in
    the research phase had no freeze to lift."""
    _set_phase(run, "research")
    research.main(["continue", "--root", str(run)])
    out = capsys.readouterr().out
    assert "synthesis freeze" not in out
    assert "nothing was blocking" in out


# --- fsck -------------------------------------------------------------

def test_fsck_reports_a_clean_graph(run, mem, mktask, capsys):
    mktask(question="q")
    assert research.main(["fsck", "--root", str(run)]) == 0
    assert "clean" in capsys.readouterr().out.lower()


def test_fsck_reports_a_dangling_reference_and_exits_nonzero(run, mem,
                                                            mktask, capsys):
    mktask(question="q", depends_on=["T-777"])
    assert research.main(["fsck", "--root", str(run)]) == 1
    assert "T-777" in capsys.readouterr().out


def test_fsck_prints_warnings_without_failing(run, mem, mkcitation, capsys):
    """An orphan citation is a warning. Exiting nonzero on one would make
    the check useless as a gate."""
    mkcitation()
    assert research.main(["fsck", "--root", str(run)]) == 0
    assert "referenced by nothing" in capsys.readouterr().out


def test_fsck_does_not_repair_anything(run, mem, mktask):
    """fsck.py's own docstring: 'Reporting only. Repair is deliberately
    manual — a corrupted research graph should be looked at, not silently
    rewritten.'"""
    task = mktask(question="q", depends_on=["T-777"])
    research.main(["fsck", "--root", str(run)])
    assert mem.read(task["id"])["depends_on"] == ["T-777"]


def test_fsck_survives_an_unparseable_node(run, mem, mktask, capsys):
    task = mktask(question="q")
    mem.path_for(task["id"]).write_text("garbage\n", encoding="utf-8")
    assert research.main(["fsck", "--root", str(run)]) == 1
    assert "unparseable" in capsys.readouterr().out


# --- the dispatch table is complete ----------------------------------

def test_every_command_the_spec_names_exists():
    """Spec section 8's operations list, plus the two loop commands."""
    for name in ("init", "next", "submit", "status", "resume", "fsck",
                 "signal", "continue"):
        assert name in research.COMMANDS, name


def test_no_command_is_half_wired():
    for name, module in research.COMMANDS.items():
        assert isinstance(module.HELP, str) and module.HELP, name
        assert callable(module.add_arguments), name
        assert callable(module.run), name
