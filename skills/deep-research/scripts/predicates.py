"""Conditional stop signals, compiled to a predicate over the graph.

Spec section 4: chat becomes state on disk, and "the predicate is then
evaluated by code like any other". The clause set is closed and every
clause carries exactly one condition, so there is nothing to interpret at
evaluation time and nothing a model can smuggle in. "Stop when the answer
feels complete" does not fail to work — it fails to validate.
"""
import json
from pathlib import Path

import jsonschema

import domains
from graph import CycleError

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

# Every condition the schema accepts. Kept beside the evaluator so a
# condition cannot be added to one and forgotten in the other — a
# condition the schema allows and evaluate() ignores would never fire,
# which is the worst possible failure for a stop signal.
CONDITIONS = ("tasks_resolved", "min_hypothesis_confidence", "min_facts",
              "min_domains", "min_supported_hypotheses")

_SCHEMA = None


class PredicateError(ValueError):
    """The request does not compile to a predicate over the graph."""


def _schema():
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = json.loads(
            (SCHEMA_DIR / "stop_predicate.json").read_text(encoding="utf-8"))
    return _SCHEMA


def validate(predicate):
    try:
        jsonschema.validate(predicate, _schema())
    except jsonschema.ValidationError as error:
        where = "/".join(str(p) for p in error.absolute_path) or "<root>"
        raise PredicateError(
            f"not a stop predicate at {where}: {error.message}. Legal "
            f"conditions are: {', '.join(CONDITIONS)}."
        ) from None


def describe(predicate):
    """The echo the user confirms. One line per clause."""
    mode = "all" if "all" in predicate else "any"
    joiner = "all of" if mode == "all" else "any one of"
    lines = [f"Stop when {joiner} these hold:"]
    for clause in predicate[mode]:
        scope = clause.get("branch") or "the whole run"
        condition = next(k for k in clause if k != "branch")
        value = clause[condition]
        if condition == "tasks_resolved":
            lines.append(f"  - {scope}: no dispatchable task remains")
        elif condition == "min_hypothesis_confidence":
            lines.append(f"  - {scope}: every unrefuted hypothesis is at "
                         f"confidence >= {value}")
        elif condition == "min_facts":
            lines.append(f"  - {scope}: at least {value} active fact(s)")
        elif condition == "min_domains":
            lines.append(f"  - {scope}: facts span at least {value} "
                         f"registrable domain(s)")
        else:
            lines.append(f"  - {scope}: at least {value} supported "
                         f"hypothesis(es)")
    return "\n".join(lines)


def _branch_of(graph, task_id):
    if task_id is None or task_id not in graph.tasks:
        return None
    try:
        return graph.root_branch(task_id)
    except CycleError:
        return None  # fsck reports it


def _in_scope(graph, branch, task_id):
    if branch is None:
        return True
    # The branch's own root task counts like any other task in scope.
    # A never-dispatched root (or one re-staled by a cascade that named
    # it directly in an assumption's `blocks` — the invalidation
    # closure in graph.cascade() walks depends_on, not parent, so a
    # staled root's children are not staled with it, and a freshly
    # created root has no descendants at all to carry the signal in
    # their place) IS open work: excluding it would let a branch that
    # has not run a single tick evaluate `tasks_resolved: True`.
    return _branch_of(graph, task_id) == branch


def _facts_in_scope(memory, graph, branch):
    return [
        fact for _, fact in graph.readable("fact")
        if fact["status"] == "active"
        and _in_scope(graph, branch, fact["provenance"]["task"])
    ]


def _hypotheses_in_scope(graph, branch):
    return [
        hypothesis for _, hypothesis in graph.readable("hypothesis")
        if _in_scope(graph, branch, hypothesis["provenance"]["task"])
    ]


def _clause(clause, memory, graph):
    branch = clause.get("branch")
    if branch is not None and branch not in graph.tasks:
        # A scope that does not exist is never satisfied. Treating it as
        # vacuously true would stop the run on a typo.
        return False
    condition = next((k for k in clause if k != "branch"), None)
    if condition not in CONDITIONS:
        raise PredicateError(f"unknown condition {condition!r}")
    value = clause[condition]

    if condition == "tasks_resolved":
        # Undispatchable tasks do not count: one task waiting on a
        # dependency that does not exist would otherwise hold a
        # conditional stop open forever.
        return not [
            task_id for task_id in graph.eventually_dispatchable()
            if _in_scope(graph, branch, task_id)
        ]

    if condition == "min_facts":
        return len(_facts_in_scope(memory, graph, branch)) >= value

    if condition == "min_domains":
        # Same eTLD+1 reduction as gate 3. citation.domain is already
        # reduced at creation; registrable() here is idempotent and
        # protects against a hand-edited node.
        seen = set()
        for fact in _facts_in_scope(memory, graph, branch):
            for citation_id in fact["citations"]:
                try:
                    citation = memory.read(citation_id)
                    memory.validate(citation)
                except Exception:
                    continue
                try:
                    seen.add(domains.registrable(citation["domain"]))
                except ValueError:
                    continue
        return len(seen) >= value

    hypotheses = _hypotheses_in_scope(graph, branch)
    if condition == "min_supported_hypotheses":
        return len([h for h in hypotheses
                    if h["status"] == "supported"]) >= value

    # min_hypothesis_confidence. Refuted claims are settled answers, not
    # under-evidenced ones, so they are excluded — otherwise the
    # condition becomes unsatisfiable the moment anything is disproven.
    live = [h for h in hypotheses if h["status"] != "refuted"]
    if not live:
        return False
    return all(h["confidence"] >= value for h in live)


def evaluate(predicate, memory, graph):
    """True when the predicate holds over the current graph."""
    validate(predicate)
    mode = "all" if "all" in predicate else "any"
    results = (_clause(c, memory, graph) for c in predicate[mode])
    return all(results) if mode == "all" else any(results)
