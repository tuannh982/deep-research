"""The verification harness. Gates run inside submit, cheapest first, so
the expensive gate only sees artifacts that survived the free ones.

    1  SCHEMA        code  ~0ms   this file, schema_check
    2  RE-CHECK      LLM   ~30s   a `recheck` task in the graph
    3  INDEPENDENCE  code  ~0ms   this file, independence (Task 10)
    4  ADVERSARIAL   LLM   ~30s   a `verify` task in the graph

Gates 2 and 4 are not here, and cannot be: the harness owns the model
call, so a Python script inside a skill cannot dispatch a subagent.
Gate 2 is the `rechecker` subagent (agents/rechecker.md); it re-reads a
citation's URL and reports whether the quote holds up, and apply_recheck
turns that verdict into VERIFIED / REJECTED / UNVERIFIABLE on the
citation. Gate 4 works the same way: accepting a `hypothesize` artifact
spawns a `verify` task instead, and the next tick's `next` dispatches the
verifier like any other agent. That is not a weakening — confidence.compute
weights a null verdict at 0.5 and base * spread cannot exceed 1.0, so an
unverified hypothesis cannot reach the 0.6 promotion threshold. Adversarial
verification is arithmetically mandatory for promotion, and Task 10 pins
that.
"""
import json
from pathlib import Path

import jsonschema

import latex

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

# Kinds an agent returns an artifact for. A kind must appear here, in
# apply.APPLIERS and in submit.KIND_ORDER together: artifact_schema RAISES
# KeyError for a kind it does not know, submit catches only ApplyError
# around it, and the result is a bare traceback that takes down the whole
# tick rather than rejecting one artifact.
ARTIFACT_KINDS = ("decompose", "search", "extract", "recheck", "hypothesize",
                  "verify", "outline", "synthesize")

# Citation states a section may cite. `verified` passed gate 2.
# `unverifiable` is a source we could not re-read — a 403, a JS wall — and
# spec section 6 requires it be "flagged rather than silently trusted",
# which means carried into the report and disclosed in Appendix D, not
# dropped. `rejected` failed gate 2 outright: its quote is not on the page,
# so citing it would put a fabricated source in the bibliography.
# `pending` has not faced the gate at all.
#
# Deliberately NOT the same set as Graph.live_citations, which is the
# promotion arithmetic and admits only `verified`. Confidence must not rest
# on a source nobody could read; the bibliography must still disclose it.
CITABLE_STATUSES = ("verified", "unverifiable")

_CACHE = {}


def artifact_schema(kind, schema_dir=None):
    if kind not in ARTIFACT_KINDS:
        raise KeyError(f"no artifact schema for kind {kind!r}")
    directory = Path(schema_dir) if schema_dir else SCHEMA_DIR
    key = (str(directory), kind)
    if key not in _CACHE:
        path = directory / f"artifact.{kind}.json"
        _CACHE[key] = json.loads(path.read_text(encoding="utf-8"))
    # Return a deep copy to prevent mutations of the cached schema from
    # affecting future validations.
    return json.loads(json.dumps(_CACHE[key]))


def schema_check(kind, artifact, task_id, schema_dir=None):
    """Gate 1. None if the artifact is acceptable, else why not.

    Returns a string rather than raising because the message has two
    destinations: journal.jsonl, and the retry prompt for the next
    attempt at this task (spec section 4). It has to read as an
    instruction to a model, so it names the path as well as the problem.

    An unknown `kind` raises instead: that is a scheduler bug, and
    reporting it as a rejected artifact would burn the task's attempts
    against a defect the model cannot fix.
    """
    schema = artifact_schema(kind, schema_dir)
    try:
        jsonschema.validate(artifact, schema)
    except jsonschema.ValidationError as error:
        where = "/".join(str(p) for p in error.absolute_path) or "<root>"
        return f"artifact.{kind} failed validation at {where}: {error.message}"
    # Checked after the schema so `artifact` is known to be an object with
    # a well-formed task_id by the time it is compared.
    if artifact["task_id"] != task_id:
        return (
            f"artifact.{kind} claims task_id {artifact['task_id']!r} but was "
            f"written to the inbox for {task_id!r}; an artifact must name the "
            "task it was dispatched for"
        )
    return None


VERIFIED = "verified"
REJECTED = "rejected"
UNVERIFIABLE = "unverifiable"


def independence(domains, min_citations, required_domains):
    """Gate 3. None if the evidence is independent enough to promote.

    `domains` is one eTLD+1 per live verified citation, duplicates
    included — produced by Graph.supporting_domains, which is produced by
    domains.registrable. The reduction is what makes the count meaningful:
    without it, blog.foo.com and foo.com look like two sources.

    Count before spread, because the reason string goes into a spawned
    task's prompt and "find more evidence" is different work from "find a
    different source".
    """
    if len(domains) < min_citations:
        return (
            f"gate 3: {len(domains)} verified citation(s), needs "
            f"{min_citations}"
        )
    distinct = len(set(domains))
    if distinct < required_domains:
        return (
            f"gate 3: {len(domains)} citation(s) span only {distinct} "
            f"registrable domain(s), needs {required_domains} — find a "
            "source on a different site"
        )
    return None


def evidence_gap(graph, cfg, hypothesis_id):
    """Gate 3 for one hypothesis in a graph. None if it clears the bar.

    Spec section 6: on failure, "spawn tasks seeking other domains". The
    returned string is what goes into that task's question, so it has to
    say which of the two bars was missed.
    """
    return independence(
        graph.supporting_domains(hypothesis_id),
        min_citations=cfg["config"]["min_citations"],
        required_domains=cfg["config"]["required_domains"],
    )


def report_section(body, section, graph):
    """Gate 5. None if the section body is acceptable, else why not.

    Spec section 6: "Prose is generated, so prose is validated." Three
    checks, returned as one message so a single retry can fix all of them.

    Two of the three resolve against the LIVE graph rather than the frozen
    section payload. `allowed_cite_keys` was fixed when apply_outline
    seeded this task; between then and now a re-check can have rejected a
    citation, and a cascade can have quarantined a fact. The frozen list
    says what the writer was offered; the graph says what is still true,
    and the report must rest on the second.
    """
    problems = []

    citable = {
        citation_id for citation_id, citation in graph.readable("citation")
        if citation["status"] in CITABLE_STATUSES
    }
    allowed = set(section.get("allowed_cite_keys") or [])
    bad_keys = sorted(
        key for key in latex.cite_keys(body)
        if key not in allowed or key not in citable
    )
    if bad_keys:
        problems.append(
            "these \\cite keys are not available to this section: "
            + ", ".join(bad_keys)
            + ". Cite only the ids listed in allowed_cite_keys"
        )

    active_facts = {
        fact_id for fact_id, fact in graph.readable("fact")
        if fact["status"] == "active"
    }
    dangling = sorted(reference for reference in latex.fact_refs(body)
                      if reference not in active_facts)
    if dangling:
        problems.append(
            "these \\factref ids do not resolve to an active fact: "
            + ", ".join(dangling)
        )

    unsourced = latex.unsourced_numerics(body)
    if unsourced:
        problems.append(
            "these sentences state a figure with no source; add a \\cite or "
            "a \\factref: " + " | ".join(unsourced)
        )

    return "; ".join(problems) if problems else None
