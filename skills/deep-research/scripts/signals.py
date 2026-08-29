"""research signal — turn a chat message into state on disk.

Spec section 4: "before every next, if the user sent a message this turn,
translate it with research signal first." Once it is in run.yaml the loop
evaluates it as code, which is what stops the model deciding for itself
that the user probably wanted to stop.
"""
import json

import predicates
import runconfig
import workspace

HELP = "record a stop request, a conditional stop, or a checkpoint"

CONFIRM_NOTE = (
    "confirm the compiled stop condition with the user before the loop "
    "resumes"
)


def add_checkpoint(cfg, note):
    """Register an unresolved checkpoint. Returns it.

    A checkpoint pauses the loop at the next tick and asks. It is the
    fallback for anything that cannot be expressed as a predicate, and
    the enforcement mechanism for "the loop does not resume until the
    user confirms".
    """
    checkpoint = {"note": note, "raised_at_tick": cfg["status"]["tick"],
                  "resolved": False}
    cfg["signals"]["checkpoints"].append(checkpoint)
    return checkpoint


def pending_checkpoints(cfg):
    return [c for c in cfg["signals"]["checkpoints"] if not c["resolved"]]


def add_arguments(parser):
    sub = parser.add_subparsers(dest="signal_kind", required=True)
    sub.add_parser("stop", help="halt at the next tick")
    conditional = sub.add_parser(
        "stop-when", help="halt once a predicate over the graph holds")
    conditional.add_argument(
        "--json", required=True, dest="predicate_json",
        help="a stop predicate; see schemas/stop_predicate.json")
    checkpoint = sub.add_parser(
        "checkpoint", help="pause at the next tick and ask the user")
    checkpoint.add_argument("--note", required=True)


def run(args):
    root = workspace.require(args.root)
    cfg = runconfig.load(root)

    if args.signal_kind == "stop":
        cfg["signals"]["stop_requested"] = True
        runconfig.save(root, cfg)
        print("stop requested: the loop will halt at the next `next`.")
        return 0

    if args.signal_kind == "checkpoint":
        checkpoint = add_checkpoint(cfg, args.note)
        runconfig.save(root, cfg)
        print(f"checkpoint registered at tick "
              f"{checkpoint['raised_at_tick']}: {checkpoint['note']}")
        print("The loop will pause at the next `next` and ask.")
        return 0

    try:
        predicate = json.loads(args.predicate_json)
    except json.JSONDecodeError as error:
        raise ValueError(f"--json is not valid JSON: {error}") from None

    try:
        predicates.validate(predicate)
    except predicates.PredicateError as error:
        # Spec section 4: refused, and a checkpoint registered instead —
        # so the user's intent survives even though it could not be
        # formalised, and the model does not get to decide it is
        # satisfied on their behalf.
        note = f"user asked to stop when: {args.predicate_json}"
        add_checkpoint(cfg, note)
        runconfig.save(root, cfg)
        print(f"refused: {error}")
        print()
        print("Registered a checkpoint instead. The loop will pause at the "
              "next `next` and ask you directly, rather than guessing.")
        return 0

    cfg["signals"]["stop_when"] = predicate
    add_checkpoint(cfg, CONFIRM_NOTE)
    runconfig.save(root, cfg)
    print(predicates.describe(predicate))
    print()
    print("Echo the above back to the user and get an explicit yes. The "
          "loop will not dispatch again until `research continue` clears "
          "the confirmation checkpoint.")
    return 0
