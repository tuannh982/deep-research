#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "jsonschema>=4.21",
#     "publicsuffix2>=2.20191221",
#     "pyyaml>=6.0",
# ]
# ///
"""deep-research skill entrypoint.

Subcommand dispatch only. Each command lives in its own module exposing
HELP, add_arguments(parser) and run(args) -> int, and is wired in through
COMMANDS below. Five separate tasks in plan 2 add a command; a one-line
dict entry each is the cheapest merge surface available.
"""
import argparse
import sys

import render
import report
import scheduler
import signals
import submit
import synthesis
import workspace

VERSION = "0.1.0"

COMMANDS = {
    "continue": report.CONTINUE,
    "fsck": report.FSCK,
    "init": workspace,
    "next": scheduler,
    "render": render,
    "resume": report.RESUME,
    "signal": signals,
    "status": report.STATUS,
    "submit": submit,
    "synthesize": synthesis,
}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="research",
        description="Deterministic long-running research over the public web.",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    subparsers = parser.add_subparsers(dest="command")
    # name -> that command's own subparser. Kept on the parser (rather
    # than discarded) so tests can inspect a command's grammar directly —
    # e.g. "does this subparser accept --root" — without constructing an
    # argv that a command's own required positionals/subcommands might
    # reject. argparse does not expose the subparser dict any other way.
    parser.subcommands = {}
    for name in sorted(COMMANDS):
        module = COMMANDS[name]
        sub = subparsers.add_parser(name, help=module.HELP)
        # Added centrally so every command has it, and so tests can point
        # at a tmp_path instead of the cwd.
        sub.add_argument(
            "--root", default="research",
            help="the run directory (default: ./research)",
        )
        module.add_arguments(sub)
        parser.subcommands[name] = sub
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1
    try:
        return COMMANDS[args.command].run(args)
    except (workspace.WorkspaceError, ValueError) as error:
        # A user-facing failure — a missing tool, an uninitialised
        # workspace, a bad run.yaml. Report it as a message, not a
        # traceback; the operator is not debugging this script.
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
