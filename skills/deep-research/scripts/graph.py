"""Derived views over the task DAG. Nothing here is stored; all of it
is computed from the node files, so it cannot drift out of sync."""
from dataclasses import dataclass, field

import confidence as confidence_mod
import ids as ids_mod
import memory as memory_mod
import nodes

# 'stale' is open: the invalidation cascade requeues work by setting it.
OPEN_TASK_STATUSES = ("pending", "ready", "stale")

# Statuses the cascade rewrites to 'stale'. `done` produced output on a
# premise now known false. `running` was dispatched but, because submit
# applies every inbox artifact *before* any cascade runs, has no artifact
# pending at this point — it timed out, and requeueing is what the spec
# asks for on expiry. `blocked` never produced output but rested on the
# same premise, so redoing it is right. Deliberately excluded: the three
# OPEN_TASK_STATUSES, which are already queued (re-marking them would only
# churn updated_at), and `abandoned`, which is a deliberate terminal state
# — reviving it would loop a task that already failed max_attempts times.
STALEABLE_TASK_STATUSES = ("done", "running", "blocked")

# Statuses a task can still produce an artifact from. `running` is not in
# OPEN_TASK_STATUSES — it has already left the frontier — but a task
# dispatched this very tick is the most alive a task ever is, and any
# question of the form "is an answer still coming?" has to include it.
# `abandoned` and `done` are terminal, and `blocked` never produces output
# on its own; a caller waiting on any of those waits forever.
LIVE_TASK_STATUSES = OPEN_TASK_STATUSES + ("running",)

# A dependency is satisfiable if it will, or may yet, produce output.
# `abandoned` never will — it is the terminal state after max_attempts —
# so a task depending on one can never be dispatched. `blocked` may yet:
# it is a human hold, and treating it as hopeless would let the coverage
# predicate halt a run while a checkpoint is waiting on the user.
SATISFIABLE_DEP_STATUSES = ("done", "running", "blocked")


@dataclass
class CascadeResult:
    stale_tasks: list = field(default_factory=list)
    quarantined_facts: list = field(default_factory=list)
    recomputed_hypotheses: list = field(default_factory=list)
    reopened_assumptions: list = field(default_factory=list)
    provenance_demoted_hypotheses: list = field(default_factory=list)
    # Neither list below is a defect report on the assumption being
    # cascaded; each id here is a node the cascade could not reason about
    # and therefore left exactly as it found it.
    #
    # skipped_tasks is every task in the WHOLE STORE that fails to parse
    # or fails its schema -- not only members of `affected`. A task's own
    # corruption can be exactly what makes it, and everything below it,
    # invisible to the affected-set walk in the first place (a task
    # missing its own `parent` field is never registered as anyone's
    # child, so its entire subtree silently drops out of subtree()) --
    # restricting this list to `affected` would miss precisely the tasks
    # whose corruption is the reason they were never counted as affected.
    #
    # skipped_nodes is every fact, assumption, hypothesis, or citation
    # `_readable` skipped anywhere in this cascade run, whether or not it
    # was ever reachable from `affected` -- a corrupt citation, for
    # instance, silently lowers a confidence score without ever appearing
    # in any other field on this dataclass.
    #
    # Task 16 journals both.
    skipped_tasks: list = field(default_factory=list)
    skipped_nodes: list = field(default_factory=list)


class CycleError(ValueError):
    """An edge would make the task graph cyclic."""


class Graph:
    """Derived views over the store, parameterised by the run's own
    thresholds.

    `max_depth`, `promotion_threshold` and `required_domains` all come
    from run.yaml and all three must be threaded in at every construction
    site. `required_domains` used to be hardcoded to 2 inside
    `recompute_confidence` while `apply._verified_status` read the
    configured value, and the two disagreed silently: at
    `required_domains: 3` a verdict scored 0.4 at promotion time and 0.6
    at recompute time, and 0.6 — computed against a bar nobody
    configured — is what persisted on the node for
    `predicates.min_hypothesis_confidence` and `render_status` to read.
    The defaults here match `runconfig.default` so a bare `Graph(memory)`
    in a test still behaves like a default run.
    """

    def __init__(self, memory, max_depth=4, promotion_threshold=0.67,
                 required_domains=2, min_citations=3):
        self.memory = memory
        self.max_depth = max_depth
        self.promotion_threshold = promotion_threshold
        self.required_domains = required_domains
        # Read by confidence.compute's volume term, which saturates at
        # this bar. Carried here for the same reason the other two are:
        # a caller that constructs a Graph with a value differing from
        # cfg must not have the score silently disagree with gate 3.
        self.min_citations = min_citations
        self._tasks = None
        self._valid_task_ids = None

    def _readable(self, node_type):
        """(filename_id, node) for every node of a type that both parses
        and satisfies its schema. Everything else is skipped.

        Three properties follow from this, and the rest of the module
        depends on all three.

        *Total.* `memory.read` raises KeyError on a missing file and
        NodeFormatError on an unparseable one; `memory.validate` raises
        ValidationError on a required key that has been deleted. Catching
        all three here is what makes the fact/assumption/hypothesis write
        loops unable to raise, which matters because the cascade writes
        across the whole graph from inside submit and the spec requires
        submit to be idempotent. A raise partway through leaves an
        invalidation half-applied and, as plan 1's final review measured,
        re-running dies on the same line.

        *Safe to index.* A node that validated has every required field,
        so callers can write `node["status"]` rather than threading
        `.get()` and a fallback through every access. It also guarantees
        `memory.update` cannot fail validation on corruption that was
        already on disk before this call.

        *Filename-keyed.* The yielded id comes from `memory.ids`, never
        from the node's own `id` field. `memory.update` resolves its path
        from the id it is handed, so passing a content-derived id writes to
        a different file than the one just read — or raises KeyError when
        that file does not exist.

        `read` and `validate` each get their own `try`, not one spanning
        both: a single `except (KeyError, ...)` around both calls would
        also swallow a KeyError raised by a bug inside jsonschema itself,
        turning a real defect into silence instead of a signal. Only
        `read`'s missing-file KeyError is meant to be caught here.

        Skipping is not silence: `cascade()` records every fact,
        assumption, or hypothesis id skipped this way in
        `CascadeResult.skipped_nodes`, and fsck.check reports every one of
        these as an error at the end of every submit regardless.

        NOT used for tasks. `Graph.tasks` deliberately does not call this
        — see its docstring for why a malformed task must stay a
        pass-through node in the topology rather than vanish the way a
        malformed fact or hypothesis correctly does here. `valid_task_ids()`
        is the schema check for tasks; it is asked, not assumed.
        """
        for node_id in self.memory.ids(node_type):
            try:
                node = self.memory.read(node_id)
            except (KeyError, nodes.NodeFormatError):
                continue
            try:
                self.memory.validate(node)
            except memory_mod.ValidationError:
                continue
            yield node_id, node

    def readable(self, node_type):
        """Public name for _readable. apply.py needs the same
        skip-the-corrupt iteration and should not reach through a
        private."""
        return self._readable(node_type)

    @property
    def tasks(self):
        """The DAG, keyed by *filename* id. Every file that PARSES is
        kept, valid or not.

        Not by frontmatter id: a file T-001.md whose frontmatter says
        `id: T-777` would otherwise put the phantom T-777 on the frontier,
        and memory.update("T-777", ...) raises KeyError because there is no
        such file. The filename is the only id the store can be trusted on
        — it is what path_for() resolves and what memory.ids() enumerates —
        so a frontmatter divergence stays a finding for fsck to report
        rather than becoming a crash in the dispatch loop.

        Schema-invalid tasks are deliberately NOT filtered out here, unlike
        every other `_readable`-backed collection in this module. An
        earlier version of this property did filter them out, on the
        theory that a task missing a required field should be invisible
        to dispatch — true for dispatch, but this dict is also what
        `children_map()`, `subtree()`, `would_cycle()`, `find_cycle()` and
        the cascade's own depends_on closure walk to find every task
        reachable from a refuted assumption. Filtering here made a
        malformed task (say, one merely missing `depth`, its `parent` and
        `depends_on` both intact) disappear from those walks entirely —
        not merely unindexable, but a hole in the graph. A healthy, done
        task on the far side of that hole, reachable from the refuted
        assumption only *through* the malformed one, kept its stale
        output because the walk never reached it. That is silent
        under-invalidation: work resting on a premise already known
        false, surviving the exact mechanism built to catch it. A crash
        on that same input — this property's failure mode before schema
        checking was added at all — is at least loud.

        Use `valid_task_ids()` to ask whether a given filename id here is
        additionally schema-valid. `frontier()` (dispatch) and the
        cascade's task-staling loop (writes) both consult it before
        indexing anything beyond `parent` / `depends_on`, because indexing
        `status` or `depth` directly on an entry from this dict is not
        safe the way it is for an `_readable`-backed collection.
        """
        if self._tasks is None:
            found = {}
            for task_id in self.memory.ids("task"):
                try:
                    found[task_id] = self.memory.read(task_id)
                except (KeyError, nodes.NodeFormatError):
                    continue
            self._tasks = found
        return self._tasks

    def valid_task_ids(self):
        """Filename ids from `self.tasks` whose file is additionally
        schema-valid. Cached alongside `self.tasks`; both reset together
        in `invalidate_cache()`.

        `self.tasks` keeps every task that merely parses so topology walks
        can route through a malformed one (see its docstring). Anything
        that indexes a task field beyond `parent` / `depends_on` needs
        this check first: dispatch eligibility in `frontier()`, because a
        task missing a required field may have no `question` to hand a
        worker, and the write in the cascade's task-staling loop, because
        `memory.update()` re-validates the merged record and a required
        field already missing on disk makes that raise.
        """
        if self._valid_task_ids is None:
            valid = set()
            for task_id, task in self.tasks.items():
                try:
                    self.memory.validate(task)
                except memory_mod.ValidationError:
                    continue
                valid.add(task_id)
            self._valid_task_ids = valid
        return self._valid_task_ids

    def invalidate_cache(self):
        self._tasks = None
        self._valid_task_ids = None

    def frontier(self):
        """Ids of tasks eligible for dispatch, sorted."""
        eligible = []
        valid = self.valid_task_ids()
        for task_id, task in sorted(self.tasks.items()):
            if task_id not in valid:
                continue  # a required field is missing; never dispatch it
            if task["status"] not in OPEN_TASK_STATUSES:
                continue
            if task["depth"] > self.max_depth:
                continue
            deps = task["depends_on"]
            if any(d not in self.tasks for d in deps):
                continue  # dangling dependency; fsck reports it
            if any(d not in valid for d in deps):
                continue  # a malformed dependency's status can't be trusted
            if all(self.tasks[d]["status"] == "done" for d in deps):
                eligible.append(task_id)
        return eligible

    def over_cap(self):
        """Open tasks deeper than the depth cap, sorted.

        These are structurally undispatchable: frontier() excludes them on
        depth and nothing ever lowers a task's depth, so they stay open
        forever. Spec section 4's coverage predicate needs to see them, or
        one of them holds the run open with nothing to dispatch. Task 11
        stops them being created; this reports any that exist anyway.

        Gated on valid_task_ids(), the same way frontier() gates before
        touching `status` or `depth`: both are required fields, and
        self.tasks (Task 1's split) keeps every task that merely parses,
        valid or not. Indexing either field on a schema-invalid entry
        would raise KeyError; a task whose own depth or status cannot be
        trusted is fsck's finding to report, not this query's to guess at.
        """
        valid = self.valid_task_ids()
        return sorted(
            task_id for task_id in valid
            if self.tasks[task_id]["status"] in OPEN_TASK_STATUSES
            and self.tasks[task_id]["depth"] > self.max_depth
        )

    def eventually_dispatchable(self):
        """Open tasks that can still reach the frontier, sorted.

        Computed as a fixed point rather than by enumerating failure
        modes: a task qualifies when it is within the depth cap and every
        dependency is already satisfiable or itself qualifies. Over-cap
        tasks, dangling dependencies, dependents of `abandoned` tasks and
        members of a dependency cycle all drop out, along with the
        transitive closure of each, without a special case for any of them.

        Terminates regardless of whether the graph is acyclic: `settled`
        only grows, over a task cache frozen for this call, so the number
        of passes that add at least one id is bounded by len(self.tasks).

        Like over_cap(), `settled` and `candidates` are built from
        valid_task_ids() before `status` or `depth` is indexed, for the
        same required-field reason. The depends_on walk below is
        different: depends_on is topology, not a required-field read, so
        it uses `.get("depends_on") or []` — the same tolerance
        would_cycle() and find_cycle() use — rather than the validity
        gate. A dependency that names a schema-invalid or dangling task
        is simply never `in settled`, so the task waiting on it never
        settles either; that is the fail-safe direction, not a crash.
        """
        valid = self.valid_task_ids()
        settled = {
            task_id for task_id in valid
            if self.tasks[task_id]["status"] in SATISFIABLE_DEP_STATUSES
        }
        candidates = {
            task_id for task_id in valid
            if self.tasks[task_id]["status"] in OPEN_TASK_STATUSES
            and self.tasks[task_id]["depth"] <= self.max_depth
        }
        grew = True
        while grew:
            grew = False
            for task_id in sorted(candidates - settled):
                deps = self.tasks[task_id].get("depends_on") or []
                if all(dep in settled for dep in deps):
                    settled.add(task_id)
                    grew = True
        return sorted(candidates & settled)

    def undispatchable(self):
        """Open tasks that can never reach the frontier, sorted.

        The complement of eventually_dispatchable() within the open set.
        The coverage halt predicate treats these as resolved-by-being-
        impossible and lists them as open questions, rather than waiting
        on them forever.

        `open_ids` is gated on valid_task_ids() for the same reason as
        over_cap() and eventually_dispatchable(): `status` is a required
        field, unsafe to index on a merely-parseable entry from
        self.tasks. A schema-invalid task is therefore reported by
        neither this query nor eventually_dispatchable() — fsck already
        reports it as its own defect, and guessing a status for it here
        would just be a second, less trustworthy way of doing that.
        """
        valid = self.valid_task_ids()
        open_ids = {
            task_id for task_id in valid
            if self.tasks[task_id]["status"] in OPEN_TASK_STATUSES
        }
        return sorted(open_ids - set(self.eventually_dispatchable()))

    # --- structure ---------------------------------------------------
    def would_cycle(self, task_id, new_dep):
        """True if task_id depending on new_dep would close a cycle."""
        if task_id == new_dep:
            return True
        seen, stack = set(), [new_dep]
        while stack:
            current = stack.pop()
            if current == task_id:
                return True
            if current in seen or current not in self.tasks:
                continue
            seen.add(current)
            # A malformed node's own depends_on may be missing; treat it as
            # empty (a dead end for this walk) rather than crash. `current`
            # is still visited above, so it cannot hide a cycle that would
            # otherwise run through it.
            stack.extend(self.tasks[current].get("depends_on") or [])
        return False

    def add_dependency(self, task_id, dep_id):
        if self.would_cycle(task_id, dep_id):
            raise CycleError(f"{task_id} -> {dep_id} would create a cycle")
        task = self.tasks[task_id]
        if dep_id in task["depends_on"]:
            return task
        updated = self.memory.update(
            task_id, depends_on=sorted(task["depends_on"] + [dep_id])
        )
        self.invalidate_cache()
        return updated

    def find_cycle(self):
        """Return one cycle as a closed walk of ids, or None. For fsck.

        Iterative. The recursive version raised RecursionError at a chain
        of roughly 1500 tasks, and a dependency chain has no bound in this
        design — max_depth caps decomposition depth, which is a different
        relation from depends_on.

        Each stack frame keeps its own iterator over that task's sorted
        dependencies, so `for dep in deps` resumes where it left off when
        the frame is revisited. That is what replaces the recursive call.

        See would_cycle(): a malformed node's depends_on may be missing;
        `.get(...) or []` treats that as a dead end rather than crash —
        the same tolerance the recursive version used, preserved here so
        `test_find_cycle_ignores_a_malformed_task_instead_of_crashing`
        still passes.
        """
        WHITE, GREY, BLACK = 0, 1, 2
        color = dict.fromkeys(self.tasks, WHITE)
        for root in sorted(color):
            if color[root] != WHITE:
                continue
            color[root] = GREY
            path = [root]
            stack = [(root, iter(sorted(self.tasks[root].get("depends_on") or [])))]
            while stack:
                node, deps = stack[-1]
                descended = False
                for dep in deps:
                    if dep not in color:
                        continue  # dangling; fsck reports it separately
                    if color[dep] == GREY:
                        return path[path.index(dep):] + [dep]
                    if color[dep] == WHITE:
                        color[dep] = GREY
                        path.append(dep)
                        stack.append(
                            (dep, iter(sorted(
                                self.tasks[dep].get("depends_on") or []
                            )))
                        )
                        descended = True
                        break
                if not descended:
                    color[node] = BLACK
                    path.pop()
                    stack.pop()
        return None

    def children_map(self):
        children = {}
        for task_id, task in self.tasks.items():
            # A malformed node's own parent may be missing; `.get()`
            # treats that as "no parent" rather than crash. The node is
            # still walked (this loop covers every id in self.tasks), so
            # it stays a pass-through for its own children below.
            parent = task.get("parent")
            if parent:
                children.setdefault(parent, []).append(task_id)
        return {k: sorted(v) for k, v in children.items()}

    def subtree(self, task_id):
        """All descendants via parent links, sorted, excluding task_id."""
        children = self.children_map()
        found, stack = set(), list(children.get(task_id, []))
        while stack:
            current = stack.pop()
            if current == task_id:
                raise CycleError(f"parent cycle at {current}")
            if current in found:
                continue
            found.add(current)
            stack.extend(children.get(current, []))
        return sorted(found)

    def _ancestry(self, task_id):
        """[task_id, its parent, ..., its top-level ancestor]."""
        chain, seen, current = [], set(), task_id
        while True:
            if current in seen:
                raise CycleError(f"parent cycle at {current}")
            seen.add(current)
            chain.append(current)
            parent = self.tasks.get(current, {}).get("parent")
            if parent is None or parent not in self.tasks:
                return chain
            current = parent

    def root_branch(self, task_id):
        """The top-level ancestor of a task.

        On a real run this is a constant function: `research init` seeds
        exactly one task with `parent: None` and everything descends from
        it. That is fine for what the predicates and the schedulers use it
        for — "are these two nodes in the same tree", which is what
        `agent_input`, `ensure_hypothesize_tasks` and `_branch_of` all
        ask — and deliberately unchanged. It is NOT what the saturation
        halt wants; see `theme_of`.
        """
        return self._ancestry(task_id)[-1]

    def theme_of(self, task_id):
        """The top-level THEME a task belongs to: its depth-1 ancestor.

        Spec section 4's saturation guard counts "distinct root branches"
        in the completion window, and spec section 7 says "root task
        branches become top-level sections". The top-level sections of a
        report are the THEMES the decomposer proposed — the children of
        the run's seeded root — not the single seeded root itself, which
        is the whole question and is therefore the same value for every
        task in the run. Keyed on `root_branch`, `saturation_branches: 2`
        was unsatisfiable by construction and saturation could never fire
        on any real run.

        The root task itself has no depth-1 ancestor, so it is its own
        theme. That keeps the function total for the one task where the
        distinction is meaningless.

        TWO consumers, and the second one is not a journal field:

        - the `root_branch` field submit writes on a `task_completed`
          record;
        - `outline._theme`, which rolls every hypothesis and fact up to
          the section it is reported in. Changing this function changes
          the report's chapter structure.

        Note what it is NOT used for: `outline` collects the report's
        THEMES from the run root's children directly (`outline._themes`),
        never by taking theme_of over every task. The self-theme fallback
        above is exactly why — it made the root, and any task parented
        straight onto it, look like a theme of its own.

        `Graph.root_branch` stays what the predicates and both schedulers
        read — this is a reporting axis, not a topology change, and
        widening it would change what "same branch" means for fact
        selection and hypothesizer scheduling too.
        """
        chain = self._ancestry(task_id)
        return chain[-2] if len(chain) > 1 else chain[-1]

    # --- invalidation ------------------------------------------------
    # Gate 2 is the only independent confirmation that a quote is really on
    # the page. It runs in a separate `rechecker` subagent, dispatched on a
    # later tick, that reads the page with the same WebFetch the extractor
    # used but sees only the url and the spans — never the claim a quote is
    # meant to support, so it has nothing to be led by. That is a different
    # subagent and a fresh context, not a different tool. Only a citation
    # that passed it counts as evidence for promotion.
    VERIFIED_CITATION_STATUS = "verified"

    # A citation that has not faced gate 2 at all. Its own `recheck` task
    # may or may not still be coming — live_rechecks_for is what tells the
    # difference, and the two are not interchangeable.
    PENDING_CITATION_STATUS = "pending"

    def live_citations(self):
        """Citations that are both still cited and independently confirmed.

        Two conditions, and both are necessary. At least one *active* fact
        must cite it — a quarantined fact's evidence is out of the report,
        so it is out of the score. And the citation's own status must be
        `verified`, i.e. the rechecker actually re-read the page and found
        the quote.

        The second condition is not the first one restated. Spec section 6
        rejects only the facts resting *solely* on a failed citation, and a
        403 or JS-wall marks a citation `unverifiable` rather than rejected,
        so both survive attached to a live fact. Surviving is not the same
        as counting: the spec's term for that state is "flagged rather than
        silently trusted", and full weight in the promotion number is
        precisely silent trust. `unverifiable` and `pending` citations
        therefore stay in the graph, the bibliography and Appendix D — they
        are flagged, not dropped — but they cannot carry a hypothesis over
        the promotion threshold. The failure direction is under-promotion.
        """
        cited = set()
        for _, fact in self._readable("fact"):
            if fact["status"] == "active":
                cited.update(fact["citations"])
        # Iterating readable files rather than `cited` keeps a dangling or
        # corrupt citation out of the live set for free, and keys on the
        # one id the store can be trusted on.
        return {
            citation_id for citation_id, citation in self._readable("citation")
            if citation_id in cited
            and citation["status"] == self.VERIFIED_CITATION_STATUS
        }

    def _domains_of(self, citation_ids):
        """The domain of each readable citation, skipping the rest.

        A citation id is only ever shape-checked by the schema — the
        `^C-[0-9]{3,}$` pattern says a reference looks like a reference, it
        cannot say the file exists — so a fact citing a citation whose write
        failed is a fully schema-valid store, no disk corruption required.
        Indexing here used to turn that into a KeyError thrown from inside
        the cascade, i.e. after the stale-marking and quarantine writes had
        already committed, wedging every later submit on the same line with
        no repair path the design permits.

        Skip-and-continue, the convention fsck.py already uses for this
        defect family: a dangling or field-less reference is fsck's to
        report, not the cascade's to die on. Dropping the citation from the
        domain list can only lower a score, so the failure mode is
        under-promotion — the safe direction.

        After the `_readable` helper landed, the surviving reason this is a
        per-id loop rather than a `_readable` pass is that the caller has a
        specific id list, not the whole store.

        The ValidationError branch of this guard is defence-in-depth and,
        as things stand, unreachable in normal operation: the only caller
        is `recompute_confidence()`, which filters `supporting` against
        `live_citations()` first, and `live_citations()` itself only ever
        returns ids that just passed through `_readable("citation")` --
        already read AND already schema-valid moments earlier. No test
        exercises that branch, and none should be manufactured to force
        it: doing so would mean fabricating a caller that hands this
        function a citation id `live_citations()` could never produce.
        (The KeyError branch is a different story and is genuinely
        exercised -- see `test_recompute_is_defensive_even_when_handed_a_dangling_live_id`
        in tests/test_graph_cascade.py, which injects a nonexistent
        citation id via a `live_citations()` subclass override.) Do not
        delete the ValidationError branch as dead code: it is the last
        line of defence if a future caller, or a future edit to
        `live_citations()`, ever stops guaranteeing every id here is
        already valid.
        """
        domains = []
        for citation_id in citation_ids:
            try:
                citation = self.memory.read(citation_id)
                self.memory.validate(citation)
            except (KeyError, nodes.NodeFormatError,
                    memory_mod.ValidationError):
                continue
            domains.append(citation["domain"])
        return domains

    def supporting_domains(self, hypothesis_id):
        """Domains of the live, verified citations supporting a hypothesis.

        The input gate 3 counts. One entry per citation, duplicates kept:
        confidence.compute reads both the count and the distinct set, and
        conflating them would lose the volume term.

        Total, like every other read here — an unreadable hypothesis has
        no supporting evidence as far as this is concerned, and fsck
        reports the file.
        """
        try:
            hypothesis = self.memory.read(hypothesis_id)
            self.memory.validate(hypothesis)
        except (KeyError, nodes.NodeFormatError, memory_mod.ValidationError):
            return []
        live = self.live_citations()
        return self._domains_of(
            [c for c in hypothesis["supporting"] if c in live])

    def refute_searches_for(self, hypothesis_id):
        """Ids of `search` tasks sent out to disprove this claim, sorted.

        Tagged `inputs.for_hypothesis` with `inputs.stance == "against"`.
        The stance is what separates them from the evidence-seeking
        searches `apply.ensure_evidence_tasks` spawns, which carry the
        same `for_hypothesis` tag and no stance at all — reading the tag
        alone conflates a search FOR a claim with a search against it,
        which is a mistake this codebase has now made twice.

        Reporting only. `halt.refutation_attempted` asks a strictly
        harder question — whether anything can *still* challenge the
        claim, which needs dispatchability — and deliberately does not
        call this: a halt predicate that shared a query with the
        renderer would be one refactor away from halting on what the
        report happens to say.
        """
        found = []
        for task_id in self.valid_task_ids():
            inputs = self.tasks[task_id].get("inputs") or {}
            if (inputs.get("for_hypothesis") == hypothesis_id
                    and inputs.get("stance") == "against"):
                found.append(task_id)
        return sorted(found, key=ids_mod.numeric)

    def was_challenged(self, hypothesis_id):
        """True if a search for this claim's disproof was ever sent out.

        Says nothing about whether it found anything — `counter` says
        that. This is the difference between "nobody looked" and "we
        looked and found nothing", which the report rendered identically
        as `Against: none` until refute searches existed and only one of
        the two was ever true.
        """
        return bool(self.refute_searches_for(hypothesis_id))

    def live_rechecks_for(self, hypothesis_id):
        """Ids of `recheck` tasks that can still settle a `pending`
        citation supporting this hypothesis, sorted.

        The single definition of "a gate-2 verdict is still coming", and
        it has two callers that must never disagree.
        `apply.ensure_evidence_tasks` declines to spawn an evidence-seeking
        search while this is non-empty — gate 3 counts only `verified`
        citations, so a hypothesis whose evidence is sitting in an
        unapplied re-check looks starved when it is merely unchecked, and
        searching for it means a redundant dispatch after every
        extraction. `halt.evidence_exhausted` asks the same question to
        decide whether a hypothesis with no evidence task at all is
        starved or merely early.

        Non-empty is not "has a pending citation". That was the veto's
        original test, and it livelocked: a `recheck` abandoned after
        three attempts (an ordinary outcome for a URL that keeps timing
        out) leaves its citations `pending` for ever, `abandoned` is not
        an open status so nothing was dispatchable, and the veto then
        suppressed the only thing that could still make work. Measured at
        empty frontier, empty eventually_dispatchable(), and no halt — so
        `research next` printed "nothing to dispatch" forever. A citation
        whose re-check is dead is not about to be checked; it is starved,
        and its hypothesis should get its search.

        Gated on valid_task_ids() before `status`/`kind` are indexed, like
        every other task query here: a schema-invalid task can never be
        dispatched, so letting one hold the veto would starve the
        hypothesis just as permanently. Reads are total for the same
        reason as supporting_domains — a dangling id is fsck's to report.
        """
        try:
            hypothesis = self.memory.read(hypothesis_id)
            self.memory.validate(hypothesis)
        except (KeyError, nodes.NodeFormatError, memory_mod.ValidationError):
            return []
        unchecked = set()
        for citation_id in hypothesis["supporting"]:
            try:
                citation = self.memory.read(citation_id)
                self.memory.validate(citation)
            except (KeyError, nodes.NodeFormatError,
                    memory_mod.ValidationError):
                continue
            if citation["status"] == self.PENDING_CITATION_STATUS:
                unchecked.add(citation_id)
        if not unchecked:
            return []
        found = []
        for task_id in self.valid_task_ids():
            task = self.tasks[task_id]
            if task["kind"] != "recheck":
                continue
            if task["status"] not in LIVE_TASK_STATUSES:
                continue
            # `inputs.citations` is what apply._seed_recheck writes to pair
            # a re-check with the citations its verdicts update; a
            # re-check for some other page settles nothing here.
            covered = (task.get("inputs") or {}).get("citations") or []
            if unchecked.intersection(covered):
                found.append(task_id)
        return sorted(found)

    def recompute_confidence(self):
        """Rescore every hypothesis against live evidence.

        Returns (id, old, new) for each hypothesis whose score moved. A
        hypothesis whose score does not move may still be rewritten, if its
        current status no longer matches the promotion threshold — that
        status-only reconciliation is not reported here, only value moves
        are. A hypothesis already known false (status "refuted") is never
        rewritten back to "proposed" by a low score; refutation is a
        stronger, separate signal that a recomputed confidence cannot undo.

        Scored against `self.required_domains`, not confidence.compute's
        own default: see this class's docstring for the silent
        disagreement that hardcoding it caused.

        `contested` is re-evaluated here, not only at the moment a verdict
        lands. It is a statement about the evidence as it stands now — "a
        live citation argues against this claim" — and over a multi-day
        run a counter citation can become live (its fact reactivated, its
        own gate-2 status moving from `pending`/`unverifiable` to
        `verified`) long after the verifier ran. Computed once, a
        hypothesis kept a `supported` badge with an undisclosed live
        dispute against it. Only a lateral move between the two promoted
        statuses is made here: this stays demote-only for `proposed`,
        because a rescore that could re-promote would immediately undo
        the cascade's provenance demotion — see
        `apply._verified_status`'s docstring.
        """
        live = self.live_citations()
        changed = []
        for hypothesis_id, hypothesis in self._readable("hypothesis"):
            supporting = [c for c in hypothesis["supporting"] if c in live]
            domains = self._domains_of(supporting)
            # Filtered through `live` exactly as `supporting` is. A
            # counter citation gate 2 rejected, or one no active fact
            # cites any more, argues against nothing — and counting it
            # would let a claim be demoted by evidence the report itself
            # refuses to stand behind.
            against = sum(1 for c in hypothesis["counter"] if c in live)
            new = confidence_mod.compute(
                domains, hypothesis["verdict"],
                required_domains=self.required_domains,
                counter=against, min_citations=self.min_citations)
            old = hypothesis["confidence"]
            status = hypothesis["status"]
            target = status
            if new < self.promotion_threshold and status != "refuted":
                target = "proposed"
            elif status in ("supported", "contested"):
                target = ("contested"
                          if any(c in live for c in hypothesis["counter"])
                          else "supported")
            if new == old and target == status:
                continue
            self.memory.update(hypothesis_id, confidence=new, status=target)
            if new != old:
                changed.append((hypothesis_id, old, new))
        return changed

    def cascade(self, assumption_id):
        """Re-open everything that rested on a refuted assumption."""
        assumption = self.memory.read(assumption_id)
        if assumption["status"] != "refuted":
            raise ValueError(
                f"{assumption_id} is {assumption['status']}, not refuted"
            )

        affected = set(self.subtree(assumption["raised_by"]))
        affected |= {b for b in assumption["blocks"] if b.startswith("T-")}

        # Close the set transitively over depends_on: a task that consumed
        # an affected task's output inherits the taint, because depends_on
        # is the data-flow relation, not merely a scheduling constraint.
        # Treating a scheduling dependency as a data dependency
        # over-invalidates rather than under-invalidates: over-invalidation
        # costs rework; under-invalidation ships claims resting on a premise
        # already known false. This terminates regardless of whether the
        # graph is acyclic: `affected` only grows, never shrinks, over a
        # task cache frozen for this call, so the number of passes that add
        # at least one id is bounded by len(self.tasks).
        #
        # A malformed task's depends_on may be missing; `.get(...) or []`
        # treats that as a dead end for THIS closure rather than crashing
        # — the node itself is still reachable (this loop iterates every
        # id in self.tasks, valid or not), it just cannot forward the
        # taint through an edge it cannot read.
        grew = True
        while grew:
            grew = False
            for task_id, task in self.tasks.items():
                if task_id in affected:
                    continue
                if any(dep in affected
                       for dep in (task.get("depends_on") or [])):
                    affected.add(task_id)
                    grew = True

        affected.discard(assumption["raised_by"])

        result = CascadeResult()

        # skipped_tasks starts from every malformed task in the WHOLE
        # store, not only members of `affected` — see CascadeResult's own
        # comment for why restricting it to `affected` would miss exactly
        # the tasks whose own corruption hid them from that walk.
        valid = self.valid_task_ids()
        skipped_tasks = set(self.memory.ids("task")) - valid

        # A task in `affected` that is not schema-valid cannot be written:
        # memory.update() re-validates the merged record, and a required
        # field already missing on disk makes that raise.
        for task_id in sorted(affected):
            task = self.tasks.get(task_id)
            if task is None:
                # Not a file `self.tasks` could even parse into a dict —
                # this is only reachable via a `blocks` entry naming a
                # task that does not exist on disk or is unparseable
                # garbage (subtree()/the depends_on closure above can only
                # ever add ids that are already keys in self.tasks). fsck
                # reports the dangling reference separately; this still
                # records that the cascade itself could not act on it.
                skipped_tasks.add(task_id)
                continue
            if task_id not in valid:
                continue  # already counted in skipped_tasks above
            if task["status"] in STALEABLE_TASK_STATUSES:
                self.memory.update(task_id, status="stale", attempts=0)
                result.stale_tasks.append(task_id)
        result.skipped_tasks = sorted(skipped_tasks)
        self.invalidate_cache()

        for fact_id, fact in self._readable("fact"):
            if (fact["status"] == "active"
                    and fact["provenance"]["task"] in affected):
                self.memory.update(fact_id, status="quarantined")
                result.quarantined_facts.append(fact_id)

        # A nested assumption confirmed by work inside the affected set was
        # confirmed by reasoning now known unsound. Reopen it rather than
        # refuting it: full transitive refutation would claim more than the
        # cascade actually knows — the nested assumption is unsupported, not
        # disproven.
        for other_id, other in self._readable("assumption"):
            if (other_id != assumption_id and other["status"] == "confirmed"
                    and other["raised_by"] in affected):
                self.memory.update(other_id, status="open")
                result.reopened_assumptions.append(other_id)

        # A hypothesis authored inside the affected set rested on reasoning
        # that pruned the tree using the now-refuted assumption, even if its
        # citations happen to remain live elsewhere. Demote it — do not
        # refute it, the claim is unsupported, not disproven — before
        # rescoring, so a still-high evidence score cannot re-promote it.
        for hypothesis_id, hypothesis in self._readable("hypothesis"):
            if (hypothesis["provenance"]["task"] in affected
                    and hypothesis["status"] not in ("proposed", "refuted")):
                self.memory.update(hypothesis_id, status="proposed")
                result.provenance_demoted_hypotheses.append(hypothesis_id)

        result.recomputed_hypotheses = self.recompute_confidence()

        # Nothing `_readable` skipped over in the fact/assumption/hypothesis
        # passes above, inside recompute_confidence's own hypothesis pass,
        # or inside live_citations()'s citation pass, should vanish either
        # — a corrupt citation silently lowers a confidence score without
        # ever touching any other field on this dataclass, so it has to be
        # counted here or it is not counted anywhere. Computed as one
        # fresh scan rather than threaded through each loop: corruption
        # does not change mid-cascade — every loop above only ever writes
        # to a node `_readable` already accepted — so the skip set is the
        # same whenever it is taken, and one pass here is simpler than
        # four call sites each tracking their own misses.
        result.skipped_nodes = sorted(
            self._unreadable_ids("fact")
            | self._unreadable_ids("assumption")
            | self._unreadable_ids("hypothesis")
            | self._unreadable_ids("citation")
        )
        return result

    def _unreadable_ids(self, node_type):
        """Every id of `node_type` that `_readable` skips: present on disk
        but unparseable or schema-invalid. Backs `CascadeResult.skipped_nodes`
        so cascade()'s own silent skips are visible rather than silent.
        """
        return (
            set(self.memory.ids(node_type))
            - {node_id for node_id, _ in self._readable(node_type)}
        )
