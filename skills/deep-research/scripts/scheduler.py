"""The scheduler and the step packet. `research next`.

Two properties, both from spec section 4.

*Determinism.* The dispatch set is a prefix of Graph.frontier(), which is
sorted and computed from the DAG. Given the same graph, tick N always
dispatches the same tasks. "The model is never the scheduler."

*Compaction survival, by construction.* The packet carries the agent, its
prompt file, its input, its output schema and its destination. Nothing in
tick 7 depends on remembering tick 6.

The second property has a consequence the spec leaves implicit: `next`
must be safe to run twice, because a compaction can land between `next`
and the dispatch and the recovery is to run `next` again. But `next` marks
its tasks `running`, taking them off the frontier, so a naive re-run would
print an empty packet and stall the run exactly when recovery was needed.
So `next` is idempotent by tick: with a `dispatched` record for the
current tick and no `tick_submitted`, it reprints that packet from the
recorded task ids. A fresh frontier is computed only after a submit.
"""
import json
from dataclasses import dataclass, field

import halt as halt_mod
import ids as ids_mod
import journal as journal_mod
import memory as memory_mod
import nodes
import runconfig
import signals
import workspace
from graph import CycleError, Graph

HELP = "print the next step packet"

# Spec section 4: "Every 25 ticks the loop prints one line and keeps
# going."
DIGEST_EVERY = 25

# Spec section 5: subagents "cannot bloat their context". A branch on a
# multi-day run accumulates hundreds of facts, and the hypothesizer's
# packet is the one place that promise can quietly break. The most recent
# facts are kept, by number — see ids.numeric.
MAX_FACTS_IN_PACKET = 40

# What agent_input must produce for each kind. Checked, so a scheduler bug
# is a loud failure here rather than three wasted attempts at a subagent
# that was handed nothing.
REQUIRED_INPUT_KEYS = {
    "decompose": ("task_id", "question", "scope", "parent_question",
                  "siblings", "depth", "max_depth", "children_allowed"),
    "search": ("task_id", "question", "seen_domains", "stance"),
    "extract": ("task_id", "question", "url", "title"),
    "recheck": ("task_id", "url", "quotes"),
    "hypothesize": ("task_id", "question", "facts", "facts_omitted",
                    "open_assumptions"),
    "verify": ("task_id", "hypothesis", "claim", "quotes"),
    "outline": ("task_id", "question", "sections"),
    "synthesize": ("task_id", "question", "section", "build_error"),
}


@dataclass
class Dispatch:
    task_id: str
    kind: str
    agent: str
    agent_file: str
    schema_file: str
    out_path: str
    model: str
    timeout_seconds: float
    attempt: int
    input: dict
    retry_error: str = None


@dataclass
class Packet:
    tick: int
    dispatches: list = field(default_factory=list)
    frontier_size: int = 0
    digest: str = None
    reprint: bool = False
    stats: dict = field(default_factory=dict)
    # (task_id, reason) for each task this tick could not build an input
    # packet for — a dangling, unparseable, or schema-invalid node it
    # referenced (see agent_input's verify branch). Not dispatched, not
    # marked running, and named here so the operator sees it rather than
    # having it silently vanish from the tick.
    skipped: list = field(default_factory=list)


def _branch_of(graph, task_id):
    if task_id is None or task_id not in graph.tasks:
        return None
    try:
        return graph.root_branch(task_id)
    except CycleError:
        return None


def _last_dispatched_tick(events):
    """The highest tick with a `dispatched` journal record, or None.

    `cfg["status"]["tick"]` is not a safe stand-in for this: it can be 0
    on a run where nothing has ever been dispatched (a task can be marked
    `running` by hand, as in a test, or in principle by a future writer),
    and `research submit --tick N` refuses a tick with no dispatch
    record. Naming that tick in the "nothing to dispatch" message would
    hand the operator a command guaranteed to fail.
    """
    ticks = [e["tick"] for e in events if e.get("event") == "dispatched"]
    return max(ticks) if ticks else None


def _describe(memory, node_ids, field):
    """[{"id": ..., <field>: ...}] for every node that still reads cleanly.

    A frozen outline names ids that were live when the task was seeded. A
    cascade running in the meantime can quarantine or delete any of them,
    and indexing a missing node raises KeyError straight out of `research
    next` — the one command that must always work, because it is the only
    way forward from an in-flight tick. Skipped and left out, the same
    treatment the verify branch already gives a dangling citation.
    """
    described = []
    for node_id in node_ids:
        try:
            node = memory.read(node_id)
            memory.validate(node)
        except (KeyError, nodes.NodeFormatError, memory_mod.ValidationError):
            continue
        described.append({"id": node_id, field: node[field]})
    return described


def agent_input(memory, graph, cfg, task_id, task):
    """The JSON packet one subagent receives. Exactly what it needs.

    Spec section 5: "No subagent reads memory/. Each receives a JSON
    input packet with exactly what it needs and returns a JSON artifact.
    They cannot bloat their context, cannot drift, and can be tested in
    isolation from a fixture file."
    """
    kind = task["kind"]
    inputs = task.get("inputs") or {}

    if kind == "decompose":
        parent = graph.tasks.get(task["parent"]) if task["parent"] else None
        siblings = sorted(
            other["question"] for other_id, other in graph.tasks.items()
            if other_id != task_id and other["parent"] == task["parent"]
        )
        payload = {
            "task_id": task_id, "question": task["question"],
            "scope": cfg["scope"],
            "parent_question": parent["question"] if parent else None,
            "siblings": siblings, "depth": task["depth"],
            "max_depth": cfg["config"]["max_depth"],
            # Told explicitly rather than left to arithmetic: a decomposer
            # that proposes children submit will prune has spent an
            # attempt on work that cannot land.
            "children_allowed": task["depth"] + 1 <= cfg["config"]["max_depth"],
        }

    elif kind == "search":
        seen = sorted({
            citation["domain"]
            for _, citation in graph.readable("citation")
        })
        # Defaulted, never required of the task: every search task
        # written before this field existed has no stance in its inputs,
        # and REQUIRED_INPUT_KEYS checks presence — so without the
        # default, upgrading mid-run would fail to build a packet for
        # every outstanding search at once. Same absent-means-default
        # convention as schemas/assumption.json's `cascaded`.
        payload = {"task_id": task_id, "question": task["question"],
                   "seen_domains": seen,
                   "stance": inputs.get("stance") or "for"}

    elif kind == "extract":
        if not inputs.get("url"):
            raise ValueError(
                f"{task_id} is an extract task with no url in its inputs; "
                "there is no page to read"
            )
        payload = {"task_id": task_id, "question": task["question"],
                   "url": inputs["url"], "title": inputs.get("title", "")}

    elif kind == "recheck":
        if not inputs.get("url"):
            raise ValueError(
                f"{task_id} is a recheck task with no url in its inputs; "
                "there is no page to re-read"
            )
        if not inputs.get("quotes"):
            raise ValueError(
                f"{task_id} is a recheck task with no quotes in its inputs; "
                "there is nothing to confirm"
            )
        # url and quotes, and nothing else. The citation ids this answer
        # will update are deliberately withheld: a checker that knows
        # which record its verdict writes to is a checker with a stake in
        # the verdict. It is asked only whether some text is on a page.
        payload = {"task_id": task_id, "url": inputs["url"],
                   "quotes": list(inputs["quotes"])}

    elif kind == "hypothesize":
        branch = _branch_of(graph, task_id)
        gathered = []
        for fact_id, fact in graph.readable("fact"):
            if fact["status"] != "active":
                continue
            if _branch_of(graph, fact["provenance"]["task"]) != branch:
                continue
            quotes = []
            for citation_id in fact["citations"]:
                try:
                    citation = memory.read(citation_id)
                    memory.validate(citation)
                except Exception:
                    continue
                quotes.append({"id": citation_id,
                               "domain": citation["domain"],
                               "quote": citation["quote"]})
            gathered.append({"id": fact_id, "statement": fact["statement"],
                             "citations": quotes})
        gathered.sort(key=lambda item: ids_mod.numeric(item["id"]))
        omitted = max(0, len(gathered) - MAX_FACTS_IN_PACKET)
        # Spend the cap on evidence nothing has used yet.
        #
        # This cap is the ONLY limit on evidence reuse, and it binds
        # harder than it looks: `_branch_of` resolves through
        # `root_branch`, which its own docstring calls "a constant
        # function on a real run" — init seeds one parentless task and
        # everything descends from it — so `gathered` is already the
        # whole run's facts, across every theme. On a long run this is
        # 40 out of hundreds.
        #
        # Recency was the wrong thing to spend that on. A fact whose
        # citation already supports or opposes some claim is the one the
        # hypothesizer least needs to see again; a fact nothing has
        # touched is exactly what an under-evidenced claim might be built
        # from, and under the old slice it fell out of the packet for
        # ever the moment 40 newer facts existed. Recency is kept as the
        # tiebreak within each group.
        attached = set()
        for _, hypothesis in graph.readable("hypothesis"):
            attached.update(hypothesis["supporting"])
            attached.update(hypothesis["counter"])

        def is_unused(item):
            return not any(quote["id"] in attached
                           for quote in item["citations"])

        # `gathered` is already in id order, so the tail of each group is
        # its most recent members.
        unused = [item for item in gathered if is_unused(item)]
        used = [item for item in gathered if not is_unused(item)]
        selected = unused[-MAX_FACTS_IN_PACKET:]
        room = MAX_FACTS_IN_PACKET - len(selected)
        if room:
            selected += used[-room:]
        # Back to id order: the packet is read by a human as well, and
        # hypothesizer.md asks for ids copied verbatim.
        selected.sort(key=lambda item: ids_mod.numeric(item["id"]))
        assumptions = [
            {"id": assumption_id, "statement": assumption["statement"]}
            for assumption_id, assumption in graph.readable("assumption")
            if assumption["status"] == "open"
            and _branch_of(graph, assumption["raised_by"]) == branch
        ]
        payload = {
            "task_id": task_id, "question": task["question"],
            "facts": selected,
            "facts_omitted": omitted,
            "open_assumptions": assumptions,
        }

    elif kind == "verify":
        hypothesis_id = inputs.get("hypothesis")
        if not hypothesis_id:
            raise ValueError(
                f"{task_id} is a verify task with no hypothesis in its "
                "inputs; there is nothing to verify"
            )
        # Guarded like the citation reads below, and for the same reason:
        # a missing input on THIS task is a scheduler bug (raise above),
        # but a dangling, unparseable, or schema-invalid hypothesis is
        # data corruption elsewhere in the graph. build_packet catches
        # this ValueError and skips just this one dispatch rather than
        # losing the whole tick to it.
        try:
            hypothesis = memory.read(hypothesis_id)
            memory.validate(hypothesis)
        except (KeyError, nodes.NodeFormatError,
                memory_mod.ValidationError) as error:
            raise ValueError(
                f"{task_id} is a verify task naming hypothesis "
                f"{hypothesis_id!r}, which is dangling, unparseable, or "
                f"schema-invalid: {error}"
            ) from error
        # Every quote carries the side it was offered for. Both lists have
        # always fed this packet, but unlabelled: verifier.md then told the
        # model it was reading "the quotes offered in support", so a
        # counter quote was read as weak support for the claim it argues
        # against, and the one agent in the run positioned to weigh a
        # dispute could not tell there was one.
        #
        # Counter wins an overlap, and the quote is emitted once. The two
        # lists are incoherent when they intersect — apply_hypothesize
        # rejects a NEW artifact that does it — but a hypothesis already
        # on disk can carry the overlap, which is why apply_verify
        # re-checks at the point of use and resolves it the same way. A
        # packet that disagreed would send the same span twice under two
        # contradictory labels.
        counter = set(hypothesis["counter"])
        quotes = []
        seen = set()
        for citation_id in (hypothesis["supporting"] + hypothesis["counter"]):
            if citation_id in seen:
                continue
            seen.add(citation_id)
            try:
                citation = memory.read(citation_id)
                memory.validate(citation)
            except Exception:
                continue
            quotes.append({"id": citation_id, "domain": citation["domain"],
                           "quote": citation["quote"],
                           "stance": ("counter" if citation_id in counter
                                      else "supporting")})
        # The claim and its quotes, nothing else. No graph, no history, no
        # sibling hypotheses — that is what makes gate 4 adversarial
        # rather than confirmatory.
        payload = {"task_id": task_id, "hypothesis": hypothesis_id,
                   "claim": hypothesis["claim"], "quotes": quotes}

    elif kind == "outline":
        frozen = inputs.get("outline")
        if not frozen or not frozen.get("sections"):
            raise ValueError(
                f"{task_id} is an outline task with no outline in its "
                "inputs; there is nothing to arrange"
            )
        sections = []
        for section in frozen["sections"]:
            # Claims and statements, not bare ids: an outliner asked to
            # retitle "S-002: H-004, H-011" is guessing. `theme` is
            # deliberately not forwarded — it is graph topology, and
            # outline.apply_artifact refuses to read it back from the
            # artifact, so sending it could only invite an edit that gets
            # silently discarded.
            sections.append({
                "id": section["id"],
                "title": section["title"],
                "hypotheses": _describe(memory, section["hypotheses"], "claim"),
                "facts": _describe(memory, section["facts"], "statement"),
            })
        payload = {"task_id": task_id, "question": cfg["question"],
                   "sections": sections}

    elif kind == "synthesize":
        section = inputs.get("section")
        if not section:
            raise ValueError(
                f"{task_id} is a synthesize task with no section in its "
                "inputs; there is nothing to write"
            )
        payload = {
            "task_id": task_id,
            "question": cfg["question"],
            # Forwarded whole. It was assembled by apply_outline against a
            # validated outline and is deliberately not recomputed here:
            # a cascade landing mid-synthesis would otherwise change what
            # the writer is asked for between one attempt and the next.
            "section": section,
            # Present and None on a first attempt so the key always exists;
            # REQUIRED_INPUT_KEYS checks presence, not truthiness.
            "build_error": inputs.get("build_error"),
        }

    else:
        raise ValueError(f"no input packet defined for kind {kind!r}")

    missing = [k for k in REQUIRED_INPUT_KEYS[kind] if k not in payload]
    if missing:
        raise ValueError(
            f"input packet for {task_id} ({kind}) is missing "
            + ", ".join(missing)
        )
    return payload


def build_packet(memory, graph, cfg, events, tick, task_ids, cap=None):
    """A Packet built by walking `task_ids` in sorted order. Pure: no
    writes.

    `cap`, if given, limits the number of *successful* dispatches, not
    the number of ids considered. A skipped task (agent_input raising —
    see its verify branch) does not consume a dispatch slot, so this walk
    can and deliberately does read past the first `cap` ids in
    `task_ids` to reach a healthy one further along: with cap=1, a
    corrupt task sorting first still lets the walk reach the next
    healthy id and dispatch it.

    Do not "simplify" this back to slicing task_ids to `cap` items before
    calling this function. That was the exact defect this parameter
    fixes: run() used to compute `graph.frontier()[:cap]` and hand only
    that slice to build_packet, so a corrupt task sorting within the
    capped slice hid every healthy task after it. Under --serial or a
    small max_parallel, the run silently livelocked — every tick reported
    "dispatching 0" and neither advanced nor made progress, with real
    work outstanding the whole time.

    Determinism is unaffected by applying the cap here rather than
    outside: the walk order is always `sorted(task_ids)`, still a pure
    function of the caller's inputs, so tick N dispatches the same task
    set every time it is computed from the same graph and the same cap.
    """
    packet = Packet(tick=tick, frontier_size=len(graph.frontier()))
    valid = graph.valid_task_ids()
    for task_id in sorted(task_ids):
        if cap is not None and len(packet.dispatches) >= cap:
            break
        task = graph.tasks.get(task_id)
        if task is None or task_id not in valid:
            # `task_ids` is NOT always graph.frontier(), which is already
            # gated on valid_task_ids(). On the reprint path it is a list
            # of ids read back out of a `dispatched` journal record, and
            # between that dispatch and this reprint the task file can
            # have been deleted, truncated to something unparseable, or
            # lost a required field. Indexing raised KeyError straight out
            # of `research next` — and `next` is the ONLY way forward from
            # an in-flight tick, so nothing could reprint the packet,
            # nothing could tell the operator what to submit, and every
            # retry died on the same line. Skipped and named instead, the
            # same treatment agent_input's dangling-node branch already
            # gets. submit's own per-task guard rejects the artifact and
            # fsck reports the file.
            packet.skipped.append((
                task_id,
                "the task record is missing, unparseable, or schema-invalid; "
                "fsck reports it",
            ))
            continue
        agent = runconfig.KIND_AGENT.get(task["kind"])
        if agent is None:
            # `kind` is schema-constrained, so this is a scheduler/schema
            # divergence rather than data corruption — but it is still a
            # KeyError out of the recovery path if left unguarded.
            packet.skipped.append((
                task_id, f"no agent is defined for kind {task['kind']!r}"))
            continue
        try:
            payload = agent_input(memory, graph, cfg, task_id, task)
        except ValueError as error:
            # One corrupt or dangling node referenced by one task must
            # not cost the whole tick: every other healthy task in this
            # frontier still gets built and dispatched below, and this
            # skip does not count against `cap`.
            packet.skipped.append((task_id, str(error)))
            continue
        packet.dispatches.append(Dispatch(
            task_id=task_id,
            kind=task["kind"],
            agent=agent,
            agent_file=f"agents/{agent}.md",
            schema_file=f"schemas/artifact.{task['kind']}.json",
            out_path=f"inbox/{task_id}.json",
            model=cfg["models"][agent],
            timeout_seconds=cfg["config"]["agent_timeout"],
            attempt=task["attempts"] + 1,
            input=payload,
            retry_error=journal_mod.last_rejection(events, task_id),
        ))
    if tick % DIGEST_EVERY == 0:
        packet.digest = halt_mod.digest(memory, graph, cfg, events)
    return packet


def render(packet, cfg, skill_dir, root):
    """The text spec section 4 shows. The whole contract, on one screen."""
    lines = []
    if packet.digest:
        lines += [packet.digest, ""]
    header = (f"TICK {packet.tick} | frontier {packet.frontier_size} | "
              f"dispatching {len(packet.dispatches)}")
    if packet.reprint:
        header += " | already dispatched, reprinting"
    lines += [header, ""]
    if packet.reprint:
        lines += ["This tick was already dispatched. If you have not run "
                  "those subagents yet, run them now; if you have, run the "
                  "submit line below.", ""]
    lines += ["Dispatch these subagents IN PARALLEL, in one message:", ""]
    for index, dispatch in enumerate(packet.dispatches, start=1):
        lines += [
            f" [{index}] agent  {skill_dir / dispatch.agent_file}",
            f"     task   {dispatch.task_id}  ({dispatch.kind}, attempt "
            f"{dispatch.attempt}/{cfg['config']['max_attempts']})",
            f"     model  {dispatch.model}   timeout "
            f"{dispatch.timeout_seconds:g}s",
            f"     input  {json.dumps(dispatch.input, sort_keys=True, ensure_ascii=False)}",
            f"     schema {skill_dir / dispatch.schema_file}",
            f"     write  {root}/{dispatch.out_path}",
        ]
        if dispatch.retry_error:
            lines += [f"     RETRY  the previous attempt was rejected: "
                      f"{dispatch.retry_error}"]
        lines += [""]
    if packet.skipped:
        lines += ["SKIPPED — not dispatched, needs investigation before "
                  "the next submit:", ""]
        for task_id, reason in packet.skipped:
            lines += [f" - {task_id}: {reason}", ""]
    if packet.dispatches:
        lines += [f"Then run: research submit --tick {packet.tick}", ""]
    elif packet.skipped or packet.reprint:
        # A tick where every candidate was skipped still has to be
        # submitted. `submit` is what charges an attempt against each
        # skipped task (step 6), and that is the only thing that ever
        # ages an unbuildable frontier out: without this line the
        # operator is told there is nothing to do, runs `next` again, and
        # gets the identical packet forever with real work outstanding.
        lines += [
            f"Nothing could be dispatched. Run: research submit --tick "
            f"{packet.tick} — it charges an attempt against each skipped "
            "task, so a frontier nothing can build ages out and is "
            "abandoned rather than holding the run open forever.", ""]
    else:
        lines += ["Nothing was dispatched this tick; there is nothing to "
                  "submit.", ""]
    return "\n".join(lines)


def add_arguments(parser):
    parser.add_argument(
        "--serial", action="store_true",
        help="cap the frontier at one task, for debugging")
    parser.add_argument(
        "--allow-empty-scope", action="store_true",
        help="dispatch the first tick without a scope; breadth is then "
             "bounded only by the depth cap")


def run(args):
    root = workspace.require(args.root)
    cfg = runconfig.load(root)
    memory = memory_mod.Memory(root)
    graph = Graph(memory, max_depth=cfg["config"]["max_depth"],
                  promotion_threshold=cfg["config"]["promotion_threshold"],
                  required_domains=cfg["config"]["required_domains"])
    events = journal_mod.read(root)

    # A checkpoint outranks a halt: the user asked a question, and
    # announcing a conclusion before answering it is the failure mode the
    # checkpoint exists to prevent.
    pending = signals.pending_checkpoints(cfg)
    if pending:
        print(f"CHECKPOINT | {len(pending)} waiting")
        for checkpoint in pending:
            print(f"  (tick {checkpoint['raised_at_tick']}) "
                  f"{checkpoint['note']}")
        print()
        print("Ask the user, then run `research continue` to clear these "
              "and resume.")
        return 0

    stored = cfg["status"]["halted"]
    if stored:
        print(f"HALT({stored['reason']}) at tick {stored['at_tick']} — "
              f"{stored['detail']}")
        print(f"See {root}/out/status.md. Run `research continue` to keep "
              "going, or move on to synthesis.")
        return 0

    halted = halt_mod.check(memory, graph, cfg, events)
    if halted:
        halt_mod.write_status(
            root, halt_mod.render_status(memory, graph, cfg, events, halted))
        halt_mod.record(root, cfg, halted)
        print(f"HALT({halted.reason}) — {halted.detail}")
        print(f"Wrote {root}/out/status.md.")
        return 0

    in_flight = journal_mod.dispatched_for_tick(events, cfg["status"]["tick"])
    if in_flight and not journal_mod.tick_submitted(events,
                                                    cfg["status"]["tick"]):
        # `.get`, not `[...]`: journal.read() only guarantees a surviving
        # record is valid JSON and a dict, not any particular shape — a
        # hand-edited or older-format journal.jsonl can be missing
        # 'task_ids'. report._resume_run already degrades rather than
        # crashing on exactly this record for exactly this reason, and
        # this path needs it more: `next` is the only way forward from an
        # in-flight tick, so a KeyError here is terminal. With no ids to
        # reprint the packet is empty and render's reprint branch points
        # at `submit`, which lands `tick_submitted` and frees the run.
        packet = build_packet(memory, graph, cfg, events,
                              cfg["status"]["tick"],
                              in_flight.get("task_ids") or [])
        packet.reprint = True
        print(render(packet, cfg, workspace.skill_dir(), root))
        return 0

    # The last moment the decision is still free. `research init` writes
    # three empty scope lists and the scoping skill fills them; nothing
    # made that mandatory, so a run could start unscoped and stay that
    # way. That matters more here than it would elsewhere because halt
    # has NO budget condition — an unscoped run is bounded only by
    # max_depth, and the decomposer argues every child it proposes
    # against `in_scope` (see this module's decompose branch), so an
    # empty list means unbounded breadth by construction.
    #
    # Tick 0 only, and after the checkpoint/halt/in-flight branches
    # above: fires exactly once, at the point where nothing has been
    # dispatched and the graph is still one seeded root. By tick 2 the
    # tree exists, the choice cannot be unmade, and refusing would strand
    # a running graph over it.
    #
    # `in_scope` alone. An honest run can have nothing to exclude and no
    # criteria worth writing down; it cannot have nothing in scope.
    #
    # WorkspaceError, not a new exception type: research.main already
    # catches it, prints `error: ...` and returns 1 — no traceback, right
    # exit code. Escape hatch mirrors init's --allow-missing-tectonic,
    # which is this codebase's settled answer to "refuse, but let someone
    # who means it through". It has to be typed.
    if (cfg["status"]["tick"] == 0
            and not cfg["scope"]["in_scope"]
            and not args.allow_empty_scope):
        raise workspace.WorkspaceError(
            "this run has an empty `scope.in_scope` and has not dispatched "
            "anything yet. The loop has no budget condition, so an unscoped "
            "run is bounded only by the depth cap: it will not stop because "
            "it has done enough. Run the deep-research:research-brainstorming "
            "skill to agree a scope and write it into run.yaml, or pass "
            "--allow-empty-scope to dispatch anyway."
        )

    tick = cfg["status"]["tick"] + 1
    cap = 1 if args.serial else cfg["config"]["max_parallel"]
    frontier = graph.frontier()
    if not frontier:
        # A tick that dispatched nothing must not be consumed: doing so
        # would let a run stuck on one slow task burn through tick
        # numbers forever with zero `dispatched` records to show for it.
        running = sorted(t for t, task in graph.tasks.items()
                         if task["status"] == "running")
        print(f"nothing to dispatch: {len(running)} task(s) in flight "
              f"({', '.join(running) or 'none'}).")
        last_dispatched = _last_dispatched_tick(events)
        if last_dispatched is None:
            print("No tick has been dispatched yet; there is nothing to "
                  "submit.")
        else:
            print(f"Run `research submit --tick {last_dispatched}` once "
                  "its artifacts are written.")
        return 0

    # The cap is applied inside build_packet, AFTER skip-filtering — the
    # whole frontier is handed over, uncapped, and build_packet walks it
    # in order until it has `cap` real dispatches. Slicing to `cap` ids
    # here, before a corrupt task can be told apart from a healthy one,
    # was the livelock this fix exists to close: see build_packet's
    # docstring.
    packet = build_packet(memory, graph, cfg, events, tick, frontier,
                          cap=cap)
    if not packet.dispatches and not packet.skipped:
        # Nothing was dispatched AND nothing was skipped: this walk made
        # no judgement about anything, so there is nothing for `submit`
        # to act on and consuming a tick would be the same runaway the
        # empty-frontier branch above exists to avoid. Unreachable with a
        # non-empty frontier as things stand — build_packet either
        # dispatches or skips every id it reaches, and run.json floors
        # max_parallel at 1 — and kept as the guard for a cap that ever
        # reaches 0.
        print(render(packet, cfg, workspace.skill_dir(), root))
        return 0
    # An all-skipped frontier DOES consume a tick, and must. `submit`
    # step 6 is the only thing that ever charges an attempt against a
    # task `next` could not build a packet for, and it reads that
    # judgement back from the `dispatch_skipped` records journaled below.
    # Returning early here — before the journaling, before the tick
    # advance — meant the attempt was never charged, the task was never
    # abandoned, and four consecutive `next` calls changed nothing: a
    # permanent livelock with real work outstanding and no halt, because
    # `frontier()` still saw the task as open.
    #
    # The `dispatched` record is written even when `task_ids` is empty,
    # and that is the shape the fix turns on: `submit --tick N` raises
    # "tick N was never dispatched" if no such record exists, so
    # journaling `dispatch_skipped` alone would age nothing out and hand
    # the operator a command guaranteed to fail. An empty dispatch record
    # is also exactly what the reprint path needs — `next` run again
    # reprints an empty packet and points at the same submit rather than
    # recomputing a frontier that is still just as unbuildable.
    dispatched_ids = sorted(dispatch.task_id for dispatch in packet.dispatches)
    for dispatch in packet.dispatches:
        memory.update(dispatch.task_id, status="running")
    graph.invalidate_cache()
    journal_mod.append(
        root, "dispatched", tick=tick, task_ids=dispatched_ids,
        agents={d.task_id: d.agent for d in packet.dispatches},
        models={d.task_id: d.model for d in packet.dispatches})
    for task_id, reason in packet.skipped:
        # Carry-forward: a task this walk could not build an input packet
        # for (agent_input raising — see build_packet's per-task guard)
        # never becomes `running` and never enters `dispatched_ids` above,
        # so nothing else in the loop ever costs it an attempt or notices
        # it is permanently stuck. Journaling the decision already made
        # here — and the exact reason given — is what lets `submit` age
        # it out later without re-deriving the same judgement itself.
        journal_mod.append(root, "dispatch_skipped", tick=tick,
                           task=task_id, reason=reason)
    cfg["status"]["tick"] = tick
    # Guarded, not unconditional. `research synthesize` sets "synthesize"
    # and then tells the operator to run this very command, which
    # dispatches the outliner and one writer per section — all of it work
    # in the synthesize phase, none of it research. Overwriting here meant
    # the phase never survived a single tick, so when the writers finished
    # and the loop halted, `research status` fell through to its `elif
    # halted:` branch and offered `research continue` or `research
    # synthesize` — never `research render`, the one command left to run.
    # "done" is guarded for the same reason: a rendered run is finished,
    # and a stray `next` must not reopen it.
    if cfg["status"]["phase"] not in ("synthesize", "done"):
        cfg["status"]["phase"] = "research"
    runconfig.save(root, cfg)
    print(render(packet, cfg, workspace.skill_dir(), root))
    return 0
