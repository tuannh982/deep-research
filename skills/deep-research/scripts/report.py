"""research status / resume / continue / fsck.

Four small commands that read the run and print. `status` shares
halt.render_status with the halt path, so what a user reads days later is
what the loop actually decided rather than a second description of it.

`resume` is the recovery of last resort — spec section 4: "SKILL.md's loop
is three lines, so it survives compaction too. research resume reprints
the protocol if even that is lost." It therefore depends on nothing but
run.yaml and the journal, and never writes.

Exposed as four SimpleNamespace objects rather than four modules: each is
a help string and two functions, and four files for that would be four
files to keep in step.
"""
from pathlib import Path
from types import SimpleNamespace

import fsck as fsck_mod
import halt as halt_mod
import journal as journal_mod
import memory as memory_mod
import runconfig
import signals
import workspace
from graph import Graph


def _open_run(root):
    root = workspace.require(root)
    cfg = runconfig.load(root)
    memory = memory_mod.Memory(root)
    graph = Graph(memory, max_depth=cfg["config"]["max_depth"],
                  promotion_threshold=cfg["config"]["promotion_threshold"],
                  required_domains=cfg["config"]["required_domains"])
    return root, cfg, memory, graph


def loop_protocol(cfg, root, skill_dir):
    """The three-line loop, with absolute paths.

    Spec section 4: "SKILL.md's loop is three lines, so it survives
    compaction too. research resume reprints the protocol if even that is
    lost." Absolute paths because in that state the agent may not know
    where it is — so this resolves `root` itself rather than trust the
    caller. `workspace.require` deliberately does not resolve (its own
    test asserts `require(root) == root`, and resolving there would also
    rewrite a macOS `/tmp` path to `/private/tmp` for unrelated reasons),
    so a cwd-relative `--root research` — argparse's own default — would
    otherwise reach here unresolved and print a command that is unusable
    from anywhere but the exact cwd the agent may no longer remember.

    Round-1 review fix: this used to print `research = uv run
    .../research.py --root <root>`, with the implication that a command
    name gets appended after it. That does not parse: `--root` is
    registered on each subcommand's own subparser (see
    research.build_parser, which adds it to `sub` *after* the subparsers
    object is created), not on the top-level parser, so
    `research.py --root <root> next` fails with "invalid choice:
    '<root>'" before `next` is ever seen. The template below puts
    `<command>` before `--root`, which is the order argparse actually
    accepts. tests/test_cli.py::
    test_resume_prints_a_command_form_that_actually_parses builds a real
    invocation from this exact string and feeds it to
    build_parser().parse_args() so this cannot regress silently again.
    """
    root = Path(root).resolve()
    command = f"uv run {skill_dir / 'scripts' / 'research.py'}"
    return "\n".join([
        "LOOP PROTOCOL",
        "",
        "  research <command> [args]  =  "
        f"{command} <command> --root {root} [args]",
        "",
        "  1. If the user sent a message this turn, run `research signal "
        "...` first.",
        "  2. Run `research next`. Dispatch every subagent it lists IN "
        "PARALLEL, in one message, exactly as printed.",
        "  3. Run `research submit --tick N`. Go back to 1.",
        "",
        f"  phase {cfg['status']['phase']}, tick {cfg['status']['tick']}",
    ])


# --- status -----------------------------------------------------------

def _status_run(args):
    root, cfg, memory, graph = _open_run(args.root)
    events = journal_mod.read(root)
    stored = cfg["status"]["halted"]
    halted = (halt_mod.Halt(stored["reason"], stored["detail"])
              if stored else None)
    print(halt_mod.render_status(memory, graph, cfg, events, halted))
    return 0


STATUS = SimpleNamespace(
    HELP="where the run is, one screen",
    add_arguments=lambda parser: None,
    run=_status_run,
)


# --- resume -------------------------------------------------------------

def _resume_run(args):
    """Print the loop protocol and where the current tick stands.

    Reads run.yaml and the journal only, and writes nothing. This is the
    recovery of last resort — a compaction that wipes the conversation
    must not also require state that only lived in that conversation.
    """
    root = workspace.require(args.root)
    cfg = runconfig.load(root)
    events = journal_mod.read(root)
    print(loop_protocol(cfg, root, workspace.skill_dir()))
    print()
    tick = cfg["status"]["tick"]
    in_flight = journal_mod.dispatched_for_tick(events, tick)
    if in_flight and not journal_mod.tick_submitted(events, tick):
        # journal.read() only guarantees a surviving record is valid JSON
        # and a dict, not any particular shape — a hand-edited or
        # older-format journal.jsonl can be missing 'task_ids'. Degrade
        # rather than crash: this is the recovery of last resort, and it
        # can still say a tick is in flight and what to run next even
        # when it cannot name the tasks.
        task_ids = in_flight.get("task_ids")
        if task_ids:
            print(f"Tick {tick} is in flight ({', '.join(task_ids)}).")
        else:
            print(f"Tick {tick} is in flight (task ids unavailable — the "
                  "journal record is missing 'task_ids').")
        print(f"Run `research next` to reprint its packet, or "
              f"`research submit --tick {tick}` if the artifacts are "
              "already written.")
    else:
        print("No tick is in flight. Run `research next`.")
    return 0


RESUME = SimpleNamespace(
    HELP="reprint the loop protocol and where the run is",
    add_arguments=lambda parser: None,
    run=_resume_run,
)


# --- continue ---------------------------------------------------------

def _continue_arguments(parser):
    parser.add_argument(
        "--clear-stop-when", action="store_true",
        help="also withdraw the conditional stop, not just the halt")


def _continue_run(args):
    root, cfg, memory, graph = _open_run(args.root)
    cleared = []
    if cfg["status"]["halted"]:
        cleared.append(f"halt({cfg['status']['halted']['reason']})")
    # Cleared together with the halt: leaving the flag set would halt
    # again on the very next tick and make `continue` look broken.
    if cfg["signals"]["stop_requested"]:
        cleared.append("stop request")
        cfg["signals"]["stop_requested"] = False
    # `next` refuses to dispatch while a checkpoint is pending, so
    # `continue` must resolve them or the loop stays stuck.
    pending = signals.pending_checkpoints(cfg)
    for checkpoint in pending:
        checkpoint["resolved"] = True
    if pending:
        cleared.append(f"{len(pending)} checkpoint(s)")
    # A conditional stop survives by default: clearing a halt is not the
    # same as withdrawing the condition the user set. --clear-stop-when is
    # the explicit opt-out.
    if args.clear_stop_when and cfg["signals"]["stop_when"]:
        cleared.append("conditional stop")
        cfg["signals"]["stop_when"] = None
    # The synthesis freeze, lifted. `submit` skips step 4 — follow-on
    # hypothesize and evidence-seeking tasks — for as long as the phase is
    # `synthesize`, and `scheduler.run` deliberately refuses to overwrite
    # that phase, so nothing anywhere wrote it back. Clearing the halt
    # therefore reopened a run that could no longer schedule any research:
    # SKILL.md's "to pick up where you left off, `research continue` and
    # then `research synthesize` again" and loop-protocol.md's "a fresh
    # `research synthesize` recomputes the outline over everything found
    # since" both described a recovery that could not happen, because
    # "everything found since" was by construction nothing.
    #
    # NOT reset from "done". That is written by `render`: the report
    # exists, and a finished run silently reopened by a `continue` is a
    # different surprise. Named in `cleared:` rather than done quietly,
    # because un-freezing an outline the operator deliberately froze is
    # the largest thing this command does.
    if cfg["status"]["phase"] == "synthesize":
        cleared.append("synthesis freeze (research reopened)")
        cfg["status"]["phase"] = "research"
    # The saturation window's cut. Saturation is not a stored flag —
    # it is recomputed every `next` from the journal's last N
    # completions — so clearing the halt above did nothing for it: `next`
    # re-read the same dry window and halted again, and no new completion
    # could arrive because the halt is what stops anything dispatching.
    # A run with real work outstanding was stranded permanently, with no
    # exit but `research signal stop`. journal.completions reads this
    # record and counts only what followed it.
    journal_mod.append(root, "resumed", tick=cfg["status"]["tick"])
    halt_mod.record(root, cfg, None)
    print("cleared: " + (", ".join(cleared) or "nothing was blocking"))
    if cfg["signals"]["stop_when"] and not args.clear_stop_when:
        print("The conditional stop is still in force; pass "
              "--clear-stop-when to withdraw it.")
    print("Run `research next`.")
    return 0


CONTINUE = SimpleNamespace(
    HELP="clear a halt or a checkpoint and keep going",
    add_arguments=_continue_arguments,
    run=_continue_run,
)


# --- fsck -------------------------------------------------------------

def _fsck_run(args):
    """Reporting only. `fsck.py`'s own docstring: repair is deliberately
    manual — a corrupted research graph should be looked at, not silently
    rewritten. So this never writes, and exits non-zero only on an error,
    never on a warning alone.
    """
    _root, _cfg, memory, graph = _open_run(args.root)
    findings = fsck_mod.check(memory, graph)
    if not findings:
        print("fsck: clean — every node validates and every reference "
              "resolves.")
        return 0
    for finding in findings:
        print(f"  {finding.severity:7s} {finding.node:10s} {finding.message}")
    errors = fsck_mod.errors(findings)
    print(f"fsck: {len(errors)} error(s), "
          f"{len(findings) - len(errors)} warning(s)")
    if errors:
        print("Repair is manual by design: a corrupted research graph "
              "should be looked at, not silently rewritten.")
    return 1 if errors else 0


FSCK = SimpleNamespace(
    HELP="revalidate every node and all cross-references",
    add_arguments=lambda parser: None,
    run=_fsck_run,
)
