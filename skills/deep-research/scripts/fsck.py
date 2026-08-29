"""Whole-graph validation: schemas plus cross-reference integrity.

Reporting only. Repair is deliberately manual — a corrupted research graph
should be looked at, not silently rewritten.
"""
from dataclasses import dataclass

import memory as memory_mod
import nodes
from graph import CycleError


@dataclass
class Finding:
    severity: str  # "error" | "warning"
    node: str
    message: str


def errors(findings):
    return [f for f in findings if f.severity == "error"]


def _check_nodes_parse_and_validate(memory):
    """Returns (findings, unreadable_ids, invalid_ids).

    `unreadable` means the file did not even parse (bad YAML, no
    frontmatter, unknown type). Cross-reference checks below all iterate
    the store and would crash on a file like that, so check() bails out
    entirely on a non-empty `unreadable`.

    `invalid` means the file parsed fine but failed its JSON Schema —
    typically a required key deleted outright. This must NOT trigger the
    same bail-out: the schema-invalid node already has its own Finding
    here, and the rest of the graph is otherwise readable. Cross-reference
    and cycle checks skip ids in `invalid` (rather than indexing fields
    that may be missing) so one corrupt node degrades to "unchecked" for
    itself only, instead of discarding every finding for a healthy
    remainder of a multi-day run.
    """
    found, unreadable, invalid = [], set(), set()
    for node_type in nodes.NODE_TYPES:
        for node_id in memory.ids(node_type):
            try:
                data = memory.read(node_id)
            except nodes.NodeFormatError as error:
                found.append(Finding("error", node_id, f"unparseable: {error}"))
                unreadable.add(node_id)
                continue
            if data.get("id") != node_id:
                found.append(Finding(
                    "error", node_id,
                    f"frontmatter id is {data.get('id')!r}, filename says {node_id!r}",
                ))
            try:
                memory.validate(data)
            except memory_mod.ValidationError as error:
                # Narrow on purpose. A bare `except Exception` here would
                # swallow a bug in the checker itself and report it as a
                # finding against the user's data.
                found.append(Finding("error", node_id, str(error)))
                invalid.add(node_id)
    return found, unreadable, invalid


def _check_references(memory, graph, known, invalid):
    """Iterates filename ids (memory.ids(type)), not memory.list(type): the
    filename is the only id a schema-invalid node still guarantees, since
    `id` is itself a required field in every schema and so can be exactly
    the key that is missing. Skip-and-continue happens before the node is
    even read, and every Finding below is labelled with the filename id,
    not anything out of the node's own (possibly missing-a-key) content.
    """
    found = []
    # provenance.task is on every node type, and it is the edge the
    # cascade's fact-quarantine and hypothesis-demotion passes match on.
    # A dangling one means the node is provenanced to a task that can
    # never enter the affected set, i.e. silent under-invalidation — the
    # same failure the null-provenance check below already guards against,
    # only harder to spot because the field looks populated.
    for node_type in nodes.NODE_TYPES:
        for node_id in memory.ids(node_type):
            if node_id in invalid:
                continue
            origin = memory.read(node_id)["provenance"]["task"]
            if origin is not None and origin not in known:
                found.append(Finding("error", node_id,
                                     f"provenance.task dangling: {origin}"))
    for task_id in memory.ids("task"):
        if task_id in invalid:
            continue  # already has its own validation Finding; fields may be missing
        task = memory.read(task_id)
        for dep in task["depends_on"]:
            if dep not in known:
                found.append(Finding("error", task_id,
                                     f"depends_on dangling: {dep}"))
        if task["parent"] and task["parent"] not in known:
            found.append(Finding("error", task_id,
                                 f"parent dangling: {task['parent']}"))
        if task["depth"] > graph.max_depth:
            found.append(Finding(
                "error", task_id,
                f"depth {task['depth']} exceeds cap {graph.max_depth}",
            ))
    for fact_id in memory.ids("fact"):
        if fact_id in invalid:
            continue
        fact = memory.read(fact_id)
        if not fact["citations"]:
            found.append(Finding("error", fact_id, "fact has no citations"))
        for citation in fact["citations"]:
            if citation not in known:
                found.append(Finding("error", fact_id,
                                     f"citation dangling: {citation}"))
        if fact["provenance"]["task"] is None:
            # `None in affected` is always False, so a fact provenanced to
            # no task can never be reached by the invalidation cascade
            # (Graph.cascade). The schema allows null here; fsck flags it.
            found.append(Finding(
                "error", fact_id,
                "provenance.task is null: fact is permanently immune to "
                "the invalidation cascade",
            ))
    for assumption_id in memory.ids("assumption"):
        if assumption_id in invalid:
            continue
        assumption = memory.read(assumption_id)
        if assumption["raised_by"] not in known:
            found.append(Finding(
                "error", assumption_id,
                f"raised_by dangling: {assumption['raised_by']}",
            ))
        if assumption["refuted_by"] and assumption["refuted_by"] not in known:
            found.append(Finding(
                "error", assumption_id,
                f"refuted_by dangling: {assumption['refuted_by']}",
            ))
        if assumption["status"] == "refuted" and not assumption.get("cascaded"):
            # apply.run_cascades sets `cascaded` only once graph.cascade()
            # has actually run for this assumption (schemas/assumption.json).
            # "refuted" and "refuted and cascaded" are otherwise
            # indistinguishable, and nothing sweeps the store looking for
            # the gap: a normal tick only ever feeds run_cascades the ids
            # collected during that same tick, so an assumption refuted
            # by one tick and never cascaded (a crash, a schema defect
            # that made run_cascades skip it, or anything else) stays
            # silently uninvalidated forever unless something reports it.
            # This is that report.
            found.append(Finding(
                "error", assumption_id,
                "refuted but not cascaded: run_cascades has not "
                "invalidated what rested on this assumption",
            ))
        # `blocks` is how the cascade extends the affected set past the
        # raiser's own subtree (Graph.cascade). An entry pointing at
        # nothing quietly narrows that set.
        for blocked in assumption["blocks"]:
            if blocked not in known:
                found.append(Finding("error", assumption_id,
                                     f"blocks dangling: {blocked}"))
    for hypothesis_id in memory.ids("hypothesis"):
        if hypothesis_id in invalid:
            continue
        hypothesis = memory.read(hypothesis_id)
        for citation in hypothesis["supporting"] + hypothesis["counter"]:
            if citation not in known:
                found.append(Finding("error", hypothesis_id,
                                     f"citation dangling: {citation}"))
    return found


def _check_parent_cycles(memory, graph):
    """A cycle among `parent` pointers is invisible to graph.find_cycle(),
    which only walks depends_on. Graph.root_branch() already raises
    CycleError on exactly this condition; catch it per task rather than
    let it escape check().

    Dedup here is partial, not exact. When a CycleError is caught, this
    replays the parent walk from the task that triggered it and marks
    every id visited on that one walk as reported, so nothing on that
    specific walk (the cycle's own members, plus whatever upstream feeder
    chain led into it from this starting point) is re-processed. It does
    NOT collapse two different tasks that feed into the very same cycle by
    two different paths: e.g. a T-001<->T-002 cycle with T-003.parent =
    T-001 and T-004.parent = T-002 produces three findings (one per walk),
    not one for the whole cycle, because T-003 and T-004 are never visited
    by each other's walk. That is over-reporting, not under-reporting, so
    it is left as-is rather than doing full cycle-membership analysis just
    to fully dedup — for a report-only tool, more findings is the safe
    direction to err in.
    """
    found, reported = [], set()
    for task_id in memory.ids("task"):
        if task_id in reported:
            continue
        try:
            graph.root_branch(task_id)
        except CycleError:
            chain, current = [], task_id
            while current not in chain and current in graph.tasks:
                chain.append(current)
                current = graph.tasks[current]["parent"]
            reported.update(chain)
            # `current` is the id the walk closed back onto. It cannot be
            # None here — root_branch() only raised because the same walk
            # revisited an id — but " -> ".join() would raise TypeError
            # rather than report anything if a future change let a None
            # parent terminate the walk, so drop it explicitly.
            trail = chain if current is None else chain + [current]
            found.append(Finding(
                "error", chain[0], "parent cycle: " + " -> ".join(trail),
            ))
    return found


def _check_orphan_citations(memory, invalid):
    cited = set()
    for fact_id in memory.ids("fact"):
        if fact_id in invalid:
            continue
        cited.update(memory.read(fact_id)["citations"])
    for hypothesis_id in memory.ids("hypothesis"):
        if hypothesis_id in invalid:
            continue
        hypothesis = memory.read(hypothesis_id)
        cited.update(hypothesis["supporting"])
        cited.update(hypothesis["counter"])
    return [
        Finding("warning", citation_id, "citation is referenced by nothing")
        for citation_id in memory.ids("citation")
        if citation_id not in cited
    ]


def _sorted(findings):
    return sorted(findings, key=lambda f: (f.severity, f.node, f.message))


def check(memory, graph):
    findings, unreadable, invalid = _check_nodes_parse_and_validate(memory)
    if unreadable:
        # Every check below walks the whole store. Stop here rather than
        # crash on the file we already know is broken.
        return _sorted(findings)

    # A schema-invalid node's file still exists, so a reference pointing
    # at it is not dangling — `known` is deliberately built from every id
    # on disk, invalid or not.
    known = set(memory.all_ids())
    findings += _check_references(memory, graph, known, invalid)

    # _check_parent_cycles() indexes `parent` directly
    # (graph.tasks[current]["parent"]), so it can still raise KeyError on a
    # schema-invalid task. find_cycle() no longer needs this guard for
    # itself -- it reads depends_on via .get() now -- but running it while
    # skipping only the parent-cycle check would report a cycle pass that
    # never happened. Skip cycle detection entirely while any task is
    # malformed, and say so with a Finding rather than skipping silently.
    malformed_tasks = {i for i in invalid if nodes.type_of(i) == "task"}
    if malformed_tasks:
        findings.append(Finding(
            "warning", "<graph>",
            "cycle detection skipped: malformed task(s) present: "
            + ", ".join(sorted(malformed_tasks)),
        ))
    else:
        cycle = graph.find_cycle()
        if cycle:
            findings.append(Finding("error", cycle[0],
                                    "dependency cycle: " + " -> ".join(cycle)))
        findings += _check_parent_cycles(memory, graph)

    findings += _check_orphan_citations(memory, invalid)
    return _sorted(findings)
