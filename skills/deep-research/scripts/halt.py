"""The halt predicates, the progress digest, and out/status.md.

Spec section 4 lists exactly three predicates: signal, coverage,
saturation. There is deliberately **no budget** predicate — "the loop runs
indefinitely" — and none must be added. A budget condition would silently
truncate a run the user explicitly asked to leave going, and the
mitigation the spec chose for unbounded breadth is the progress digest
plus `research signal`, not a cap.

Order matters and is checked in this order:

  signal      the user's instruction outranks the graph's opinion
  coverage    a finished run is finished, not stalled
  saturation  only then is "nothing new is coming" the right story

Two readings of the spec's wording are stated in the plan and encoded
below: the open set is the *dispatchable* open set (a task over the depth
cap would otherwise hold a run open forever), and the evidence bar applies
to hypotheses that are not `refuted` (a refuted claim can never gather
three verified citations for itself).

A third reading, carried forward from an earlier review: a task whose file
fails its own schema is invisible to `Graph.over_cap`,
`Graph.eventually_dispatchable` and `Graph.undispatchable` alike — correct
for scheduling, since none of those may trust a field a malformed task
might be missing, but wrong for reporting. `coverage_halt` would otherwise
fire clean while a schema-invalid task sits outstanding, unmentioned. Both
`coverage_halt`'s detail and `render_status`'s open questions therefore
separately list `set(memory.ids("task")) - set(graph.valid_task_ids())` —
every task id on disk that Graph could not certify, whether or not it even
parsed.
"""
from dataclasses import dataclass
from pathlib import Path

import atomicio
import gates
import journal as journal_mod
import memory as memory_mod
import nodes
import predicates
import runconfig


@dataclass(frozen=True)
class Halt:
    reason: str   # "signal" | "coverage" | "saturation"
    detail: str


def signal_halt(memory, graph, cfg):
    """The user asked, directly or conditionally.

    `predicates.evaluate` calls `predicates.validate` first and raises
    `PredicateError` if the stored predicate no longer compiles. That is
    deliberately NOT caught here. `research signal stop-when` (signals.py)
    validates a predicate before it is ever written to run.yaml, so this
    can only raise on a stop_when corrupted outside that path — a hand
    edit, or disk damage. Swallowing the error and treating it as "not yet
    satisfied" would silently disable the one stop condition the user
    explicitly confirmed, forever, with nothing in status.md to say so;
    that is a worse outcome than the loud, immediate failure a raise
    gives, which at least tells the operator exactly what to fix
    (`research signal stop`, or repair run.yaml) rather than letting a
    multi-day run keep going on a promise it can never keep.
    """
    signals = cfg["signals"]
    if signals["stop_requested"]:
        return Halt("signal", "the user asked to stop")
    predicate = signals["stop_when"]
    if predicate and predicates.evaluate(predicate, memory, graph):
        return Halt("signal",
                    "the confirmed stop condition now holds:\n"
                    + predicates.describe(predicate))
    return None


def evidence_exhausted(graph, hypothesis_id, hypothesis, live=None):
    """True when nothing can still gather evidence for this hypothesis.

    `live` is `set(graph.eventually_dispatchable())`, hoisted by callers
    that ask about more than one hypothesis: that query is a fixed-point
    walk over the whole task graph, and recomputing it per hypothesis
    turns a status render on a multi-day run into O(hypotheses x tasks^2).

    `ensure_evidence_tasks` spawns one search task per under-evidenced
    hypothesis, tagged `inputs.for_hypothesis`. This asks whether any of
    them can still run.

    Only ever consulted from `coverage_halt`, and only after it has
    established that `eventually_dispatchable()` is empty and no task is
    `running` — so an open task tagged for this hypothesis is by
    definition stranded, and `blocked` (which nothing in this codebase
    ever writes, and which `frontier()` never picks up) is a dead end
    too. Both are reported as open questions rather than waited on; a
    halt is not the end of a run, `research continue` resumes it.

    A hypothesis with no evidence task at all is NOT exhausted: submit
    step 4 runs `ensure_evidence_tasks` before the halt check, so one is
    about to exist and there is genuinely more work coming. That premise
    only holds where `ensure_evidence_tasks` would actually spawn, and
    there are exactly two cases where it declines; both are checked below,
    because a hypothesis it will never serve, waited on, is waited on
    forever.
    """
    valid = graph.valid_task_ids()
    if live is None:
        live = set(graph.eventually_dispatchable())
    found = False
    for task_id in sorted(valid):
        task = graph.tasks[task_id]
        inputs = task.get("inputs") or {}
        if inputs.get("for_hypothesis") != hypothesis_id:
            continue
        # Stance-filtered, and this is load-bearing. A refute search
        # carries the same `for_hypothesis` tag, and counting one here
        # would report a hypothesis as "evidence exhausted" on the
        # strength of a search that went looking for the OPPOSITE of the
        # evidence this function is asking about — so a claim short on
        # support would be filed as an open question the moment its
        # challenge came back, with no confirmatory search ever having
        # run. Mirrors the split in apply._open_for_hypothesis.
        if (inputs.get("stance") or "for") != "for":
            continue
        found = True
        if task["status"] == "running" or task_id in live:
            return False
    if found:
        return True
    # Decline 1: the hypothesis's own provenance task is missing or
    # malformed. `ensure_evidence_tasks` drops exactly that case, so no
    # task will ever appear.
    parent = hypothesis["provenance"]["task"]
    if parent is None or parent not in valid:
        return True
    # Decline 2: a gate-2 re-check is still live for one of its citations,
    # so `ensure_evidence_tasks` leaves the gap to close on its own. That
    # is the right call while the re-check can still run — and this asks
    # the same query rather than re-deriving it, so the two cannot drift.
    # But an OPEN re-check that is neither `running` nor in `live` can
    # never reach the frontier (past the depth cap, or waiting on a
    # dependency that will never be done): it holds the veto while
    # contributing nothing to `eventually_dispatchable()`, so nothing will
    # check those citations and nothing will go looking for others. That
    # is exhausted. Without this the run sits on an empty frontier with no
    # halt, which is the same livelock the abandoned re-check produced.
    blocking = graph.live_rechecks_for(hypothesis_id)
    if blocking:
        return not any(graph.tasks[task_id]["status"] == "running"
                       or task_id in live for task_id in blocking)
    return False


# A claim the report stands behind, and so one that must have been
# challenged. Mirrors apply.PROMOTED_STATUSES; kept as its own constant
# rather than imported because halt.py imports nothing from apply, and a
# cycle here would be worse than a duplicated pair. tests pin them equal.
PROMOTED_STATUSES = ("supported", "contested")

# Phases in which no refute task can ever be created, so requiring one
# would refuse forever. submit skips ALL follow-on scheduling once the
# phase is `synthesize` — the outline is frozen and new evidence could
# not reach a section anyway — which means a promoted claim that reached
# synthesis unchallenged can never acquire a challenge. Requiring one
# there would make the synthesis phase unhaltable and the report
# unrenderable.
NO_SCHEDULING_PHASES = ("synthesize", "done")


def refutation_attempted(graph, hypothesis_id, hypothesis, live=None):
    """True when this claim has faced a search for its own disproof, or
    never can.

    Deliberately shaped like `evidence_exhausted` above rather than
    invented fresh, because that function is what fixed this predicate's
    documented history of being unfireable. It does not ask "is this
    claim well challenged"; it asks "can anything still challenge it".
    Every terminal state counts as done:

    - ran and returned sources, or ran and returned none — challenged.
      An empty result is a real answer: the claim survived a search for
      its opposite, and searcher.md says so.
    - abandoned after max_attempts — challenged, and reported in
      Appendix C. It never re-enters the frontier, so waiting on it
      waits forever.
    - open but NOT dispatchable — stranded past the depth cap or behind
      a dependency that will never be done. Reported, not waited on.
    - open and dispatchable, or running — not yet. Keep going.

    `live` is hoisted by the caller for the same O(hypotheses x tasks^2)
    reason evidence_exhausted gives.
    """
    valid = graph.valid_task_ids()
    if live is None:
        live = set(graph.eventually_dispatchable())
    found = False
    for task_id in sorted(valid):
        task = graph.tasks[task_id]
        inputs = task.get("inputs") or {}
        if inputs.get("for_hypothesis") != hypothesis_id:
            continue
        if inputs.get("stance") != "against":
            continue
        found = True
        if task["status"] == "running" or task_id in live:
            return False
    if found:
        return True
    # Decline: the claim's own provenance task is missing or malformed.
    # `ensure_refute_tasks` drops exactly that case, so no refute task
    # will ever appear for it. evidence_exhausted's decline 1, mirrored.
    parent = hypothesis["provenance"]["task"]
    if parent is None or parent not in valid:
        return True
    return False


def coverage_halt(memory, graph, cfg):
    """Nothing left to dispatch, and every claim we could evidence, we did.

    Not "every claim is properly evidenced". A hypothesis that failed
    gate 3, whose evidence-seeking search has already run and come back
    with nothing more, is a FINDING — it belongs in the open questions of
    the report — not a reason to run forever.

    Read the strong way, this predicate could not fire at all. It refused
    while any non-refuted hypothesis failed gate 3, and
    `ensure_evidence_tasks` could not make new work for it either: the
    gap string is stable, so `TASK_KEY` resolves to the search task that
    already ran, `create_task` reuses it, and `open_for` never sees it
    because it is `done`. Reproduced at 13 tasks done, 0 in flight, "6 of
    6 dry", no halt, forever — with `saturation_halt` unable to rescue it
    for a separate reason (see Graph.theme_of).
    """
    outstanding = graph.eventually_dispatchable()
    if outstanding:
        return None

    valid = graph.valid_task_ids()
    # `running` is not in OPEN_TASK_STATUSES, so it is not in
    # eventually_dispatchable either. A tick in flight must not read as a
    # finished run. Gated on valid_task_ids() before indexing `status`,
    # the same way every other Graph query does: a schema-invalid task may
    # be missing the field entirely.
    in_flight = sorted(task_id for task_id in valid
                       if graph.tasks[task_id]["status"] == "running")
    if in_flight:
        return None

    # Two buckets, and the difference is whether anything can still be
    # done about it. `awaited` holds the run open; `thin` is reported.
    thin, awaited, unchallenged = [], [], []
    scheduling = cfg["status"]["phase"] not in NO_SCHEDULING_PHASES
    for hypothesis_id, hypothesis in graph.readable("hypothesis"):
        # A promoted claim must have faced a search for its own
        # disproof. Checked here rather than left to emerge from
        # submit's step 4 happening to run before the next halt check:
        # that ordering is what makes it work today, nothing tested it,
        # and a reorder would silently remove the guarantee that this
        # run's report rests on.
        if scheduling and hypothesis["status"] in PROMOTED_STATUSES:
            if not refutation_attempted(graph, hypothesis_id, hypothesis,
                                        live=set(outstanding)):
                unchallenged.append(hypothesis_id)
        if hypothesis["status"] == "refuted":
            continue
        gap = gates.evidence_gap(graph, cfg, hypothesis_id)
        if not gap:
            continue
        # `outstanding` is empty here — the early return above guarantees
        # it — so it IS set(graph.eventually_dispatchable()), reused
        # rather than recomputed once per hypothesis.
        if evidence_exhausted(graph, hypothesis_id, hypothesis,
                              live=set(outstanding)):
            thin.append(f"{hypothesis_id}: {gap}")
        else:
            awaited.append(hypothesis_id)
    if awaited or unchallenged:
        return None

    stranded = graph.undispatchable()
    # Carry-forward: a task whose file fails its own schema is invisible
    # to eventually_dispatchable() and undispatchable() alike, because
    # neither may trust a field it might be missing. That is correct for
    # scheduling but would let coverage fire clean with a malformed task
    # outstanding and unmentioned. Name it here.
    malformed = sorted(set(memory.ids("task")) - valid)

    if thin:
        detail = ("no dispatchable task remains, and every hypothesis that "
                  "could still be evidenced has been")
        detail += (f"; {len(thin)} hypothesis(es) fell short of the evidence "
                   "bar with no evidence-seeking task left to run, and are "
                   "reported as open questions: " + "; ".join(sorted(thin)))
    else:
        detail = ("no dispatchable task remains and every hypothesis meets "
                  "the evidence bar")
    if stranded:
        detail += (f"; {len(stranded)} task(s) can never be dispatched and "
                   "are reported as open questions")
    if malformed:
        detail += (f"; {len(malformed)} task(s) are schema-invalid and "
                   f"invisible to scheduling: {', '.join(malformed)}")
    return Halt("coverage", detail)


def saturation_halt(cfg, events):
    """The last N completions produced nothing new.

    The branch count is a guard, not a second condition: six dry
    completions inside one branch say that branch is exhausted, which is
    no reason to abandon the others.
    """
    window = cfg["config"]["saturation_window"]
    needed_branches = cfg["config"]["saturation_branches"]
    recent = journal_mod.completions(events)[-window:]
    if len(recent) < window:
        return None
    branches = {record.get("root_branch") for record in recent}
    branches.discard(None)
    if len(branches) < needed_branches:
        return None
    if any(record.get("new_facts") or record.get("new_domains")
           for record in recent):
        return None
    return Halt(
        "saturation",
        f"the last {window} completed tasks, across {len(branches)} "
        "branches, produced no new facts and no new domains",
    )


def check(memory, graph, cfg, events):
    """The first predicate that fires, or None.

    Runs every tick, before `next` computes a frontier. May raise
    `predicates.PredicateError` — see `signal_halt`'s docstring for why
    that is deliberate rather than caught.
    """
    return (signal_halt(memory, graph, cfg)
            or coverage_halt(memory, graph, cfg)
            or saturation_halt(cfg, events))


def _counts(memory, graph, cfg, events):
    tasks = graph.tasks
    valid = graph.valid_task_ids()
    by_status = {}
    for task_id in valid:
        status = graph.tasks[task_id]["status"]
        by_status[status] = by_status.get(status, 0) + 1

    facts, domains_seen = 0, set()
    for _, fact in graph.readable("fact"):
        if fact["status"] != "active":
            continue
        facts += 1
        for citation_id in fact["citations"]:
            try:
                citation = memory.read(citation_id)
                memory.validate(citation)
            except (KeyError, nodes.NodeFormatError,
                    memory_mod.ValidationError):
                continue
            domains_seen.add(citation["domain"])

    hypotheses = {}
    for _, hypothesis in graph.readable("hypothesis"):
        status = hypothesis["status"]
        hypotheses[status] = hypotheses.get(status, 0) + 1

    window = cfg["config"]["saturation_window"]
    recent = journal_mod.completions(events)[-window:]
    dry = sum(1 for record in recent
              if not record.get("new_facts") and not record.get("new_domains"))

    return {"tasks": tasks, "by_status": by_status, "facts": facts,
            "domains": len(domains_seen), "hypotheses": hypotheses,
            "dry": dry, "window": window}


def digest(memory, graph, cfg, events):
    """Spec section 4's one-line progress notice. Never a gate.

    Its "domains" count is informational and deliberately looser than
    coverage_halt's evidence bar: it counts the domain of every
    schema-valid citation attached to an active fact, regardless of that
    citation's own status, where coverage_halt (via
    Graph.supporting_domains -> live_citations) counts only `verified`
    ones. That gap is intentional — this is a notice meant to show the
    run's overall shape at a glance, not a second implementation of gate
    3's promotion arithmetic — but it means "domains N" here can be
    larger than what coverage actually requires before it will halt.
    """
    counts = _counts(memory, graph, cfg, events)
    status = counts["by_status"]
    hypotheses = counts["hypotheses"]
    return (
        f"TICK {cfg['status']['tick']} | "
        f"tasks {len(counts['tasks'])} (done {status.get('done', 0)}, "
        f"ready {len(graph.frontier())}, abandoned "
        f"{status.get('abandoned', 0)}) | "
        f"facts {counts['facts']} | domains {counts['domains']} | "
        f"hypotheses {sum(hypotheses.values())} "
        f"(supported {hypotheses.get('supported', 0)}, "
        f"contested {hypotheses.get('contested', 0)}) | "
        f"saturation window: {counts['dry']} of {counts['window']} dry"
    )


def _bullets(items, empty="none"):
    return "\n".join(f"- {item}" for item in items) if items else f"_{empty}_"


def render_status(memory, graph, cfg, events, halted=None):
    """out/status.md, and the body of `research status`.

    One renderer, two sinks. The halt path writes it to disk; `research
    status` prints it. Keeping them the same text means what the user
    reads days later is what the loop actually decided.
    """
    lines = [f"# {cfg['question']}", ""]
    if halted:
        lines += [f"**HALTED({halted.reason})** — {halted.detail}", ""]
    else:
        lines += [f"Phase **{cfg['status']['phase']}**, tick "
                  f"{cfg['status']['tick']}.", ""]
    lines += ["```", digest(memory, graph, cfg, events), "```", ""]

    for warning in runconfig.warnings(cfg):
        lines += [f"> **config warning** {warning}", ""]

    pending = [c for c in cfg["signals"]["checkpoints"] if not c["resolved"]]
    if pending:
        lines += ["## Waiting on you", ""]
        lines += [_bullets(
            [f"(raised at tick {c['raised_at_tick']}) {c['note']}"
             for c in pending])]
        lines += [""]

    valid = graph.valid_task_ids()
    open_questions = []
    for task_id in sorted(valid):
        task = graph.tasks[task_id]
        if task["status"] == "abandoned":
            reason = task.get("abandoned_reason") or "no reason recorded"
            open_questions.append(f"`{task_id}` {task['question']} "
                                  f"— abandoned: {reason}")
    for task_id in graph.undispatchable():
        task = graph.tasks[task_id]
        open_questions.append(
            f"`{task_id}` {task['question']} — cannot be dispatched "
            f"(depth {task['depth']}, waiting on "
            f"{', '.join(task['depends_on']) or 'nothing'})")
    # Carry-forward: a task whose file fails its own schema check is
    # invisible to over_cap()/eventually_dispatchable()/undispatchable()
    # alike, so it would otherwise never appear here, and a halt would
    # read as completion even though a task's fate is unknown.
    for task_id in sorted(set(memory.ids("task")) - valid):
        open_questions.append(
            f"`{task_id}` — task file is schema-invalid or unreadable; "
            "excluded from scheduling and its status cannot be confirmed")
    # The other half of coverage_halt's ruling: a claim we tried and
    # failed to evidence is a finding, and coverage no longer waits on
    # it — so this is the only place the user ever hears about it. Listed
    # whether or not the run has halted, because it is just as true
    # mid-run, and named with the gate-3 gap so it says what fell short.
    dispatchable = set(graph.eventually_dispatchable())
    for hypothesis_id, hypothesis in graph.readable("hypothesis"):
        if hypothesis["status"] == "refuted":
            continue
        gap = gates.evidence_gap(graph, cfg, hypothesis_id)
        if gap and evidence_exhausted(graph, hypothesis_id, hypothesis,
                                      live=dispatchable):
            open_questions.append(
                f"`{hypothesis_id}` {hypothesis['claim']} — under-evidenced "
                f"and no evidence-seeking task remains: {gap}")
    lines += ["## Open questions", "", _bullets(open_questions), ""]

    refuted = [f"`{aid}` {a['statement']} — refuted by "
               f"{a['refuted_by'] or 'an unrecorded hypothesis'}"
               for aid, a in graph.readable("assumption")
               if a["status"] == "refuted"]
    lines += ["## Refuted assumptions", "", _bullets(refuted), ""]

    scored = sorted(
        ((h["confidence"], hid, h["claim"], h["status"])
         for hid, h in graph.readable("hypothesis")
         if h["status"] != "refuted"),
        key=lambda row: (row[0], row[1]),
    )
    weakest = [f"`{hid}` ({status}, {score}) {claim}"
               for score, hid, claim, status in scored[:10]]
    lines += ["## Weakest hypotheses", "", _bullets(weakest), ""]

    # What to actually do next. A halted run's documented options were
    # `continue` or stop, which silently omitted the third and most likely
    # one: the research is finished and the report is the point.
    phase = cfg["status"]["phase"]
    if phase == "synthesize":
        # `research continue` is named here only because it can now lift
        # the freeze and reopen research. While it could not, offering it
        # would have pointed the operator at a command that did nothing
        # about the state they were in; now that it does, leaving it out
        # is the omission — `research render` is not the only door out of
        # synthesis, and deciding to write the report is reversible.
        lines += ["", "Next: run the loop until it halts, then "
                      "`research render` — or `research continue` to lift "
                      "the freeze and go back to researching."]
    # No branch for "render". Nothing assigns that phase: scheduler writes
    # "research", synthesis "synthesize", render "done". It is not a state
    # a run rests in — it is one synchronous command — and the three
    # assigned phases plus `halted` already name every observable state.
    # Making it reachable would now be harmful rather than merely unused:
    # the phase write in scheduler.run is guarded, so a "render" set on a
    # failed build would survive the next `research next` and tell the
    # operator to render while a reopened writer was still pending.
    elif phase == "done":
        lines += ["", "This run is complete; the report is in out/."]
    elif halted:
        lines += ["", "Next: `research continue` to keep researching, or "
                      "`research synthesize` to write the report."]

    return "\n".join(lines)


def write_status(root, text):
    return atomicio.write_text(Path(root) / "out" / "status.md", text)


def record(root, cfg, halted):
    """Store the halt (or clear it) in run.yaml. Returns the config."""
    cfg["status"]["halted"] = None if halted is None else {
        "reason": halted.reason,
        "at_tick": cfg["status"]["tick"],
        "at": memory_mod.utcnow(),
        "detail": halted.detail,
    }
    runconfig.save(root, cfg)
    return cfg
