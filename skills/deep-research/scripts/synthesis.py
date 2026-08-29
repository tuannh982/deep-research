"""research synthesize: cross from gathering evidence to writing it up.

Spec section 8: after a halt the user "either `research continue` or
`research synthesize`". This command does not write any prose. It computes
the outline from the graph and seeds ONE `outline` task, because code
cannot dispatch a subagent — the outliner has to reach the loop as a task,
exactly as gate 4's adversarial verifier does.

The whole outline is frozen into that task's `inputs`. scheduler.agent_input
has no `root` and could not read it back from a file, and validating the
outliner's answer against a graph recomputed at submit time would reject it
for "dropping" whatever landed in between.
"""
import sys

import apply
import memory as memory_mod
import outline as outline_mod
import runconfig
import workspace
from graph import Graph

HELP = "compute the report outline and seed the section writers"


def add_arguments(parser):
    parser.add_argument(
        "--force", action="store_true",
        help="seed the outline even while research tasks remain "
             "dispatchable; the report will omit whatever they would "
             "have found",
    )


def seed(memory, graph, cfg, *, computed=None):
    """Create the outline task, or return the existing one.

    Returns (task_id, created, computed). Idempotent through the same
    natural-key dedup every other task creation uses: the computed outline
    is part of TASK_KEY via canonical(inputs), so re-running against an
    unchanged graph resolves to the task already there.

    `computed` is an optional pre-computed outline. `run` below has to
    compute one anyway, to check there is anything to report BEFORE
    seeding; passing it in stops the graph being walked twice per
    invocation. `outline.compute` is pure, so the two walks only ever
    differed in cost.
    """
    if computed is None:
        computed = outline_mod.compute(graph, cfg)
    index = apply.index_of(memory, "task", apply.TASK_KEY)
    task_id, created = apply.create_task(
        memory, index,
        question="arrange the report outline",
        kind="outline", parent=None, depth=0,
        origin_task=None, agent=None,
        inputs={"outline": computed},
    )
    return task_id, created, computed


def run(args):
    root = workspace.require(args.root)
    cfg = runconfig.load(root)
    memory = memory_mod.Memory(root)
    graph = Graph(memory, max_depth=cfg["config"]["max_depth"],
                  promotion_threshold=cfg["config"]["promotion_threshold"],
                  required_domains=cfg["config"]["required_domains"])

    outstanding = graph.eventually_dispatchable()
    if outstanding and not args.force:
        named = (", ".join(outstanding[:5])
                 + ("..." if len(outstanding) > 5 else ""))
        # Re-running `research synthesize` before running the loop finds
        # exactly one dispatchable task: the outline task the previous run
        # seeded. Calling that a "research task" whose findings the report
        # would omit was wrong twice over — it is this command's own
        # machinery, and it finds nothing. Told apart by kind, so a run
        # with real research left AND an open outliner still gets the
        # message that matters.
        if all(graph.tasks.get(task_id, {}).get("kind")
               in outline_mod.MACHINERY_KINDS for task_id in outstanding):
            print(
                f"error: synthesis is already under way — {len(outstanding)} "
                "outline/section task(s) are still dispatchable. Run "
                "`research next` and `research submit` until the loop halts, "
                "then `research render`: " + named,
                file=sys.stderr,
            )
            return 1
        print(
            f"error: {len(outstanding)} research task(s) are still "
            "dispatchable, so an outline computed now would omit whatever "
            "they find. Run the loop until it halts, or pass --force to "
            "synthesize anyway: " + named,
            file=sys.stderr,
        )
        return 1

    # Computed and checked BEFORE seeding. Seeding first would leave an
    # outline task behind on a run with nothing to report, and the loop
    # would then dispatch the outliner against an empty outline.
    computed = outline_mod.compute(graph, cfg)
    if not computed["sections"]:
        print(
            "error: no findings to report — every theme is empty. There is "
            "nothing to synthesize yet; run the loop until hypotheses have "
            "been raised.",
            file=sys.stderr,
        )
        return 1

    task_id, created, _ = seed(memory, graph, cfg, computed=computed)

    cfg["status"]["phase"] = "synthesize"
    # The run halted to get here, and synthesis IS the way forward from
    # that halt. Leaving it set would make the next `next` print HALT and
    # refuse to dispatch the task this command just seeded.
    cfg["status"]["halted"] = None
    runconfig.save(root, cfg)

    print(f"{'seeded' if created else 'reusing'} {task_id} (outline, depth 0)")
    print(f"  question   {cfg['question']}")
    for section in computed["sections"]:
        print(f"  {section['id']}      {section['title']} "
              f"({len(section['hypotheses'])} hypotheses, "
              f"{len(section['facts'])} facts)")
    orphans = computed["orphans"]
    stranded = len(orphans["hypotheses"]) + len(orphans["facts"])
    if stranded:
        print(f"  orphaned   {stranded} finding(s) could not be placed; "
              "they are reported in Appendix C")
    print()
    print("Next: run `research next` — it dispatches the outliner, and "
          "submitting its artifact seeds one writer per section.")
    return 0
