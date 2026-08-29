"""research submit — the tick commit. The only mutator in the loop.

Spec section 8: "submit --tick N is idempotent: inbox artifacts are keyed
by task id and already-submitted ones are skipped, so a crash mid-tick is
recovered by re-running the same submit."

The journal's artifact_applied record is the fast path for that. It is
not the *correctness* guarantee — natural-key dedup in apply.py is, and
it also covers a crash between the node writes and the journal write, the
one window a log-based skip cannot see — but it is emphatically not
behaviour-free either, and reading it as "merely an optimisation" is what
made this module's worst bug. A task on that list is NOT re-applied, so
everything downstream of applying it has to be reached some other way, or
a crash in the middle of a tick strands the task:

  * step 2 never sees it, so it stays `running` forever. `frontier()`
    excludes `running`, `coverage_halt`'s in-flight check then returns
    None forever, dependents never dispatch, and fsck reports nothing
    wrong. Closed by `_finish` below, driven off `recovered`.
  * step 3 never sees its cascade ids, so an assumption this task's
    artifact refuted stays refuted-but-never-cascaded and the work
    resting on it silently stands. Closed by replaying `cascaded` out of
    the journal record itself.

That same "read the journal back, not just the graph" discipline is what
steps 2, "task_completed" journaling, and the timeout/reject/unbuildable
paths below all lean on too — see each one's own comment for the specific
recovery hazard it closes.

Phase order:

  1  apply each dispatched task's artifact, in kind order then id order
  2  mark every applied task `done` — unless it is already `stale`
  3  run the cascades collected in step 1
  4  schedule follow-on work (hypothesizers, evidence searches)
  5  recompute confidence
  6  charge an attempt against every task `next` declined to dispatch
  7  fsck
  8  journal tick_submitted

Only one of these boundaries is load-bearing on ordering alone: **step 7
must run after step 3.** fsck reports a refuted assumption with no
`cascaded` marker as an error (see fsck.py), and a normal tick refutes an
assumption in step 1 and cascades it in step 3; checking before step 3
would misreport that gap on every single tick. Pinned by
`tests/test_submit.py::test_fsck_runs_after_the_cascade_so_a_fresh_refutation_is_not_misreported`,
which fails if `fsck.check` is moved ahead of `run_cascades`.

Step 2 before step 3 is **not** load-bearing the same way, and must not be
read as such. It was, before the fix for a resurfaced-stale-on-recovery
bug: a cascade running mid-loop used to stale a task whose artifact was
still queued, and a later unconditional `done` write would erase that
flag — verbatim the failure this ordering was written to prevent. The fix
that closed it did not restore the ordering's power; it replaced it. Step
2 now refuses to overwrite a task that is already `stale` (see its own
comment, right where that guard lives), and separately,
`Graph.cascade`'s `STALEABLE_TASK_STATUSES` includes `running` — so a
task's artifact applying and its cascade landing in either order produces
the same `stale` outcome regardless of which of steps 2/3 ran first. The
guard is what actually enforces the invariant now; the ordering is
defence in depth on top of it, not the mechanism. Moving step 2 after
step 3 today leaves the full suite green — that is expected, not a gap:
the guard covers what the ordering used to.

Step 1's kind order puts evidence before verdicts, so a verifier's
citations exist before its verdict is applied even when both land in the
same tick.

Step 6 charges an attempt against a task `research next` could not build
an input packet for (a verify task naming a dangling or corrupt
hypothesis, say — see scheduler.build_packet's per-task skip). That task
never enters a `dispatched` record, so nothing else in this module ever
sees it, and it is schema-valid, so Graph.undispatchable() cannot tell the
coverage halt about it either — it would sit "eventually dispatchable"
forever. `next` already makes this judgement once, over the exact
frontier it is about to act on, and journals it (`dispatch_skipped`);
step 6 reads that back rather than re-deriving the same judgement itself,
which is both cheaper (O(skips), not O(frontier x store) every tick) and
incapable of disagreeing with what `next` actually decided.
"""
import json
from dataclasses import dataclass, field

import apply
import fsck
import gates
import graph as graph_mod
import halt as halt_mod
import journal as journal_mod
import memory as memory_mod
import nodes
import runconfig
import workspace

HELP = "apply the artifacts for a tick"

# Kind order. A verifier's citations must exist before its verdict is
# applied, even when both land in the same tick — and a re-check must be
# applied before the hypotheses whose gate-3 score depends on it, or a
# citation verified this tick would not count until the next one.
KIND_ORDER = ("decompose", "search", "extract", "recheck", "hypothesize",
              "verify", "outline", "synthesize")


@dataclass
class SubmitReport:
    tick: int
    applied: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    rejected: list = field(default_factory=list)   # (task_id, reason)
    timed_out: list = field(default_factory=list)
    abandoned: list = field(default_factory=list)
    cascaded: list = field(default_factory=list)
    # (task_id, reason) for a task `next` declined to dispatch this tick
    # because it could not build an input packet for it — step 6.
    unbuildable: list = field(default_factory=list)
    spawned: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    halted: object = None
    already_submitted: bool = False


def _yield_snapshot(graph):
    """(active fact ids, citation domains). Differenced per artifact to
    give the saturation predicate its new_facts and new_domains."""
    facts = {fid for fid, fact in graph.readable("fact")
             if fact["status"] == "active"}
    domains = {citation["domain"]
               for _, citation in graph.readable("citation")}
    return facts, domains


def _tasks_with_event(events, event, tick):
    """Task ids already carrying `event` for this tick.

    The journal-keyed idempotence guard shared by the timeout, reject and
    unbuildable paths: a crash-and-rerun of the same tick must not
    re-charge an attempt for a failure it already journaled, even though
    the condition that caused it (no artifact, a bad artifact moved out
    of the inbox, an unbuildable input) is still just as true on the
    retry as it was the first time.
    """
    return {r["task"] for r in events
            if r.get("event") == event and r.get("tick") == tick
            and isinstance(r.get("task"), str)}


def _finish(memory, task_id):
    """Move a task whose artifact has been applied to `done`. Total.

    Called for every task step 1 applied AND for every task step 1
    skipped because the journal already records its artifact as applied.
    That second case is the whole point: a crash between the
    `artifact_applied` write and this step leaves the task `running`, the
    recovery re-run skips it on the fast path, and nothing else in the
    system ever moves it — `frontier()` excludes `running`, so it is not
    dispatchable; `coverage_halt` sees a tick in flight, so it never
    fires; fsck has no complaint, because `running` is a perfectly legal
    status. Measured at 18 of the 20 crash points in one ordinary tick.

    Returns True if it wrote.

    `running` and nothing else. That is the status `next` sets on every
    task it dispatches and the only status a task whose artifact has just
    been applied can be in — so writing exclusively from it is both
    behaviour-preserving for the ordinary path and the tightest possible
    guard for the recovery one. Every other status it might be found in
    belongs to something that outranks "this artifact landed":

    `stale` — a cascade requeued this task because its output rested on a
    premise since refuted. Overwriting that back to `done` is verbatim
    the failure the done-before-cascade split exists to prevent, and on a
    recovery re-run it can happen through the one door step 3 cannot see:
    the cascade ran in the crashed pass, so nothing runs this pass to
    re-set the flag. See this module's docstring.

    `abandoned` — a deliberate terminal state after max_attempts, which
    `Graph.cascade` itself declines to revive for the same reason.

    `done` — already terminal; writing it again only churns updated_at.

    Reads and validates rather than indexing: this is now on the recovery
    path for a record nothing upstream has checked, and `memory.update`
    re-validates the merged record, so a task whose file went
    schema-invalid between the dispatch and this call would raise out of
    `research submit` — a bare traceback, `tick_submitted` never landing,
    and every retry dying on the same line. fsck reports the file.
    """
    try:
        task = memory.read(task_id)
        memory.validate(task)
    except (KeyError, nodes.NodeFormatError, memory_mod.ValidationError):
        return False
    if task["status"] != "running":
        return False
    memory.update(task_id, status="done")
    return True


def _fail(memory, cfg, task_id, task, reason):
    """One rejection, timeout, or unbuildable-input failure. Returns True
    if the task was abandoned.

    Spec section 4: "A rejected artifact increments task.attempts ... At
    attempts == 3 the task is marked abandoned with its reason and
    surfaces in Appendix C as an open question. The loop never blocks on
    a task it cannot complete."

    Reads `attempts`/`status` with `.get` rather than indexing: every
    caller in this module now guards its own task record against
    schema-invalidity before reaching here, but this is the shared choke
    point for three call sites, and a fourth caller added later should
    not have to remember to re-derive that same guard for `_fail` not to
    raise `KeyError` on a corrupt record's missing keys.
    """
    attempts = (task.get("attempts") or 0) + 1
    if attempts >= cfg["config"]["max_attempts"]:
        memory.update(task_id, status="abandoned", attempts=attempts,
                      abandoned_reason=reason)
        return True
    # A task already `stale` (requeued by a cascade) must stay stale, not
    # be silently downgraded to plain `pending`: that flag is what tells
    # a later tick this task's prior output rested on a premise since
    # refuted. Both `stale` and `pending` are equally open for dispatch
    # (Graph.OPEN_TASK_STATUSES), so this changes nothing about when the
    # task is next eligible — it only stops _fail from erasing a signal
    # step 2, a few lines below, is equally careful never to overwrite.
    requeue_status = "stale" if task.get("status") == "stale" else "pending"
    memory.update(task_id, status=requeue_status, attempts=attempts)
    return False


def _move(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.replace(destination)


def submit(root, tick, *, memory=None):
    root = workspace.require(root)
    cfg = runconfig.load(root)
    memory = memory or memory_mod.Memory(root)
    graph = graph_mod.Graph(
        memory, max_depth=cfg["config"]["max_depth"],
        promotion_threshold=cfg["config"]["promotion_threshold"],
        required_domains=cfg["config"]["required_domains"])
    events = journal_mod.read(root)
    report = SubmitReport(tick=tick)

    if journal_mod.tick_submitted(events, tick):
        # A full no-op, but not an empty report: this tick's own artifacts
        # were already applied by an earlier call, and that earlier call's
        # `skipped` is exactly what a caller re-running the same submit
        # needs to see here too — the natural-key dedup in apply.py is
        # what makes re-applying safe, but nothing in that earlier call's
        # own report survives to be returned a second time otherwise.
        report.already_submitted = True
        report.skipped = sorted(journal_mod.applied_tasks(events, tick))
        return report
    dispatched = journal_mod.dispatched_for_tick(events, tick)
    if dispatched is None:
        # Checked before the tick-in-flight comparison below: a tick with
        # no dispatch record at all is refused for that reason specifically
        # (there is nothing here to apply), even when it also happens to
        # not be the current tick — the more fundamental precondition, so
        # it is the more informative message. The in-flight tick number is
        # named too: after a typo in --tick, it is the single most useful
        # fact for finding the right one.
        raise ValueError(
            f"tick {tick} was never dispatched; the run is currently at "
            f"tick {cfg['status']['tick']}"
        )
    if tick != cfg["status"]["tick"]:
        raise ValueError(
            f"tick {tick} is not the tick in flight ({cfg['status']['tick']}); "
            "re-run `research next` to see where the run is"
        )

    applied_records = journal_mod.applied_records(events, tick)
    already = set(applied_records)
    # A recovery re-run must not re-charge a failure it already
    # journaled for this tick either, even though the artifact file
    # for a rejected task has since been moved out of the inbox (so
    # re-detecting "no file" would otherwise look exactly like a
    # fresh timeout) or a timed-out task's artifact is, correctly,
    # still missing on the retry.
    failed = (_tasks_with_event(events, "task_timed_out", tick)
              | _tasks_with_event(events, "artifact_rejected", tick))
    already |= failed
    inbox = root / "inbox"

    # Kind order, then id order. Deterministic, and evidence first.
    def order(task_id):
        task = graph.tasks.get(task_id)
        # `.get`, not `[...]`: `graph.tasks` keeps every task that
        # merely PARSES (see its own docstring), so a task missing
        # `kind` entirely must not raise out of this sort key before
        # a single artifact in the tick has been read.
        kind = (task or {}).get("kind", "")
        rank = (KIND_ORDER.index(kind) if kind in KIND_ORDER
               else len(KIND_ORDER))
        return (rank, task_id)

    pending_cascades = []
    completed = []
    # Tasks this tick already applied in an earlier, crashed pass.
    # Not re-applied — dedup is what makes recovery safe, and
    # re-running a fetch and every write for an artifact already on
    # disk is exactly what the fast path is for — but still carried
    # into step 2, because "applied" and "finished" are two writes
    # with a crash window between them. See _finish.
    recovered = []

    # `.get`, not `[...]`: journal.read() guarantees a record is valid
    # JSON and a dict, nothing about its shape (see
    # journal.applied_records). A `dispatched` record missing
    # 'task_ids' used to raise KeyError here — and because that raise
    # happened before `tick_submitted` could ever land, `next`
    # reprinted the same in-flight tick and `submit` died on the same
    # line forever. An empty list applies nothing, journals
    # `tick_submitted`, and lets the run move on; `next` is also
    # careful with the same field, for the same reason.
    for task_id in sorted(dispatched.get("task_ids") or [], key=order):
        if task_id in already:
            report.skipped.append(task_id)
            if task_id in applied_records and task_id not in failed:
                # `not in failed`: a task carrying BOTH an applied and
                # a rejected/timed-out record for one tick is only
                # reachable through a hand-edited journal, but the two
                # say opposite things about what happened to it and
                # `_fail` has already given it a status of its own.
                # Deferring to the failure is the direction that
                # cannot invent a completion.
                recovered.append(task_id)
                # Carry-forward, replayed from the journal rather than
                # re-derived: the applier's `cascaded` ids reach step 3
                # via `pending_cascades`, and skipping the applier
                # skips that too. A crash between this record and
                # run_cascades would otherwise leave an assumption
                # refuted-but-never-cascaded, with the work resting on
                # it silently standing. run_cascades ignores anything
                # already marked `cascaded`, so replaying is a no-op
                # whenever the cascade did land.
                cascaded = applied_records[task_id].get("cascaded") or []
                pending_cascades += [c for c in cascaded
                                     if isinstance(c, str)]
            continue
        task = graph.tasks.get(task_id)
        if task is None or task_id not in graph.valid_task_ids():
            # `graph.tasks` keeps every task that merely parses;
            # `valid_task_ids()` is the schema check. A task that
            # parses but has lost a required key would otherwise
            # raise KeyError the moment anything below indexes it
            # (`task["kind"]` in schema_check/APPLIERS, `task
            # ["attempts"]` in _fail) — before a single OTHER
            # artifact in this tick was read, and with nothing caught
            # above ValueError/WorkspaceError in research.main, that
            # is a bare traceback, tick_submitted never lands, and
            # `next` reprints the same wedged tick forever. No
            # attempt can safely be charged against a record code
            # cannot trust to validate (memory.update() would just
            # raise the same way), so this is reported and the
            # artifact moved aside like any other rejection, never
            # retried against the same broken record.
            reason = ("the task record is missing, unparseable, or "
                     "schema-invalid; fsck reports it")
            report.rejected.append((task_id, reason))
            journal_mod.append(root, "artifact_rejected", tick=tick,
                               task=task_id, error=reason)
            artifact_path = inbox / f"{task_id}.json"
            if artifact_path.is_file():
                _move(artifact_path,
                      inbox / "rejected" / f"{task_id}.{tick}.json")
            continue

        path = inbox / f"{task_id}.json"
        if not path.is_file():
            # The timeout, observed the only way code can observe it:
            # the artifact never arrived. Spec section 4 requeues
            # rather than losing the task.
            reason = (f"no artifact after the "
                     f"{cfg['config']['agent_timeout']:g}s timeout")
            if _fail(memory, cfg, task_id, task, reason):
                report.abandoned.append(task_id)
            report.timed_out.append(task_id)
            journal_mod.append(root, "task_timed_out", tick=tick,
                               task=task_id)
            graph.invalidate_cache()
            continue

        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
            # UnicodeDecodeError is a ValueError but not a
            # JSONDecodeError — one stray non-UTF-8 byte from a
            # subagent must be a rejection like any other bad
            # artifact, not an unguarded exception that aborts the
            # whole tick (and dies identically on every retry, since
            # the same bytes are still sitting in the inbox).
            # journal.read solves the same problem for the journal
            # itself; this is the same discipline applied here.
            error_text = (
                "the artifact file could not be read or parsed as "
                f"JSON: {error}"
            )
        else:
            error_text = gates.schema_check(task["kind"], artifact, task_id)

        if error_text is None:
            facts_before, domains_before = _yield_snapshot(graph)
            try:
                result = apply.APPLIERS[task["kind"]](
                    memory, graph, cfg, task_id, task, artifact,
                    root=root)
            except apply.ApplyError as error:
                error_text = str(error)
            else:
                graph.invalidate_cache()
                facts_after, domains_after = _yield_snapshot(graph)
                # Carry-forward: every applier's cascaded ids must
                # reach run_cascades, or a refutation recorded in
                # step 1 above never gets its invalidation and a
                # crash between the two is unrecoverable. Accumulated
                # across every artifact in the tick, from every kind,
                # and run once below in step 3 — after every
                # completed task is marked `done` in step 2 — never
                # called per-artifact here.
                pending_cascades += result.cascaded
                report.spawned += result.spawned
                report.applied.append(task_id)
                completed.append((task_id, len(facts_after - facts_before),
                                  len(domains_after - domains_before)))
                journal_mod.append(
                    root, "artifact_applied", tick=tick, task=task_id,
                    kind=task["kind"], created=result.created,
                    reused=result.reused, dropped=result.dropped,
                    spawned=result.spawned, cascaded=result.cascaded,
                    rejected_citations=result.rejected_citations,
                    unverifiable_citations=result.unverifiable_citations,
                    reactivated_facts=result.reactivated_facts)
                _move(path, inbox / "applied" / f"{task_id}.{tick}.json")

        if error_text is not None:
            if _fail(memory, cfg, task_id, task, error_text):
                report.abandoned.append(task_id)
            report.rejected.append((task_id, error_text))
            journal_mod.append(root, "artifact_rejected", tick=tick,
                               task=task_id, error=error_text)
            # Moved out of the inbox, or the next attempt re-reads the
            # same bad artifact and spends all three attempts on one
            # mistake.
            _move(path, inbox / "rejected" / f"{task_id}.{tick}.json")
            graph.invalidate_cache()

    # Step 2: every applied task is done before any cascade runs —
    # UNLESS it is already `stale`. A recovery re-run can reprocess a
    # task whose artifact was applied AND cascaded away in an
    # earlier pass whose journal record was the one the crash
    # dropped: apply_verify sees the assumption already
    # refuted-and-cascaded and returns no NEW cascade ids, so step 3
    # below runs nothing this time, and this task's on-disk status is
    # already `stale` from that earlier cascade. Overwriting it back
    # to `done` here — verbatim the failure the done-before-cascade
    # split exists to prevent — would erase that flag through the
    # one door step 3 cannot see: nothing ran this pass to re-set it.
    # A task genuinely just applied this tick is always `running` at
    # this point, never `stale`, so this changes nothing on the
    # normal path.
    #
    # That `stale` check, not step 2 running before step 3, is what
    # actually enforces the invariant now — see this module's
    # docstring and _finish's. Graph.cascade's STALEABLE_TASK_STATUSES
    # includes `running`, so a task lands `stale` from its cascade
    # whether that cascade runs before or after this loop; ordering
    # can no longer change the outcome, only that guard can. Do not
    # remove it on the assumption that "step 2 already runs before
    # step 3 handles this" — that ordering is defence in depth on top
    # of the guard, not a substitute for it, and _finish is the one
    # place the invariant is still enforced if the ordering above it
    # ever moves.
    #
    # `recovered` is walked here too, and that is the load-bearing
    # half: a task whose artifact was applied in a crashed pass is
    # skipped by step 1's fast path, so this loop is the only thing
    # left that can move it out of `running` — and nothing else in the
    # system ever will.
    for task_id in [t for t, _, _ in completed] + recovered:
        _finish(memory, task_id)
    graph.invalidate_cache()

    # Step 3: now the cascades, so a stale flag is the last word.
    for assumption_id, cascade_result in apply.run_cascades(
            graph, pending_cascades):
        report.cascaded.append(assumption_id)
        # Carry-forward: Graph.cascade returns skipped_tasks and
        # skipped_nodes for every node it could not reason about — a
        # corrupt task, fact, assumption, hypothesis or citation
        # anywhere in the store, not only inside this assumption's
        # affected set. graph.py's own docstring stakes the whole
        # skip-on-invalid design on "fsck runs at the end of every
        # submit" to catch what the cascade itself could not act on;
        # that mitigation only holds if submit actually surfaces
        # these rather than discarding them, so they are journaled
        # alongside the cascade they came from.
        journal_mod.append(
            root, "cascade", tick=tick, assumption=assumption_id,
            stale_tasks=cascade_result.stale_tasks,
            quarantined_facts=cascade_result.quarantined_facts,
            skipped_tasks=cascade_result.skipped_tasks,
            skipped_nodes=cascade_result.skipped_nodes)
    graph.invalidate_cache()

    # Journaled after the cascades so root_branch and the done/stale
    # status are the settled ones. Guarded against a task_completed
    # already on the journal for this (tick, task): a recovery
    # re-run can reprocess an already-applied task whose
    # artifact_applied record (not this one) was what the crash
    # dropped, and natural-key dedup means that reprocessing
    # genuinely yields new_facts=0/new_domains=0 the second time —
    # correct for THAT call, but appending it as a SECOND completion
    # record duplicates it in the saturation window. Recorded once,
    # replayed any number of times, and every replay after the first
    # writes nothing new.
    #
    # `recovered` tasks are deliberately NOT journaled here. Their
    # completion record either already exists (the ordinary case, and
    # `already_completed` skips it) or was lost to the crash, and
    # inventing a replacement means inventing its yield: the honest
    # value on a re-run is new_facts=0/new_domains=0, which would
    # count as a DRY completion in the saturation window for a task
    # that in fact produced evidence. That biases towards halting a
    # productive run early — the one direction the halt predicates
    # must never fail in. A missing record only shifts the window.
    already_completed = _tasks_with_event(events, "task_completed", tick)
    for task_id, new_facts, new_domains in completed:
        if task_id in already_completed:
            continue
        try:
            # theme_of, not root_branch: `research init` seeds exactly
            # one task with `parent: None` and everything descends
            # from it, so root_branch is a CONSTANT over any real run
            # and `saturation_branches: 2` was unsatisfiable by
            # construction — saturation could never fire. The themes
            # (the seeded root's own children, i.e. the top-level
            # sections of the report) are what spec section 4's
            # branch guard is actually about. The field keeps its name
            # so existing journals still read; only what it is
            # computed from changes. See Graph.theme_of.
            branch = graph.theme_of(task_id)
        except graph_mod.CycleError:
            branch = None
        journal_mod.append(root, "task_completed", tick=tick, task=task_id,
                           root_branch=branch, new_facts=new_facts,
                           new_domains=new_domains)

    # Step 4: follow-on work, computed from the graph, never chosen by
    # a model.
    #
    # Skipped once the run is synthesising. `research synthesize`
    # computes an outline and FREEZES it into the outline task's
    # inputs; apply_outline validates the outliner's answer against
    # that frozen copy. Anything discovered after that point is
    # therefore invisible to the report — it lands in Appendix A with
    # no chapter discussing it, while Limitations counts it from the
    # stale view. Observed live: the submit that applied the outline
    # artifact spawned two fresh search tasks, and a hypothesis formed
    # from them reached the graph and no section.
    #
    # Deciding to write the report IS the decision to stop gathering.
    # `research continue` and a fresh `research synthesize` are how an
    # operator resumes research and picks up what changed.
    if cfg["status"]["phase"] != "synthesize":
        # ensure_refute_tasks runs last of the three: it reads promoted
        # status, and a `verify` artifact applied earlier in this same
        # submit is what promotes. Scheduled ahead of the others it would
        # miss every claim promoted this tick and pick them up one tick
        # late, which is survivable but makes the coverage halt's
        # unchallenged set lag the graph it is computed from.
        for scheduled in (apply.ensure_hypothesize_tasks(memory, graph, cfg),
                          apply.ensure_evidence_tasks(memory, graph, cfg),
                          apply.ensure_refute_tasks(memory, graph, cfg)):
            report.spawned += scheduled.spawned
        graph.invalidate_cache()

    # Step 5.
    graph.recompute_confidence()
    graph.invalidate_cache()

    # Step 6: charge an attempt against every task `next` declined to
    # dispatch this tick because it could not build an input packet
    # for it (a dangling, unparseable, or schema-invalid node the
    # task references — see scheduler.build_packet's per-task skip).
    # That task never becomes `running` and never enters a
    # `dispatched` record, so nothing else in this module ever sees
    # it, and it is schema-valid on its own terms, so
    # Graph.undispatchable() cannot report it to the coverage halt
    # either — it would sit "eventually dispatchable" forever with
    # nothing anyone will ever do about it. `next` already makes this
    # judgement once, over the exact frontier it is about to act on,
    # and journals it (`dispatch_skipped`) alongside its own
    # `dispatched` record; reading that back here — rather than
    # re-running the same check over the whole frontier ourselves —
    # is O(skips), never disagrees with what `next` actually decided
    # (a from-scratch frontier walk here does not know about `next`'s
    # own dispatch cap, and could charge a task `next` never even
    # attempted), and carries the ACTUAL reason `next` gave.
    already_unbuildable = _tasks_with_event(events, "task_unbuildable", tick)
    # `.get`, guarded on type: same discipline as _tasks_with_event
    # and journal.applied_records. A `dispatch_skipped` record whose
    # `task` field a hand-edit or an older format dropped used to
    # raise KeyError from inside the dict comprehension — after every
    # artifact in the tick had already been applied and every cascade
    # run, so `tick_submitted` never landed and the identical crash
    # repeated on every retry with no way to make progress.
    declined = {
        record["task"]: record.get("reason", "")
        for record in events
        if record.get("event") == "dispatch_skipped"
        and record.get("tick") == tick
        and isinstance(record.get("task"), str)
    }
    for task_id, reason in sorted(declined.items()):
        if task_id in already_unbuildable:
            continue
        task = graph.tasks.get(task_id)
        if task is None or task_id not in graph.valid_task_ids():
            continue  # fsck reports the record itself as invalid
        wrapped = (
            "`research next` could not build an input packet for "
            f"this task, so it was never dispatched: {reason}"
        )
        if _fail(memory, cfg, task_id, task, wrapped):
            report.abandoned.append(task_id)
        report.unbuildable.append((task_id, wrapped))
        journal_mod.append(root, "task_unbuildable", tick=tick,
                           task=task_id, error=reason)
        graph.invalidate_cache()

    # Step 7. Spec section 2: fsck "runs automatically at the end of
    # every submit". Reporting only — a pre-existing inconsistency
    # must not discard this tick's real work. Runs after every
    # cascade (step 3): fsck reports a refuted-but-not-cascaded
    # assumption as an error, and a normal tick refutes in step 1 and
    # cascades in step 3, so checking any earlier would misreport
    # that gap on every tick.
    report.findings = fsck.check(memory, graph)
    journal_mod.append(root, "fsck", tick=tick,
                       errors=len(fsck.errors(report.findings)),
                       findings=len(report.findings))

    journal_mod.append(root, "tick_submitted", tick=tick)

    events = journal_mod.read(root)
    report.halted = halt_mod.check(memory, graph, cfg, events)
    if report.halted:
        halt_mod.write_status(
            root, halt_mod.render_status(memory, graph, cfg, events,
                                         report.halted))
        halt_mod.record(root, cfg, report.halted)

    # Every function returning a collection of ids returns it sorted.
    report.applied = sorted(report.applied)
    report.skipped = sorted(report.skipped)
    report.rejected = sorted(report.rejected)
    report.timed_out = sorted(report.timed_out)
    report.abandoned = sorted(set(report.abandoned))
    report.cascaded = sorted(set(report.cascaded))
    report.unbuildable = sorted(report.unbuildable)
    report.spawned = sorted(set(report.spawned))
    return report


def add_arguments(parser):
    parser.add_argument("--tick", type=int, required=True,
                        help="the tick being submitted")


def run(args):
    report = submit(args.root, args.tick)
    if report.already_submitted:
        print(f"tick {report.tick} was already submitted; nothing to do.")
        print("Run `research next` for the next tick.")
        return 0

    print(f"tick {report.tick}: applied {len(report.applied)}, "
          f"rejected {len(report.rejected)}, timed out "
          f"{len(report.timed_out)}, skipped {len(report.skipped)}, "
          f"unbuildable {len(report.unbuildable)}")
    for task_id, reason in report.rejected:
        print(f"  rejected {task_id}: {reason}")
    for task_id, reason in report.unbuildable:
        print(f"  unbuildable {task_id}: {reason}")
    for task_id in report.abandoned:
        print(f"  abandoned {task_id} after "
              f"{runconfig.load(args.root)['config']['max_attempts']} attempts")
    for assumption_id in report.cascaded:
        print(f"  cascade ran for refuted {assumption_id}")
    if report.spawned:
        print(f"  spawned {len(report.spawned)} task(s): "
              f"{', '.join(report.spawned)}")
    errors = fsck.errors(report.findings)
    if errors:
        print(f"  fsck: {len(errors)} error(s) — run `research fsck` for "
              "the detail")

    if report.halted:
        print()
        print(f"HALT({report.halted.reason}) — {report.halted.detail}")
        print(f"Wrote {args.root}/out/status.md. Run `research continue` to "
              "keep going, or move on to synthesis.")
        return 0

    print()
    print("Next: run `research next`.")
    return 0
