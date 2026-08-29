"""Turn a gate-passed artifact into graph writes.

Idempotent by natural key, not by transaction log. Spec section 8
requires that "a crash mid-tick is recovered by re-running the same
submit", and this is how: every node written here has a key derived from
its content and provenance, and nothing is created if a node with that
key already exists. A re-run converges whether the crash landed before,
during or after the journal write. Citation dedup is separately required
by spec section 2 -- "a source cited by twelve facts must not be stored
twelve times".

Nothing here writes node files directly; memory.py stays the only writer.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

import atomicio
import confidence as confidence_mod
import domains
import evidence
import gates
import latex
import memory as memory_mod
import nodes
import outline as outline_mod
from graph import CycleError, OPEN_TASK_STATUSES


class ApplyError(ValueError):
    """The artifact passed gate 1 but refers to something that is not there.

    Distinct from a schema failure because the cause is different: the
    shape was right and the content was wrong. submit treats it the same
    way -- attempts incremented, reason journaled and fed back into the
    retry prompt -- but the message tells the model something a schema
    never could.
    """


@dataclass
class ApplyResult:
    created: list = field(default_factory=list)
    reused: list = field(default_factory=list)
    # (what, why) for everything code declined to write. Not a failure:
    # the artifact was fine and the graph's own rules pruned it.
    dropped: list = field(default_factory=list)
    spawned: list = field(default_factory=list)
    cascaded: list = field(default_factory=list)
    rejected_citations: list = field(default_factory=list)
    unverifiable_citations: list = field(default_factory=list)
    # Facts a cascade had quarantined, whose evidence this pass re-checked
    # and gate 2 verified again. Not `created` (the node already existed)
    # and not `reused` alone (that says nothing changed, and a status did):
    # a quarantine being lifted is a state transition spec section 8 asks
    # the journal to record.
    reactivated_facts: list = field(default_factory=list)

    def sort(self):
        """Called at the end of every applier. Every id list in this
        codebase is sorted, so the journal is byte-comparable across
        re-runs of the same submit."""
        for name in ("created", "reused", "spawned", "cascaded",
                     "rejected_citations", "unverifiable_citations",
                     "reactivated_facts"):
            setattr(self, name, sorted(set(getattr(self, name))))
        self.dropped.sort()
        return self


def canonical(value):
    """A stable string for any JSON value. Used inside natural keys."""
    return json.dumps(value or {}, sort_keys=True, ensure_ascii=False)


# Natural keys. Each one answers "is this the same node, or a new one?"
# `inputs` is in TASK_KEY because two extract tasks under one search task
# differ only by URL; without it they collapse into one and every source
# but the first is silently lost.
def TASK_KEY(node):
    return (node["parent"], node["kind"], node["question"],
            canonical(node.get("inputs")))


def FACT_KEY(node):
    return (node["provenance"]["task"], node["statement"])


def CITATION_KEY(node):
    return (node["url"], node["quote_sha256"])


def ASSUMPTION_KEY(node):
    return (node["raised_by"], node["statement"])


def HYPOTHESIS_KEY(node):
    return (node["provenance"]["task"], node["claim"])


def index_of(memory, node_type, key):
    """{natural key: filename id} over every readable node of a type.

    One pass over the store per applier call rather than a scan per
    lookup: an artifact carries many items and this is O(n) instead of
    O(n * m).

    Mirrors Graph._readable: a node must both parse and satisfy its
    schema to be indexed, and `read`'s KeyError/NodeFormatError is caught
    in a separate try from `validate`'s ValidationError, the same
    separation _readable uses, so a KeyError raised from inside
    jsonschema itself would not be mistaken for a missing file and
    silently swallowed here either.

    A dedup pass must not be the thing that takes submit down, so a
    corrupt file is skipped rather than raised on, and setdefault keeps
    the lowest id when two nodes share a key, so a re-applied artifact
    always resolves to the same node.
    """
    found = {}
    for node_id in memory.ids(node_type):
        try:
            node = memory.read(node_id)
        except (KeyError, nodes.NodeFormatError):
            continue
        try:
            memory.validate(node)
        except memory_mod.ValidationError:
            continue
        found.setdefault(key(node), node_id)
    return found


def create_task(memory, index, *, question, kind, parent, depth, origin_task,
                agent, inputs=None):
    """Create a task, or return the existing one with the same key.

    Returns (task_id, created). The index is mutated so two identical
    items inside one artifact also collapse.
    """
    candidate = {
        "question": question, "kind": kind, "parent": parent,
        "depth": depth, "status": "pending", "depends_on": [], "attempts": 0,
        "inputs": inputs or {},
        "provenance": {"task": origin_task, "agent": agent},
    }
    key = TASK_KEY(candidate)
    if key in index:
        return index[key], False
    created = memory.create("task", candidate)
    index[key] = created["id"]
    return created["id"], True


def apply_decompose(memory, graph, cfg, task_id, task, artifact, **kwargs):
    """Children, sibling dependencies, and the assumptions the decomposer
    had to make."""
    result = ApplyResult()
    max_depth = cfg["config"]["max_depth"]
    child_depth = task["depth"] + 1
    task_index = index_of(memory, "task", TASK_KEY)

    # Positional, so depends_on_index and blocks_index can be resolved.
    # None marks a child that code declined to create.
    child_ids = []
    for child in artifact["children"]:
        if child_depth > max_depth:
            result.dropped.append((
                "task",
                f"depth {child_depth} exceeds cap {max_depth}: "
                f"{child['question']!r}",
            ))
            child_ids.append(None)
            continue
        child_id, created = create_task(
            memory, task_index, question=child["question"],
            kind=child["kind"], parent=task_id, depth=child_depth,
            origin_task=task_id, agent="decomposer",
        )
        child_ids.append(child_id)
        (result.created if created else result.reused).append(child_id)
        result.spawned.append(child_id)

    # The cache was built before these tasks existed, and add_dependency
    # indexes graph.tasks.
    graph.invalidate_cache()

    for child, child_id in zip(artifact["children"], child_ids):
        if child_id is None:
            continue
        for position in child["depends_on_index"]:
            if position >= len(child_ids):
                raise ApplyError(
                    f"depends_on_index {position} is out of range: the "
                    f"artifact has {len(child_ids)} children"
                )
            dep_id = child_ids[position]
            if dep_id is None:
                result.dropped.append((
                    "dependency",
                    f"{child_id} cannot depend on child {position}, which "
                    "was not created",
                ))
                continue
            if dep_id == child_id:
                # Reachable through dedup: two identical children collapse
                # to one id and a dependency between them turns
                # self-referential. Not the model's fault, so it does not
                # cost the task an attempt.
                result.dropped.append((
                    "dependency",
                    f"{child_id} would self-depend after dedup",
                ))
                continue
            try:
                graph.add_dependency(child_id, dep_id)
            except CycleError as error:
                raise ApplyError(f"child dependency rejected: {error}") from None

    assumption_index = index_of(memory, "assumption", ASSUMPTION_KEY)
    for assumption in artifact["assumptions"]:
        blocks = []
        for position in assumption["blocks_index"]:
            if position >= len(child_ids) or child_ids[position] is None:
                result.dropped.append((
                    "blocks",
                    f"blocks_index {position} names no created child",
                ))
                continue
            blocks.append(child_ids[position])
        candidate = {
            "statement": assumption["statement"], "raised_by": task_id,
            "status": "open", "blocks": sorted(set(blocks)),
            "refuted_by": None,
            "provenance": {"task": task_id, "agent": "decomposer"},
        }
        key = ASSUMPTION_KEY(candidate)
        if key in assumption_index:
            result.reused.append(assumption_index[key])
            continue
        created = memory.create("assumption", candidate)
        assumption_index[key] = created["id"]
        result.created.append(created["id"])

    return result.sort()


def apply_search(memory, graph, cfg, task_id, task, artifact, **kwargs):
    """One extract task per source.

    Depth is inherited, not incremented: depth counts decomposition
    levels, and search -> extract is one level's work. Incrementing would
    dead-end the pipeline exactly at the cap, where a search task could
    find sources it could never read.
    """
    result = ApplyResult()
    task_index = index_of(memory, "task", TASK_KEY)

    # What this search actually looked for, recorded on the task so
    # Appendix E can render it. AS REPORTED: nothing here observes the
    # WebSearch call, and the appendix says so. `.get` because the field
    # is required of the artifact but apply_search is also reachable with
    # one written before it existed, and a bare KeyError would take down
    # the tick rather than reject one artifact.
    queries = artifact.get("queries")
    if queries:
        memory.update(task_id, queries=list(queries))

    for source in artifact["sources"]:
        url = source["url"]
        try:
            # The one place a URL becomes an eTLD+1. Gate 3 counts
            # distinct values of it, so computing it here -- once, by the
            # function that owns the public suffix list -- is what makes
            # that count mean anything.
            domain = domains.registrable(url)
        except ValueError as error:
            result.dropped.append(("task", f"unusable source url: {error}"))
            continue
        extract_inputs = {"url": url, "title": source["title"],
                          "domain": domain}
        # A refute search's extractions are counter-evidence, and this is
        # where apply_extract can be told. Carried explicitly rather than
        # left for apply_extract to walk up to this parent: a
        # self-contained task record is the convention everywhere else
        # here, and TASK_KEY hashes `inputs`, so an "against" extract of
        # a URL keys distinctly from a "for" extract of the same URL.
        # Citations still dedup on (url, quote_sha256), so no quote is
        # stored twice.
        #
        # Only set when the search was actually against something. An
        # ordinary search must leave these absent, or every extraction in
        # the run starts writing counter-evidence onto whatever claim was
        # last tagged.
        target = (task.get("inputs") or {}).get("for_hypothesis")
        if target and _stance_of(task) == AGAINST:
            extract_inputs["for_hypothesis"] = target
            extract_inputs["stance"] = AGAINST
        extract_id, created = create_task(
            memory, task_index,
            question=f"Extract facts from {url} bearing on: "
                     f"{task['question']}",
            kind="extract", parent=task_id, depth=task["depth"],
            origin_task=task_id, agent="searcher",
            inputs=extract_inputs,
        )
        (result.created if created else result.reused).append(extract_id)
        result.spawned.append(extract_id)

    graph.invalidate_cache()
    return result.sort()


def _citation_is_gone(memory, citation_id):
    """True if `citation_id` no longer counts as live evidence: gate 2
    rejected it, or its own record is missing, unparseable or
    schema-invalid.

    Read directly, not through index_of/CITATION_KEY: a citation whose
    file has gone schema-invalid (say, `domain` deleted) is skipped by
    index_of, and the next apply_extract mints a brand NEW citation id
    for the same (url, quote_sha256) -- the fact keeps citing the OLD,
    now-orphaned id, which this function must still be able to judge.
    Treating "cannot be read or validated" the same as "rejected" matches
    how live_citations()/`_domains_of` already treat a corrupt citation:
    it cannot vouch for anything, so it counts as gone rather than as
    silently still-good.
    """
    try:
        citation = memory.read(citation_id)
        memory.validate(citation)
    except (KeyError, nodes.NodeFormatError, memory_mod.ValidationError):
        return True
    return citation["status"] == gates.REJECTED


def apply_extract(memory, graph, cfg, task_id, task, artifact, *, root=None,
                  **kwargs):
    """Citations and facts land here. Gate 2 happens on a later tick.

    Gate 2 used to run inline: this applier re-downloaded the page with
    httpx and byte-compared each quote. It is now a `recheck` task
    dispatched to an agent holding WebFetch, because a Python process
    cannot call WebFetch — the same constraint that made gate 4 a task.
    So a citation is born `pending` and a re-check is seeded beside it.

    A `pending` citation counts for nothing: Graph.live_citations admits
    only `verified`, so gate 3 and confidence.compute ignore it until the
    re-check lands. The run therefore under-promotes for a tick, which is
    the direction to fail in.
    """
    result = ApplyResult()
    url = artifact["url"]

    expected = (task.get("inputs") or {}).get("url")
    if expected and expected != url:
        raise ApplyError(
            f"artifact read url {url!r} but this task was given {expected!r}; "
            "the re-check would confirm a page nobody asked about"
        )
    try:
        domain = domains.registrable(url)
    except ValueError as error:
        raise ApplyError(f"cannot record a citation for {url!r}: {error}") from None

    citation_index = index_of(memory, "citation", CITATION_KEY)
    title = (task.get("inputs") or {}).get("title", "")
    # `.get`, unlike the required fields above: the field is required by
    # artifact.extract, but apply_extract is also called directly by
    # tests and by a re-application of an artifact written before it
    # existed, and a bare KeyError there would take down the tick.
    published_at = artifact.get("published_at")
    source_type = artifact.get("source_type")
    citation_for = {}

    for quote in sorted({fact["quote"] for fact in artifact["facts"]}):
        if evidence.meaningful_length(quote) < evidence.MIN_QUOTE_CHARS:
            # schemas/citation.json will not store this, so creating one
            # would raise ValidationError out of memory.create, past
            # ApplyError, and take the whole tick down. Gate 1 refuses the
            # artifact before an applier sees it, so this is unreachable
            # on the real path; it exists because "unreachable" and
            # "cannot crash submit" are different promises.
            result.dropped.append((
                "citation",
                f"{quote!r}: fewer than {evidence.MIN_QUOTE_CHARS} characters "
                "of content, which is a substring of almost any page"))
            continue
        candidate = {
            "url": url, "domain": domain, "title": title, "quote": quote,
            # The NORMALIZED form's hash: see schemas/citation.json. The
            # same sentence quoted with different line wrapping has to
            # resolve to one citation.
            "quote_sha256": evidence.sha256_of(evidence.normalize(quote)),
            "status": "pending", "http_status": None, "fetched_at": None,
            # When the SOURCE says it was published, not when we read it.
            # `fetched_at` is written by apply_recheck, so before this the
            # only date the bibliography could print was our own.
            "published_at": published_at,
            # Recorded, never gated on. See citation.json.
            "source_type": source_type,
            "provenance": {"task": task_id, "agent": "extractor"},
        }
        key = CITATION_KEY(candidate)
        existing_id = citation_index.get(key)
        if existing_id is None:
            created = memory.create("citation", candidate)
            citation_index[key] = created["id"]
            citation_for[quote] = created["id"]
            result.created.append(created["id"])
        else:
            # Already known — possibly already re-checked. Its status is
            # NOT reset to pending: that would discard a verdict and
            # re-queue work already done.
            #
            # The publication date IS filled when absent, and never
            # overwritten. A second extraction of the same page may find
            # a date the first missed, and taking it is a straight gain;
            # replacing one that is already there is not, because the two
            # readings are equally authoritative and a disagreement would
            # otherwise flip-flop on every re-extraction.
            if published_at is not None or source_type is not None:
                known = memory.read(existing_id)
                fill = {}
                if published_at is not None and known.get("published_at") is None:
                    fill["published_at"] = published_at
                if source_type is not None and known.get("source_type") is None:
                    fill["source_type"] = source_type
                if fill:
                    memory.update(existing_id, **fill)
            citation_for[quote] = existing_id
            result.reused.append(existing_id)

    fact_index = index_of(memory, "fact", FACT_KEY)
    for fact in artifact["facts"]:
        citation_id = citation_for.get(fact["quote"])
        if citation_id is None:
            result.dropped.append((
                "fact",
                f"{fact['statement']!r} rests only on a quote too short to "
                "store as a citation"))
            continue
        key = FACT_KEY({"provenance": {"task": task_id, "agent": "extractor"},
                        "statement": fact["statement"]})
        if key in fact_index:
            result.reused.append(fact_index[key])
            continue
        created = memory.create("fact", {
            "statement": fact["statement"], "citations": [citation_id],
            "status": "active",
            "provenance": {"task": task_id, "agent": "extractor"},
        })
        fact_index[key] = created["id"]
        result.created.append(created["id"])

    _attach_counter_evidence(memory, graph, task, citation_for, result)
    _seed_recheck(memory, task_id, task, url, citation_for, result)
    return result.sort()


def _attach_counter_evidence(memory, graph, task, citation_for, result):
    """Add this extraction's citations to the claim it was gathered against.

    The second writer of `counter`. The hypothesizer was the only one,
    and it writes at creation from facts it was shown; this writes to a
    claim that already exists, from evidence a refute search went out and
    found specifically to break it.

    A direct path exists because there is no indirect one. Nothing else
    attaches a citation to an EXISTING hypothesis: apply_hypothesize
    appends to result.reused and leaves the node untouched, and
    HYPOTHESIS_KEY is (provenance.task, claim) while
    ensure_hypothesize_tasks deliberately varies its question by fact
    count — so a re-proposed identical claim writes a DUPLICATE node
    rather than strengthening the original. That is a real pre-existing
    defect, it is not fixed here, and it leaves an asymmetry worth
    knowing about: the confirmatory side duplicates, this side attaches.

    Never both sides. apply_hypothesize rejects an artifact that puts one
    id in `supporting` and `counter` as incoherent, and apply_verify
    re-checks at the point of use because a node already on disk can
    carry the overlap. One quote arguing both ways is not a dispute, it
    is a contradiction in the record — and _verified_status would then
    see live counter-evidence that is also the claim's own support.
    """
    inputs = task.get("inputs") or {}
    target = inputs.get("for_hypothesis")
    if not target or _stance_of(task) != AGAINST:
        return
    try:
        hypothesis = memory.read(target)
        memory.validate(hypothesis)
    except (KeyError, nodes.NodeFormatError, memory_mod.ValidationError):
        # Dangling or corrupt: fsck reports it. Losing the attachment is
        # a lost challenge, not a crashed tick.
        result.dropped.append((
            "hypothesis",
            f"{target!r} was named as the target of a refute search but is "
            "missing, unparseable or schema-invalid; its counter-evidence "
            "could not be attached"))
        return

    supporting = set(hypothesis["supporting"])
    counter = set(hypothesis["counter"])
    added = False
    for citation_id in sorted(set(citation_for.values())):
        if citation_id in supporting:
            result.dropped.append((
                "citation",
                f"{citation_id} already supports {target}; a citation "
                "cannot argue both for and against the same claim"))
            continue
        if citation_id not in counter:
            counter.add(citation_id)
            added = True
    if not added:
        return
    memory.update(target, counter=sorted(counter))

    # Re-open the adversarial question. Gate 4 weighs counter-evidence
    # (the verify packet labels every quote), but the verifier only ever
    # saw a claim once — when the hypothesizer proposed it. So a refute
    # search could gather fifteen contradicting sources and the one agent
    # able to act on them was never dispatched again. There was an
    # incidental route, via a hypothesize round re-proposing the claim
    # and merging, but it depended on the hypothesizer choosing to
    # restate that particular claim.
    #
    # Only when no verification is already open for it: each is a real
    # subagent call, and a page yielding five quotes would otherwise seed
    # five. `refutes` is None because that proposal belongs to the
    # hypothesizer that made it — inventing one here fires an assumption
    # cascade nobody asked for.
    for _, task in graph.readable("task"):
        if (task["kind"] == "verify"
                and (task.get("inputs") or {}).get("hypothesis") == target
                and task["status"] in OPEN_TASK_STATUSES):
            return
    parent = hypothesis["provenance"]["task"]
    if parent is None or parent not in graph.valid_task_ids():
        result.dropped.append((
            "task",
            f"counter-evidence attached to {target} but its provenance task "
            f"{parent!r} is missing or malformed, so no fresh verification "
            "could be scheduled"))
        return
    # The counter count is in the question, the same trick
    # ensure_hypothesize_tasks uses with its fact count and for the same
    # reason: TASK_KEY is (parent, kind, question, inputs), and without
    # it this resolves to the verify task apply_hypothesize already
    # created — same parent, same claim, same inputs — so `create_task`
    # reused the finished one and no re-adjudication ever happened.
    #
    # It also gives the dedup the right shape by itself: one
    # verification per distinct evidential state, and a re-extraction
    # that adds no new counter does not ask again.
    verify_id, created = create_task(
        memory, index_of(memory, "task", TASK_KEY),
        question=f"Adversarially verify against {len(counter)} "
                 f"counter-citation(s): {hypothesis['claim']}",
        kind="verify", parent=parent, depth=graph.tasks[parent]["depth"],
        origin_task=parent, agent="scheduler",
        inputs={"hypothesis": target, "refutes": None},
    )
    (result.created if created else result.reused).append(verify_id)
    result.spawned.append(verify_id)


def _seed_recheck(memory, task_id, task, url, citation_for, result):
    """One re-check task for this page, carrying its quotes in index order.

    `depth=task["depth"]`, NOT depth + 1. Graph.over_cap refuses anything
    past max_depth, and an extract task already sits two or three levels
    down — a deeper re-check would be undispatchable, its citations would
    stay `pending` for ever, and nothing they support could ever be
    promoted. apply_hypothesize creates its verify tasks at the parent's
    own depth for exactly this reason.

    `quotes` and `citations` are positional partners: the artifact reports
    verdicts by index, so quotes[i] is the span whose verdict updates
    citations[i]. Sorted so TASK_KEY — which hashes `inputs` — is stable
    and re-applying the same artifact reuses this task instead of seeding
    a second one.
    """
    ordered = sorted(citation_for)
    if not ordered:
        return
    index = index_of(memory, "task", TASK_KEY)
    recheck_id, created = create_task(
        memory, index,
        question=f"re-read {url} and confirm {len(ordered)} quoted span(s)",
        kind="recheck", parent=task_id, depth=task["depth"],
        origin_task=task_id, agent="extractor",
        inputs={"url": url,
                "quotes": ordered,
                "citations": [citation_for[quote] for quote in ordered]},
    )
    (result.created if created else result.reused).append(recheck_id)
    result.spawned.append(recheck_id)


# What a re-check outcome does to every citation on the page, when the
# outcome is not `read` and there are no per-quote verdicts to apply.
_WHOLE_PAGE_STATUS = {
    # Spec section 6: a login wall or a JS wall is "flagged rather than
    # silently trusted". It is the ABSENCE of verification, not disproof,
    # so the citation survives and is disclosed in Appendix D.
    "blocked": gates.UNVERIFIABLE,
    # The page is gone. Spec section 9 lists a 404 as a case a citation
    # must fail.
    "gone": gates.REJECTED,
}


def apply_recheck(memory, graph, cfg, task_id, task, artifact, **kwargs):
    """Gate 2. Turn one rechecker's verdicts into citation statuses.

    This is the applier for the check that used to run inline in
    apply_extract over an httpx re-download. A Python process cannot call
    WebFetch, so the re-read happens in an agent and lands here as an
    artifact.

    Verdicts arrive BY INDEX into the task's frozen `quotes` list, never
    as echoed text: a model asked to repeat a long span will eventually
    retype it slightly wrong, and a mangled echo is indistinguishable from
    a genuine absence.
    """
    inputs = task.get("inputs") or {}
    url = inputs.get("url")
    quotes = inputs.get("quotes") or []
    citation_ids = inputs.get("citations") or []
    if not url or not quotes:
        raise ApplyError(
            f"{task_id} is a recheck task with no url or quotes in its "
            "inputs; there is nothing to confirm"
        )
    if len(quotes) != len(citation_ids):
        raise ApplyError(
            f"{task_id} carries {len(quotes)} quote(s) and "
            f"{len(citation_ids)} citation id(s); they are positional "
            "partners and a mismatch would apply a verdict to the wrong "
            "citation"
        )
    if artifact["url"] != url:
        raise ApplyError(
            f"this artifact re-read {artifact['url']!r} but {task_id} was "
            f"given {url!r}; a verdict from a different page cannot stand "
            "for these citations"
        )

    outcome = artifact["outcome"]
    positions = range(len(quotes))
    if outcome == "read":
        # Duplicates FIRST, before the dict comprehension below, because
        # that comprehension silently keeps the last value for a repeated
        # index: [{0: true}, {0: false}] collapses to {0: False} and the
        # citation is rejected with no error raised. The schema cannot
        # catch this — `uniqueItems` compares whole objects, and those two
        # entries genuinely differ — so a rechecker that contradicted
        # itself would have its last word applied as if it were its only
        # word. Verified: the comprehension alone yields {0: False}.
        seen_indices = [entry["index"] for entry in artifact["quotes"]]
        repeated = sorted({index for index in seen_indices
                           if seen_indices.count(index) > 1})
        if repeated:
            raise ApplyError(
                f"quote index {repeated} judged more than once; a re-check "
                "that contradicts itself cannot be applied"
            )
        verdicts = {entry["index"]: entry["present"]
                    for entry in artifact["quotes"]}
        missing = sorted(set(positions) - set(verdicts))
        if missing:
            raise ApplyError(
                f"no verdict for quote index {missing}; every quote needs "
                "one, so a rechecker cannot pass over the span it could "
                "not find"
            )
        unknown = sorted(set(verdicts) - set(positions))
        if unknown:
            raise ApplyError(
                f"verdict for quote index {unknown}, but this task carries "
                f"only {len(quotes)}"
            )
        status_for = {
            position: gates.VERIFIED if verdicts[position] else gates.REJECTED
            for position in positions
        }
    else:
        status_for = {position: _WHOLE_PAGE_STATUS[outcome]
                      for position in positions}

    result = ApplyResult()
    now = memory_mod.utcnow()
    # Ids whose write did NOT land -- memory.update raised instead of
    # committing. _settle_facts must judge facts against what is actually
    # on disk, not against status_for's INTENDED verdict: without this,
    # a citation dropped as unreadable still counted as `verified` (or
    # `rejected`) below even though its own file never changed, and a
    # quarantined fact citing it was reactivated on a write that never
    # happened -- measured: citation left `pending` on disk, its fact
    # moved to `active` anyway.
    dropped_ids = set()
    for position in positions:
        citation_id = citation_ids[position]
        status = status_for[position]
        try:
            memory.update(citation_id, status=status, fetched_at=now)
        except (KeyError, nodes.NodeFormatError, memory_mod.ValidationError):
            # Three-wide, matching every other read-then-write guard in
            # this module: KeyError is the citation deleted between the
            # extract and the re-check; NodeFormatError is a file that no
            # longer parses at all; ValidationError is memory.update's own
            # re-validation of the merged record failing because the
            # stored file has gone schema-invalid (say, a hand edit, or
            # a field some other bug corrupted). memory.update reads
            # before it writes, so any of the three raises past this line
            # and out of apply_recheck as an uncaught exception -- not an
            # ApplyError, so submit's per-artifact guard does not catch
            # it, and it takes down the whole tick, identically on every
            # retry, because the file on disk never changes. fsck reports
            # a corrupt node; a gate must not be the thing that dies on
            # one.
            result.dropped.append((
                "citation",
                f"{citation_id} no longer exists or is unreadable"))
            dropped_ids.add(citation_id)
            continue
        if status == gates.REJECTED:
            result.rejected_citations.append(citation_id)
        elif status == gates.UNVERIFIABLE:
            result.unverifiable_citations.append(citation_id)

    _settle_facts(memory, status_for, citation_ids, result, dropped_ids)
    return result.sort()


def _settle_facts(memory, status_for, citation_ids, result, dropped_ids):
    """Both directions of the fact lifecycle, after a re-check verdict.

    Quarantine a fact whose every citation is now gone; reactivate a
    quarantined fact whose citation this pass verified. The second half is
    not optional: the invalidation cascade stales a branch's extract tasks
    precisely so the work is redone, and without reactivation redoing it
    re-verifies the citation and leaves the fact quarantined anyway.
    Measured once, before this existed: 12 staled tasks, zero active facts.

    `dropped_ids` excludes any citation whose write in apply_recheck's own
    loop did not land. Built from status_for/citation_ids alone, `verified`
    and `rejected` would answer "what this pass MEANT to write", not "what
    is true on disk" -- and a fact must only move on the second.
    """
    verified = {citation_ids[i] for i, status in status_for.items()
                if status == gates.VERIFIED and citation_ids[i] not in dropped_ids}
    rejected = {citation_ids[i] for i, status in status_for.items()
                if status == gates.REJECTED and citation_ids[i] not in dropped_ids}

    for fact_id in memory.ids("fact"):
        try:
            fact = memory.read(fact_id)
            memory.validate(fact)
        except (KeyError, nodes.NodeFormatError, memory_mod.ValidationError):
            continue
        cites = set(fact["citations"])
        if fact["status"] == "active" and cites & rejected:
            # Spec section 6 rejects a fact resting SOLELY on a failed
            # citation. _citation_is_gone reads each one directly, so a
            # fact still standing on other good evidence survives.
            if all(_citation_is_gone(memory, c) for c in fact["citations"]):
                memory.update(fact_id, status="quarantined")
                result.dropped.append((
                    "fact",
                    f"{fact_id} rested only on citations the re-check "
                    "rejected; quarantined"))
        elif fact["status"] == "quarantined" and cites & verified:
            # VERIFIED only. An `unverifiable` re-check is the absence of
            # verification, and reactivating on one would let a site that
            # merely started rate-limiting undo a quarantine.
            memory.update(fact_id, status="active")
            result.reactivated_facts.append(fact_id)


def ensure_hypothesize_tasks(memory, graph, cfg):
    """One open hypothesize task per branch that has enough evidence.

    Scheduling policy, computed by code from the graph — spec section 4:
    "The model is never the scheduler." A branch qualifies when it holds
    at least min_citations active facts and has no open hypothesize task
    already. Called once per submit, after every artifact is applied.

    A branch whose hypothesizer already ran becomes eligible again, which
    is what lets a multi-day run form new claims as evidence accumulates.
    """
    result = ApplyResult()
    minimum = cfg["config"]["min_citations"]

    def branch_of(task_id):
        if task_id is None:
            return None
        try:
            return graph.root_branch(task_id)
        except CycleError:
            return None  # fsck reports it

    facts_by_branch = {}
    for _, fact in graph.readable("fact"):
        if fact["status"] != "active":
            continue
        branch = branch_of(fact["provenance"]["task"])
        if branch is not None:
            facts_by_branch[branch] = facts_by_branch.get(branch, 0) + 1

    busy = set()
    valid = graph.valid_task_ids()
    for task_id, task in graph.tasks.items():
        if task_id not in valid:
            # graph.tasks keeps every task that merely parses; `kind` and
            # `status` are both required fields, unsafe to index on an
            # entry that only parsed. Scanned unconditionally below, so a
            # malformed task anywhere in the store -- not only a branch
            # root -- must not crash this loop before it even gets there.
            continue
        if task["kind"] == "hypothesize" and task["status"] in OPEN_TASK_STATUSES:
            branch = branch_of(task_id)
            if branch is not None:
                busy.add(branch)

    task_index = index_of(memory, "task", TASK_KEY)
    for branch in sorted(facts_by_branch):
        if facts_by_branch[branch] < minimum or branch in busy:
            continue
        if branch not in graph.valid_task_ids():
            # graph.tasks keeps every task that merely parses, valid or
            # not (see its own docstring); a branch root missing a
            # required field like `depth` has no safe value to index
            # below. fsck reports the malformed file separately.
            result.dropped.append((
                "task",
                f"branch {branch} has enough evidence but its own task "
                "record is malformed; cannot schedule a hypothesizer for it",
            ))
            continue
        root = graph.tasks[branch]
        # The count is in the question so two rounds on one branch are
        # different natural keys, and a human reading the task file can
        # see why it was scheduled.
        new_id, created = create_task(
            memory, task_index,
            question=f"Form candidate claims from the "
                     f"{facts_by_branch[branch]} facts gathered under: "
                     f"{root['question']}",
            kind="hypothesize", parent=branch, depth=root["depth"],
            origin_task=branch, agent="scheduler",
        )
        (result.created if created else result.reused).append(new_id)
        result.spawned.append(new_id)

    graph.invalidate_cache()
    return result.sort()


def _claim_index(memory, graph):
    """{claim: hypothesis id} over every readable node.

    The lookup apply_hypothesize needs, and one HYPOTHESIS_KEY cannot
    express: that key is `(provenance.task, claim)`, and the candidate
    carries the CURRENT round's task id while ensure_hypothesize_tasks
    varies each round's question by fact count, so it never matches
    across rounds.

    Keyed on the claim alone. This was `(theme, claim)`, to stop two
    themes' identical sentences colliding — and it never fired at all on
    a real run: ensure_hypothesize_tasks parents every round on
    `branch_of(...)` -> `Graph.root_branch`, "on a real run a constant
    function", so each round is a depth-0 child of the root, `theme_of`
    resolves it to ITSELF, and no two rounds ever shared a key. Measured
    on a driven run: nine nodes for one claim, and nine chapters named
    after the rounds that made them.

    There is no per-theme hypothesizing for a theme key to protect.
    `scheduler.agent_input` hands the hypothesizer every active fact in
    the run — `_branch_of` resolves through the same constant function —
    and ensure_hypothesize_tasks schedules one round at a time run-wide.
    One lineage, one claim namespace: the same sentence is the same
    finding wherever its evidence came from. `outline` now files a claim
    by its evidence rather than by the round that proposed it, so a node
    drawing on two themes lands in the dominant one instead of being
    impossible to place.

    If per-theme hypothesizing is ever introduced, this key has to grow
    back a theme with it.
    """
    index = {}
    for hypothesis_id, hypothesis in graph.readable("hypothesis"):
        index.setdefault(hypothesis["claim"], hypothesis_id)
    return index


def apply_hypothesize(memory, graph, cfg, task_id, task, artifact, **kwargs):
    """Candidate claims, and one verify task each — which is gate 4."""
    result = ApplyResult()
    known_citations = set(memory.ids("citation"))
    known_assumptions = set(memory.ids("assumption"))

    # NOT index_of(..., HYPOTHESIS_KEY). That key is
    # (provenance.task, claim), and the candidate below carries THIS
    # round's task id while ensure_hypothesize_tasks deliberately varies
    # each round's question by fact count so two rounds are different
    # task keys. So the key could never match an earlier node and the
    # reuse branch below was unreachable across rounds: every round
    # forked the claim. Measured at three rounds — 3 hypothesis nodes, 3
    # verify tasks and 3 refute searches for one claim, with the earliest
    # left permanently under-evidenced while a duplicate carried the
    # fuller evidence, and all of them reaching the report.
    #
    # Keyed by theme, not by claim alone: two themes can reach the
    # same sentence about different questions, and outline assigns a
    # section from provenance — one node in two sections is exactly what
    # outline.validate forbids.
    hypothesis_index = _claim_index(memory, graph)
    task_index = index_of(memory, "task", TASK_KEY)

    for item in artifact["hypotheses"]:
        referenced = sorted(set(item["supporting"]) | set(item["counter"]))
        missing = [c for c in referenced if c not in known_citations]
        if missing:
            # Gate 1 checks that a citation id looks like one; only the
            # graph knows whether it exists. This is gate 5's
            # allowed-keys rule applied at the point of entry.
            raise ApplyError(
                "hypothesis cites citations that do not exist: "
                + ", ".join(missing)
            )
        overlap = sorted(set(item["supporting"]) & set(item["counter"]))
        if overlap:
            # JSON Schema cannot express cross-field disjointness, so
            # gate 1 lets this through; only code can catch it. A
            # citation on both sides is incoherent output, and leaving it
            # in `supporting` would also reopen finding 1's attack: it is
            # `in supporting`, so apply_verify's failing_citations guard
            # would permit rejecting it, and rejecting it drops it from
            # live_citations too -- killing it as counter-evidence and
            # promoting a hypothesis the graph still records a dispute
            # against.
            raise ApplyError(
                "hypothesis lists the same citation as both supporting "
                "and counter, which is incoherent: " + ", ".join(overlap)
            )
        if item["refutes"] and item["refutes"] not in known_assumptions:
            raise ApplyError(
                f"hypothesis claims to refute {item['refutes']}, which does "
                "not exist"
            )

        candidate = {
            "claim": item["claim"],
            "supporting": sorted(set(item["supporting"])),
            "counter": sorted(set(item["counter"])),
            # No model sets a confidence or a status. Both are derived,
            # and recompute_confidence runs at the end of the tick.
            "status": "proposed", "confidence": 0.0, "verdict": None,
            "provenance": {"task": task_id, "agent": "hypothesizer"},
        }
        key = item["claim"]
        if key in hypothesis_index:
            hypothesis_id = hypothesis_index[key]
            existing = memory.read(hypothesis_id)
            # Union, not "latest wins". A round can legitimately cite a
            # subset of what is known — the hypothesizer's packet is
            # capped and it argues from what it was shown — and
            # overwriting would undo evidence already gathered. It would
            # also erase counter citations that
            # _attach_counter_evidence wrote directly, discarding a
            # refutation the run paid a full search -> extract ->
            # recheck cycle to find.
            supporting = sorted(set(existing["supporting"])
                                | set(candidate["supporting"]))
            counter = sorted(set(existing["counter"])
                             | set(candidate["counter"]))
            # Re-checked across the MERGED sets. The guard above runs on
            # the artifact item alone, and two internally coherent rounds
            # can still union into a citation on both sides — which makes
            # _verified_status see live counter-evidence that is also the
            # claim's own support.
            merged_overlap = sorted(set(supporting) & set(counter))
            if merged_overlap:
                raise ApplyError(
                    "merging this claim into " + hypothesis_id + " would "
                    "list the same citation as both supporting and counter, "
                    "which is incoherent: " + ", ".join(merged_overlap)
                )
            # status/confidence/verdict/provenance are deliberately NOT
            # written. Merging must not promote — recompute_confidence is
            # demote-only by design (see _verified_status) — and the
            # node records who FIRST proposed the claim, because outline
            # derives its section from that provenance.
            if (supporting != existing["supporting"]
                    or counter != existing["counter"]):
                memory.update(hypothesis_id, supporting=supporting,
                              counter=counter)
            result.reused.append(hypothesis_id)
        else:
            created = memory.create("hypothesis", candidate)
            hypothesis_id = created["id"]
            hypothesis_index[key] = hypothesis_id
            result.created.append(hypothesis_id)

        # Gate 4. Spawned rather than called: the harness owns the model
        # call, so submit cannot dispatch a verifier itself. The refutes
        # proposal rides along because it has to survive until the
        # verdict comes back, and this is the only place it can.
        verify_id, created = create_task(
            memory, task_index,
            question=f"Adversarially verify: {item['claim']}",
            kind="verify", parent=task_id, depth=task["depth"],
            origin_task=task_id, agent="scheduler",
            inputs={"hypothesis": hypothesis_id, "refutes": item["refutes"]},
        )
        (result.created if created else result.reused).append(verify_id)
        result.spawned.append(verify_id)

    graph.invalidate_cache()
    return result.sort()


def _verified_status(memory, graph, cfg, hypothesis_id, verdict):
    """The status a verdict earns. The only place `supported` is written.

    Promotion needs three things and this is the one moment all three are
    known: an adversarial verdict of `supported`, a score at or above the
    promotion threshold, and no live counter-evidence.

    Not folded into Graph.recompute_confidence, though that is where a
    derived status naturally belongs. Measured: making recompute promote
    breaks
    test_a_hypothesis_authored_in_the_pruned_branch_is_demoted_by_provenance
    — the cascade demotes a hypothesis whose authoring reasoning is now
    unsound, and a rescore immediately re-promotes it on evidence the
    cascade never disputed. graph.py's own comment says as much. So
    recompute stays demote-only and promotion happens once, here, where a
    verdict arrives.
    """
    if verdict == "contradicted":
        return "refuted"
    if verdict != "supported":
        return "proposed"
    domains_seen = graph.supporting_domains(hypothesis_id)
    live = graph.live_citations()
    hypothesis = memory.read(hypothesis_id)
    # Counter count passed here too, and that is not optional: this
    # function and Graph.recompute_confidence must produce the same
    # number for the same node. The comment below records what happened
    # last time they disagreed. Without it, a claim with live opposition
    # would be promoted here and demoted again by the recompute at the
    # end of the same submit.
    against = sum(1 for c in hypothesis["counter"] if c in live)
    score = confidence_mod.compute(
        domains_seen, verdict, required_domains=graph.required_domains,
        counter=against, min_citations=graph.min_citations)
    # graph.promotion_threshold and graph.required_domains, not
    # cfg["config"][...]: Graph.recompute_confidence reads the former pair,
    # runs at the end of the same submit, and its value is the one that
    # persists on the node. A caller that constructs a Graph with either
    # threshold set differently from cfg must not have this function
    # silently disagree with it — measured at required_domains 3, that
    # disagreement scored 0.4 here and wrote 0.6 there.
    if score < graph.promotion_threshold:
        return "proposed"
    if against:
        # Spec section 4's digest counts these separately from supported.
        # Live counter-evidence is a real dispute, not noise. Reaching
        # here now means the support was strong enough to absorb the
        # opposition in the score and still clear the bar, which is a
        # sharper reading of `contested` than it used to carry.
        return "contested"
    return "supported"


# A search task's `inputs.stance`. FOR is the default and is never
# written: every search task on disk predates this field, and
# scheduler.agent_input defaults a missing value, so absent means "for"
# exactly as absent `cascaded` means false in schemas/assumption.json.
FOR = "for"
AGAINST = "against"


def _stance_of(task):
    return ((task.get("inputs") or {}).get("stance")) or FOR


def _open_for_hypothesis(graph, stance):
    """Hypotheses with an open search task of `stance` already on them.

    Split by stance, and that is load-bearing rather than tidy.
    ensure_evidence_tasks used to skip a hypothesis carrying ANY open
    task tagged `for_hypothesis`, and both kinds of search carry that
    tag — so whichever opened first suppressed the other for the rest of
    the run. A promoted claim whose citation is rejected by a late gate-2
    re-check needs both at once: more evidence for the gap, and the
    challenge it has not yet faced.
    """
    found = set()
    valid = graph.valid_task_ids()
    for task_id, task in graph.tasks.items():
        if task_id not in valid:
            # Same shape as ensure_hypothesize_tasks's busy loop: `status`
            # is a required field, unsafe to index on a task that merely
            # parses. `inputs` is already read with `.get`, but `status`
            # was not.
            continue
        target = (task.get("inputs") or {}).get("for_hypothesis")
        if (target and task["status"] in OPEN_TASK_STATUSES
                and _stance_of(task) == stance):
            found.add(target)
    return found


# A claim the report will stand behind. `outline.BODY_HYPOTHESIS_STATUSES`
# also admits `proposed`, deliberately not mirrored here: an unpromoted
# claim is reported as open rather than as a finding, and challenging one
# costs a full search -> extract -> recheck cycle per claim.
PROMOTED_STATUSES = ("supported", "contested")


def ensure_refute_tasks(memory, graph, cfg):
    """One open search task per promoted claim, looking for the opposite.

    The gap this closes: `ensure_evidence_tasks` below is the only other
    hypothesis-driven search in the system, it fires only when gate 3
    FAILS, and it asks for MORE SUPPORT. Nothing had ever gone looking
    for evidence against a claim, so `supported` meant "three quotes
    nobody sought the contrary of". Gate 4 adjudicates counter-evidence
    now, but it could only ever weigh what turned up by accident.

    The question is written here, not by a model: spec section 4's "the
    model is never the scheduler" covers which claim is attacked and the
    fact that it is being attacked, both of which are scheduling.

    Deliberately NOT scoped to hypotheses that cleared gate 3 — scoped to
    the ones that got promoted. They are the same set most of the time,
    and where they differ (a re-check rejecting a citation after
    promotion) the promoted status is what the report acts on.
    """
    result = ApplyResult()
    task_index = index_of(memory, "task", TASK_KEY)
    open_for = _open_for_hypothesis(graph, AGAINST)

    for hypothesis_id, hypothesis in graph.readable("hypothesis"):
        if hypothesis["status"] not in PROMOTED_STATUSES:
            continue
        if hypothesis_id in open_for:
            continue
        parent = hypothesis["provenance"]["task"]
        if parent is None or parent not in graph.valid_task_ids():
            # Same guard, and the same reason, as ensure_evidence_tasks:
            # graph.tasks keeps every task that merely parses, so a task
            # missing `depth` would pass a bare membership check and then
            # raise indexing it below.
            result.dropped.append((
                "task",
                f"{hypothesis_id} is promoted and unchallenged but its "
                f"provenance task {parent!r} is not in the graph or is "
                "malformed",
            ))
            continue
        new_id, created = create_task(
            memory, task_index,
            question=f"Find evidence that would show this claim is false: "
                     f"{hypothesis['claim']}",
            kind="search", parent=parent, depth=graph.tasks[parent]["depth"],
            origin_task=parent, agent="scheduler",
            inputs={"for_hypothesis": hypothesis_id, "stance": AGAINST},
        )
        (result.created if created else result.reused).append(new_id)
        result.spawned.append(new_id)

    graph.invalidate_cache()
    return result.sort()


def ensure_evidence_tasks(memory, graph, cfg):
    """Gate 3's failure action: spec section 6, "spawn tasks seeking
    other domains".

    One open search task per non-refuted hypothesis that does not clear
    gate 3. The gap string goes into the question, so the searcher is
    told whether it needs more citations or a different site.

    This is also what makes the coverage halt predicate reachable: it
    requires every hypothesis to meet the evidence bar, and without this
    nothing would go looking.
    """
    result = ApplyResult()
    task_index = index_of(memory, "task", TASK_KEY)
    # FOR, not AGAINST: this function's own outstanding work is the
    # confirmatory search it spawns, which carries no stance and so reads
    # as `for`. Reading the AGAINST set here would make a refute search
    # suppress the evidence search and vice versa.
    open_for = _open_for_hypothesis(graph, FOR)

    for hypothesis_id, hypothesis in graph.readable("hypothesis"):
        if hypothesis["status"] == "refuted" or hypothesis_id in open_for:
            continue
        gap = gates.evidence_gap(graph, cfg, hypothesis_id)
        if gap is None:
            continue
        # The gap may be about to close on its own. Gate 3 counts only
        # `verified` citations, so a hypothesis whose evidence is still
        # sitting in an unapplied re-check looks starved when it is merely
        # unchecked. Spawning a search for it means a redundant dispatch
        # after every extraction, and the searcher is told to avoid the
        # domains we already have — so it goes looking for a fourth source
        # while the third is still being confirmed.
        #
        # LIVE re-checks only, not "has a pending citation". That wider
        # test livelocked the run: a `recheck` abandoned after three
        # attempts leaves its citations `pending` for ever, `abandoned` is
        # not in OPEN_TASK_STATUSES so nothing was dispatchable, and this
        # veto then suppressed the only thing that could still make work —
        # empty frontier, no halt, `research next` printing "nothing to
        # dispatch" forever. See Graph.live_rechecks_for; halt.
        # evidence_exhausted asks the same query so the two cannot drift.
        if graph.live_rechecks_for(hypothesis_id):
            continue
        # NOT deferred to a hypothesize round when unused evidence is
        # sitting in the store, though a round is far cheaper than this
        # search (one tool-less agent call against a WebSearch, one
        # extract per source and one recheck per page). Tried and
        # reverted: no bounded version of the condition exists without
        # new per-claim state.
        #
        # ensure_hypothesize_tasks opens a fresh round after nearly every
        # submit, so "a round is open" is almost always true; and a fact
        # the hypothesizer judged irrelevant stays unused for ever, so
        # "unused evidence exists" never becomes false either. Together
        # they defer the search permanently — the claim never gets its
        # evidence, the frontier empties, and coverage refuses to halt
        # because the claim is under-evidenced with work still notionally
        # possible. That is the same livelock this file and halt.py have
        # each already been bitten by once.
        #
        # The useful half of "look in the store first" is delivered
        # anyway: claims merge rather than fork, so a round attaches
        # existing facts to an existing claim, and the packet now spends
        # its cap on facts nothing has used.
        parent = hypothesis["provenance"]["task"]
        if parent is None or parent not in graph.valid_task_ids():
            # `parent not in graph.tasks` alone is not enough: graph.tasks
            # keeps every task that merely parses, so a task missing
            # `depth` would pass that check and then raise indexing it
            # below. valid_task_ids() is the schema-valid subset.
            result.dropped.append((
                "task",
                f"{hypothesis_id} needs evidence but its provenance task "
                f"{parent!r} is not in the graph or is malformed",
            ))
            continue
        new_id, created = create_task(
            memory, task_index,
            question=f"Find further evidence bearing on: "
                     f"{hypothesis['claim']} — {gap}",
            kind="search", parent=parent, depth=graph.tasks[parent]["depth"],
            origin_task=parent, agent="scheduler",
            inputs={"for_hypothesis": hypothesis_id},
        )
        (result.created if created else result.reused).append(new_id)
        result.spawned.append(new_id)

    graph.invalidate_cache()
    return result.sort()


def apply_verify(memory, graph, cfg, task_id, task, artifact, **kwargs):
    """Spec section 6's verdict-to-graph transition. Gate 4 landing."""
    result = ApplyResult()
    inputs = task.get("inputs") or {}
    hypothesis_id = inputs.get("hypothesis")
    if not hypothesis_id:
        raise ApplyError(
            f"{task_id} is a verify task with no hypothesis in its inputs; "
            "there is nothing to apply a verdict to"
        )
    if artifact["hypothesis"] != hypothesis_id:
        raise ApplyError(
            f"artifact returns a verdict on {artifact['hypothesis']} but this "
            f"task was dispatched for {hypothesis_id}"
        )
    try:
        hypothesis = memory.read(hypothesis_id)
    except (KeyError, nodes.NodeFormatError):
        raise ApplyError(f"{hypothesis_id} no longer exists") from None

    supporting = set(hypothesis["supporting"])
    attached = supporting | set(hypothesis["counter"])
    stranger = sorted(set(artifact["failing_citations"]) - attached)
    if stranger:
        # The verifier's input packet is the claim and its quotes, nothing
        # else (spec section 5). Any other id came from somewhere it could
        # not have seen.
        raise ApplyError(
            "verifier failed citations it was never given: "
            + ", ".join(stranger)
        )
    for citation_id in sorted(artifact["failing_citations"]):
        if citation_id not in supporting or citation_id in hypothesis["counter"]:
            # A counter citation is evidence AGAINST the claim; the
            # verifier's job is to judge the claim, not erase opposition.
            # Checked here, at the point of use, not only at
            # apply_hypothesize's entry: that guard stops a NEW hypothesis
            # from listing one citation on both sides, but does nothing
            # for a hypothesis already on disk with `supporting` and
            # `counter` overlapping (e.g. written before that guard
            # existed). `citation_id in hypothesis["counter"]` catches
            # that case even when the same id is also, incoherently,
            # `in supporting` -- without it, rejecting such an id drops
            # it from live_citations too, erasing it as counter-evidence
            # and promoting a hypothesis the graph still records a
            # dispute against. Not a stranger (that raised above): the id
            # is genuinely attached, just on the wrong side, so this does
            # not cost the task an attempt -- it is simply not
            # actionable, and _verified_status's live-counter check still
            # sees it.
            result.dropped.append((
                "citation",
                f"{citation_id} is counter-evidence, not supporting; a "
                "verifier cannot reject the opposition it disagrees with",
            ))
            continue
        try:
            citation = memory.read(citation_id)
        except (KeyError, nodes.NodeFormatError):
            # A citation id is only ever shape-checked by the schema
            # (graph.py's _domains_of docstring): a fact citing a
            # citation whose write failed is a fully schema-valid store,
            # no disk corruption required. The verifier can be handed the
            # same dangling -- or, same convention as index_of and
            # Graph._readable, unparseable -- id; rejecting a citation
            # that never landed cleanly must not crash the verdict it
            # arrived with.
            result.dropped.append((
                "citation",
                f"{citation_id} was named as failing but does not exist "
                "or does not parse; nothing to reject",
            ))
            continue
        if citation["status"] != gates.REJECTED:
            memory.update(citation_id, status=gates.REJECTED)
            result.rejected_citations.append(citation_id)

    verdict = artifact["verdict"]
    target = _verified_status(memory, graph, cfg, hypothesis_id, verdict)
    # `reasoning` is required by artifact.verify and gate 1 has already
    # run, so it is present and non-empty here -- no defensive .get.
    #
    # In the guard as well as the update, and that is not redundant: a
    # re-verification can reach the SAME verdict by a different argument,
    # and on the two-field guard that write was skipped. The node then
    # kept the first pass's prose while the journal recorded the second
    # artifact, and Appendix A quoted an argument the verifier no longer
    # made. `.get` on this one, unlike the artifact: a hypothesis written
    # before this field existed has no key.
    reasoning = artifact["reasoning"]
    if (hypothesis["verdict"] != verdict or hypothesis["status"] != target
            or hypothesis.get("verdict_reasoning") != reasoning):
        memory.update(hypothesis_id, verdict=verdict, status=target,
                      verdict_reasoning=reasoning)
        # Re-read: the status just written is what the cascade below and
        # every later branch must see, not the pre-verdict snapshot.
        hypothesis = memory.read(hypothesis_id)

    if verdict == "unsupported":
        # Spec section 6: "demoted to an open assumption; task spawned for
        # better evidence". The hypothesis survives — VERDICT_WEIGHT
        # ["unsupported"] is 0.5, so recompute puts it back to `proposed`
        # — and the assumption records the claim as something now owed
        # proof.
        assumption_index = index_of(memory, "assumption", ASSUMPTION_KEY)
        candidate = {
            "statement": hypothesis["claim"], "raised_by": task_id,
            "status": "open", "blocks": [hypothesis_id], "refuted_by": None,
            "provenance": {"task": task_id, "agent": "verifier"},
        }
        key = ASSUMPTION_KEY(candidate)
        if key in assumption_index:
            result.reused.append(assumption_index[key])
        else:
            created = memory.create("assumption", candidate)
            result.created.append(created["id"])

        gap = (gates.evidence_gap(graph, cfg, hypothesis_id)
               or "the adversarial verifier found the quotes insufficient")
        search_id, created = create_task(
            memory, index_of(memory, "task", TASK_KEY),
            question=f"Find independent evidence for or against: "
                     f"{hypothesis['claim']} — {gap}",
            kind="search", parent=task_id, depth=task["depth"],
            origin_task=task_id, agent="scheduler",
            # Without this, ensure_evidence_tasks cannot see that a
            # search for this hypothesis is already open and spawns a
            # near-duplicate for the same gap.
            inputs={"for_hypothesis": hypothesis_id},
        )
        (result.created if created else result.reused).append(search_id)
        result.spawned.append(search_id)

    refutes = inputs.get("refutes")
    if verdict == "contradicted" and refutes:
        try:
            assumption = memory.read(refutes)
        except (KeyError, nodes.NodeFormatError):
            result.dropped.append(
                ("cascade", f"{refutes} no longer exists"))
            assumption = None
        else:
            try:
                memory.validate(assumption)
            except memory_mod.ValidationError as error:
                # Validated BEFORE anything indexes or writes it, exactly
                # as run_cascades does a few dozen lines below and for the
                # same reason — that function's docstring spells the
                # hazard out and this one used to ignore it.
                #
                # `assumption["status"]` on a record that parsed but lost
                # that key is a bare KeyError; `memory.update(refutes,
                # status="refuted", ...)` re-validates the whole merged
                # record, so an unrelated invalid field (a `blocks` entry
                # failing its pattern, a missing `provenance`, a
                # `raised_by` that is no longer a task id) is a
                # ValidationError. Neither is caught by research.main,
                # which means no `tick_submitted`, a bare traceback, and
                # every single retry dying on the same line — with
                # hand-editing research/memory/ the only escape, which
                # SKILL.md forbids.
                #
                # Declining is the whole fix: an assumption code cannot
                # validate is one code must not rewrite. The verdict
                # itself still lands (it was applied above), fsck reports
                # the file, and this says so rather than failing silently.
                result.dropped.append((
                    "cascade",
                    f"{refutes} is schema-invalid, so it cannot be marked "
                    f"refuted or cascaded: {error}",
                ))
                assumption = None
        if assumption is not None and assumption["status"] != "refuted":
            # Refutes without marking `cascaded`: that flag is
            # run_cascades's alone to set, once it has actually run.
            # Refuted here; cascaded later. submit runs run_cascades()
            # after every artifact in the tick has been applied and every
            # completed task marked `done`, so a cascade cannot stale a
            # task whose own artifact is still queued behind it — that
            # task would then be marked `done` on top of the stale flag
            # and its work, resting on a premise now known false, would
            # silently stand.
            memory.update(refutes, status="refuted", refuted_by=hypothesis_id)
            result.cascaded.append(refutes)
        elif assumption is not None and not assumption.get("cascaded"):
            # Already refuted, but not yet cascaded: "refuted" and
            # "refuted and cascaded" are indistinguishable from status
            # alone, and a crash landing between the write above and
            # run_cascades is exactly what makes this branch reachable on
            # a recovery re-apply of the same artifact. Report it again
            # so the run_cascades call right after this recovery pass
            # still performs the invalidation the crash interrupted.
            result.cascaded.append(refutes)
        elif assumption is not None:
            # Already refuted AND already cascaded. Re-running the
            # cascade would re-stale tasks a later tick has since
            # completed.
            result.dropped.append(
                ("cascade", f"{refutes} was already refuted"))

    graph.invalidate_cache()
    return result.sort()


def run_cascades(graph, assumption_ids):
    """Run the invalidation cascade for each refuted assumption.

    Separated from apply_verify so submit controls *when* it happens:
    after every artifact in the tick is applied and every completed task
    is marked `done`. A cascade that ran mid-loop could stale a task
    whose artifact had not been applied yet, and the later `done` write
    would erase the stale flag.

    Skips anything no longer `refuted` — Graph.cascade raises on that, and
    submit's recovery path re-applies artifacts. Also skips anything
    already marked `cascaded`: "refuted" alone does not say whether the
    cascade for it has run, which is exactly what the `cascaded` field on
    the assumption node is for (schemas/assumption.json). Setting it here,
    only after `cascade()` returns, is what makes a crash between the two
    recoverable in either direction — a crash before this line leaves
    `cascaded` unset, so a later call still performs the invalidation; a
    crash after it does not re-run a cascade whose stale-marking a later
    tick may have already legitimately superseded.

    Also skips a refuted assumption that is not itself schema-valid (say,
    a `refuted_by` failing its own pattern). `graph.cascade()` never calls
    `validate()` on the assumption -- it only reads `status` -- so it
    would run and commit stale/quarantine writes regardless; but the
    marker write below always re-validates the WHOLE merged record, and a
    field invalid for an unrelated reason would then raise AFTER those
    writes land and WITHOUT the marker set, wedging every retry on the
    same line: the cascade re-runs, re-commits the same writes, and
    re-raises, forever. Validated BEFORE cascading, not after, so a
    malformed record commits nothing and needs no undoing; fsck reports
    it on its own. This also closes the gap of indexing `status` on a
    record that parsed but is missing that key entirely -- validate()
    catches that too, before the index below ever runs.
    """
    results = []
    for assumption_id in sorted(set(assumption_ids)):
        try:
            assumption = graph.memory.read(assumption_id)
        except (KeyError, nodes.NodeFormatError):
            continue
        try:
            graph.memory.validate(assumption)
        except memory_mod.ValidationError:
            continue
        if assumption["status"] != "refuted" or assumption.get("cascaded"):
            continue
        graph.invalidate_cache()
        result = graph.cascade(assumption_id)
        graph.memory.update(assumption_id, cascaded=True)
        results.append((assumption_id, result))
    return results


def _citation_view(memory, citation_id):
    """One citation as a section payload sees it, or None if uncitable."""
    try:
        citation = memory.read(citation_id)
        memory.validate(citation)
    except (KeyError, nodes.NodeFormatError, memory_mod.ValidationError):
        return None
    if citation["status"] not in gates.CITABLE_STATUSES:
        return None
    return {
        "id": citation_id,
        "domain": citation["domain"],
        "quote": citation["quote"],
        # The synthesizer needs to know which sentences rest on a source
        # nobody could re-read, so it can hedge them rather than stating
        # them flat.
        "unverified": citation["status"] != "verified",
    }


def _section_payload(memory, graph, section):
    """Everything one synthesizer needs, frozen at seed time.

    Spec section 5's table: "one outline node + assigned facts + allowed
    cite keys". Frozen rather than recomputed at dispatch because
    scheduler.agent_input has no `root` and cannot read out/outline.json,
    and because a fact landing between dispatch and submit would otherwise
    make the artifact look like it dropped something it never saw.

    `graph` is here for one thing: whether a hypothesis's counter
    evidence is still live. That is a graph-wide question — a citation's
    gate-2 status — and cannot be answered from the section alone.
    """
    live = graph.live_citations()
    allowed = set()
    facts = []
    for fact_id in section["facts"]:
        try:
            fact = memory.read(fact_id)
            memory.validate(fact)
        except (KeyError, nodes.NodeFormatError, memory_mod.ValidationError):
            continue
        views = [view for view in
                 (_citation_view(memory, c) for c in fact["citations"])
                 if view is not None]
        allowed.update(view["id"] for view in views)
        facts.append({"id": fact_id, "statement": fact["statement"],
                      "citations": sorted(views, key=lambda v: v["id"])})

    hypotheses = []
    for hypothesis_id in section["hypotheses"]:
        try:
            hypothesis = memory.read(hypothesis_id)
            memory.validate(hypothesis)
        except (KeyError, nodes.NodeFormatError, memory_mod.ValidationError):
            continue
        for citation_id in hypothesis["supporting"] + hypothesis["counter"]:
            view = _citation_view(memory, citation_id)
            if view is not None:
                allowed.add(view["id"])
        hypotheses.append({
            "id": hypothesis_id, "claim": hypothesis["claim"],
            "status": hypothesis["status"],
            # The verdict, not the score. confidence is
            # base * spread * weight — a promotion threshold, not
            # something a writer can calibrate prose against.
            # synthesizer.md rule 4 used to contrast 0.9 with 0.5:
            # n/(n+2) reaches 0.9 only at 18 live citations, 0.5 is below
            # the 0.6 promotion floor, and every real promoted claim sits
            # in 0.60-0.75 — a band the rubric said nothing about.
            "verdict": hypothesis["verdict"],
            # `status` carries live opposition for a PROMOTED claim
            # (contested against supported) and not for a `proposed` one,
            # which is exactly the claim a writer is about to describe as
            # unsettled. Asks the same question _verified_status asks: is
            # the opposition LIVE. A counter citation rejected by gate 2
            # has no quote on its page and disputes nothing.
            "disputed": any(c in live for c in hypothesis["counter"]),
        })

    return {
        "id": section["id"],
        "title": section["title"],
        "hypotheses": hypotheses,
        "facts": facts,
        "allowed_cite_keys": sorted(allowed),
    }


def apply_outline(memory, graph, cfg, task_id, task, artifact, *, root=None,
                  **kwargs):
    """Accept the outliner's arrangement and seed one writer per section.

    The artifact is checked against the outline FROZEN INTO THIS TASK, not
    against a fresh computation. Recomputing would validate the model's
    answer against a graph it never saw.
    """
    frozen = (task.get("inputs") or {}).get("outline")
    if not frozen or not frozen.get("sections"):
        raise ApplyError(
            f"{task_id} is an outline task with no outline in its inputs; "
            "re-run `research synthesize` to seed it"
        )

    errors = outline_mod.validate(frozen, artifact)
    if errors:
        # Every problem in one message. Reporting them one per attempt
        # spends all three of the task's attempts on three complaints the
        # model could have fixed in a single retry.
        raise ApplyError(
            "the outline does not match the one you were given: "
            + "; ".join(errors)
        )

    accepted = outline_mod.apply_artifact(frozen, artifact)
    atomicio.write_text(
        Path(root) / "out" / outline_mod.PATH_NAME,
        json.dumps(accepted, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n",
    )

    result = ApplyResult()
    index = index_of(memory, "task", TASK_KEY)

    # Built once and reused: the synthesis payload below is the union of
    # these, and _section_payload walks every fact and citation in a
    # section.
    payloads = [_section_payload(memory, graph, section)
                for section in accepted["sections"]]

    def seed(question, payload):
        new_id, created = create_task(
            memory, index, question=question, kind="synthesize",
            parent=task_id, depth=task["depth"] + 1, origin_task=task_id,
            agent="outliner", inputs={"section": payload},
        )
        (result.created if created else result.reused).append(new_id)
        result.spawned.append(new_id)

    for payload in payloads:
        seed(f"write section {payload['id']}: {payload['title']}", payload)

    # Spec section 7's document shape puts a cross-cutting Synthesis after
    # the theme sections. It has to see every theme at once, which is
    # precisely what outline.validate forbids of an outline section — that
    # requires every hypothesis to be assigned exactly ONCE. So it is
    # seeded here instead, outside accepted["sections"], under a reserved
    # id. Its facts list is empty on purpose: the synthesis argues over
    # claims that the theme sections already evidenced, and re-litigating
    # raw facts is what makes a synthesis section a summary.
    seed("write the cross-cutting synthesis section", {
        "id": outline_mod.SYNTHESIS_SECTION_ID,
        "title": "Synthesis",
        "hypotheses": [hypothesis for payload in payloads
                       for hypothesis in payload["hypotheses"]],
        "facts": [],
        "allowed_cite_keys": sorted({
            key for payload in payloads
            for key in payload["allowed_cite_keys"]}),
    })
    return result.sort()


def apply_synthesize(memory, graph, cfg, task_id, task, artifact, *, root=None,
                     **kwargs):
    """Gate 5, then escape, then write the section body.

    The ORDER of those three is deliberate, for two reasons that hold
    regardless of how `latex.escape` happens to be implemented:

    1. Gate 5's rejection message quotes the offending sentences back to
       the model for its retry, and that quote must be the model's own
       text. A retry prompt showing `40\\%` where the model actually wrote
       `40%` teaches it to escape LaTeX by hand — precisely the job this
       design exists to take away from it.
    2. `latex.escape` is deliberately non-idempotent (see latex.py), so it
       must run at exactly one point in the pipeline. Running it here,
       after validation, means what gate 5 checked is byte-identical to
       what lands on disk, with no transformation in between to drift out
       of sync with it.

    Note for a future reader: this order is NOT pinned by a test. With
    `latex.escape` as currently written, it protects `\\cite{}`/`\\factref{}`
    spans by construction (its span regex is the same one gate 5 uses to
    extract them), so `cite_keys(body) == cite_keys(escape(body))` for
    every body either order would ever see — there is no failure mode left
    to assert against. Escape runs exactly once, here — never again at
    render time.
    """
    section = (task.get("inputs") or {}).get("section")
    if not section:
        raise ApplyError(
            f"{task_id} is a synthesize task with no section in its inputs; "
            "re-run `research synthesize` to rebuild the outline"
        )

    if artifact["section"] != section["id"]:
        # A synthesizer that echoed the wrong id would otherwise overwrite
        # a sibling section's file with this section's prose.
        raise ApplyError(
            f"this artifact answers for section {artifact['section']!r} but "
            f"{task_id} was dispatched for {section['id']!r}; echo back the "
            "section id you were given"
        )

    body = artifact["body"]
    problem = gates.report_section(body, section, graph)
    if problem:
        raise ApplyError(f"section {section['id']} failed gate 5: {problem}")

    # Written only after the gate passes, so a rejected artifact leaves
    # nothing on disk for render to pick up on a later run.
    atomicio.write_text(
        Path(root) / "sections" / f"{section['id']}.tex",
        latex.escape(body).strip() + "\n",
    )
    return ApplyResult().sort()


APPLIERS = {
    "decompose": apply_decompose,
    "search": apply_search,
    "extract": apply_extract,
    "recheck": apply_recheck,
    "hypothesize": apply_hypothesize,
    "verify": apply_verify,
    "outline": apply_outline,
    "synthesize": apply_synthesize,
}
