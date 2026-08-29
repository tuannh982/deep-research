"""The tick commit. Every test that runs the same submit twice is
testing spec section 8's recovery promise."""
import json

import pytest

import evidence
import gates
import journal
import research
import runconfig
import submit as submit_mod
import workspace
from graph import Graph
from memory import Memory

PAGE = (
    "<!DOCTYPE html><html><body><h1>Latency</h1>"
    "<p>The service reports 42ms at p99 under steady load.</p>"
    "<p>Cold starts account for most of the tail, a finding this "
    "paragraph exists to make the page long enough that gate 2 does not "
    "call it a shell that never rendered.</p>"
    "<p>A third paragraph, so the extracted text clears the JS-wall "
    "threshold without depending on an exact character count.</p>"
    "</body></html>"
)


@pytest.fixture
def run(workspace_root):
    return workspace_root


@pytest.fixture
def mem(run):
    """Overrides the shared fixture so the store and the workspace are
    the same directory."""
    return Memory(run)


def dispatch(run, mem, tick, task_ids):
    """Stand in for `next` without printing a packet."""
    for task_id in task_ids:
        mem.update(task_id, status="running")
    journal.append(run, "dispatched", tick=tick,
                   task_ids=sorted(task_ids), agents={}, models={})
    cfg = runconfig.load(run)
    cfg["status"]["tick"] = tick
    runconfig.save(run, cfg)


def write_artifact(run, task_id, artifact):
    (run / "inbox" / f"{task_id}.json").write_text(
        json.dumps(artifact), encoding="utf-8")


def search_artifact(task_id, url="https://a-example.com/p"):
    return {"task_id": task_id,
            "sources": [{"url": url, "title": "P", "relevance": 0.9,
                         "why": "measures it"}],
            "queries": ["a search query"],
            "no_sources_reason": None}


def go(run, mem, tick):
    return submit_mod.submit(run, tick, memory=mem)


# --- the happy path ---------------------------------------------------

def test_an_accepted_artifact_marks_its_task_done(run, mem, mktask):
    task = mktask(question="find sources", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], search_artifact(task["id"]))
    report = go(run, mem, 1)
    assert report.applied == [task["id"]]
    assert mem.read(task["id"])["status"] == "done"


def test_an_accepted_artifact_is_applied_to_the_graph(run, mem, mktask):
    task = mktask(question="find sources", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], search_artifact(task["id"]))
    go(run, mem, 1)
    assert [t for t in mem.list("task") if t["kind"] == "extract"]


def test_the_inbox_file_moves_to_applied(run, mem, mktask):
    """Audit trail, and it keeps the inbox meaningful: a file still in
    inbox/ is work not yet accepted."""
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], search_artifact(task["id"]))
    go(run, mem, 1)
    assert not (run / "inbox" / f"{task['id']}.json").exists()
    assert list((run / "inbox" / "applied").glob(f"{task['id']}*.json"))


def test_the_journal_records_the_application(run, mem, mktask):
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], search_artifact(task["id"]))
    go(run, mem, 1)
    events = journal.read(run)
    assert journal.applied_tasks(events, 1) == {task["id"]}
    assert journal.tick_submitted(events, 1)


def test_the_completion_record_carries_the_yield_for_saturation(
    run, mem, mktask
):
    """The saturation predicate has no other source for this.

    `root_branch` on the record is the completing task's THEME — its
    depth-1 ancestor, the child of the run's seeded root — not the seeded
    root itself. Here `searcher` is the root (parent None) and `extractor`
    is its only child, so the extractor is its own theme. Keyed on the
    root instead, the field was the same value for every task in the run
    and `saturation_branches: 2` could never be satisfied.
    """
    searcher = mktask(question="find", kind="search")
    extractor = mktask(question="read", kind="extract", parent=searcher["id"])
    mem.update(extractor["id"], inputs={"url": "https://a-example.com/p",
                                        "title": "P",
                                        "domain": "a-example.com"})
    dispatch(run, mem, 1, [extractor["id"]])
    write_artifact(run, extractor["id"], {
        "task_id": extractor["id"], "url": "https://a-example.com/p",
        "facts": [{"statement": "42ms at p99",
                   "quote": "The service reports 42ms at p99"}],
        "published_at": None,
        "source_type": "primary",
        "no_facts_reason": None})
    go(run, mem, 1)
    completion = journal.completions(journal.read(run))[0]
    assert completion["new_facts"] == 1
    assert completion["new_domains"] == 1
    assert completion["root_branch"] == extractor["id"]


def test_a_second_tick_with_no_new_evidence_reports_a_dry_completion(
    run, mem, mktask
):
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], search_artifact(task["id"]))
    go(run, mem, 1)
    completion = journal.completions(journal.read(run))[0]
    assert completion["new_facts"] == 0
    assert completion["new_domains"] == 0


# --- recovery re-runs must not fabricate a saturation halt (C4) -------

def test_a_recovery_re_run_does_not_duplicate_a_task_completed_record(
    run, mem, mktask
):
    """C4: task_completed was appended unconditionally, so a recovery
    re-run that reprocesses an already-applied task (natural-key dedup:
    nothing NEW is created the second time) appended a second, genuinely
    dry completion record for the SAME (tick, task) into the saturation
    window. Replayed enough times, a one-tick-old, productive two-branch
    run is told it is exhausted. saturation_window is lowered to 4 here
    only to keep the arithmetic small and deterministic; the mechanism is
    the same at the default of 6, just slower to reach."""
    cfg = runconfig.load(run)
    cfg["config"]["saturation_window"] = 4
    runconfig.save(run, cfg)

    searcher1 = mktask(question="branch one", kind="search")
    extractor1 = mktask(question="read one", kind="extract",
                        parent=searcher1["id"])
    mem.update(extractor1["id"], inputs={"url": "https://a-example.com/p1",
                                         "title": "P", "domain": "a-example.com"})
    searcher2 = mktask(question="branch two", kind="search")
    extractor2 = mktask(question="read two", kind="extract",
                        parent=searcher2["id"])
    mem.update(extractor2["id"], inputs={"url": "https://a-example.com/p2",
                                         "title": "P", "domain": "a-example.com"})

    dispatch(run, mem, 1, [extractor1["id"], extractor2["id"]])
    artifact1 = {"task_id": extractor1["id"], "url": "https://a-example.com/p1",
                "facts": [{"statement": "one",
                          "quote": "The service reports 42ms at p99"}],
                "published_at": None,
                "source_type": "primary",
                "no_facts_reason": None}
    artifact2 = {"task_id": extractor2["id"], "url": "https://a-example.com/p2",
                "facts": [{"statement": "two",
                          "quote": "The service reports 42ms at p99"}],
                "published_at": None,
                "source_type": "primary",
                "no_facts_reason": None}
    write_artifact(run, extractor1["id"], artifact1)
    write_artifact(run, extractor2["id"], artifact2)

    report = go(run, mem, 1)
    completions = journal.completions(journal.read(run))
    assert len(completions) == 2
    assert all(c["new_facts"] == 1 for c in completions)
    assert report.halted is None

    # Two "recovery" cycles, the crash the brief models: restore both
    # artifacts, drop both artifact_applied records AND tick_submitted,
    # so the whole tick genuinely reprocesses (natural-key dedup finds
    # both fact and citation already exist -- new_facts=0 this time,
    # correctly) rather than hitting the already-submitted no-op.
    for _ in range(2):
        write_artifact(run, extractor1["id"], artifact1)
        write_artifact(run, extractor2["id"], artifact2)
        surviving = [
            e for e in journal.read(run)
            if e["event"] != "tick_submitted"
            and not (e["event"] == "artifact_applied"
                     and e.get("task") in (extractor1["id"], extractor2["id"]))
        ]
        journal.path_for(run).write_text(
            "".join(json.dumps(e, sort_keys=True) + "\n" for e in surviving),
            encoding="utf-8")
        report = go(run, mem, 1)
        assert report.halted is None

    completions = journal.completions(journal.read(run))
    assert len(completions) == 2


# --- gate 1 rejection -------------------------------------------------

def test_a_schema_invalid_artifact_is_rejected_and_requeued(run, mem, mktask):
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], {"task_id": task["id"], "sources": "nope",
                                     "queries": ["a search query"],
                                     "no_sources_reason": None})
    report = go(run, mem, 1)
    assert [t for t, _ in report.rejected] == [task["id"]]
    stored = mem.read(task["id"])
    assert stored["status"] == "pending"
    assert stored["attempts"] == 1


def test_the_rejection_reason_is_journaled_for_the_retry_prompt(
    run, mem, mktask
):
    """Spec section 4: 'next re-emits the task with the validator error
    appended to its prompt.' journal.last_rejection is how it gets
    there."""
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], {"task_id": task["id"], "sources": "nope",
                                     "queries": ["a search query"],
                                     "no_sources_reason": None})
    go(run, mem, 1)
    assert "sources" in journal.last_rejection(journal.read(run), task["id"])


def test_a_rejected_inbox_file_is_moved_out_of_the_inbox(run, mem, mktask):
    """Left in place, the next attempt would re-read the same bad
    artifact and burn all three attempts on one mistake."""
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], {"task_id": task["id"], "sources": "nope",
                                     "queries": ["a search query"],
                                     "no_sources_reason": None})
    go(run, mem, 1)
    assert not (run / "inbox" / f"{task['id']}.json").exists()
    assert list((run / "inbox" / "rejected").glob(f"{task['id']}*.json"))


def test_unparseable_json_is_a_rejection_not_a_crash(run, mem, mktask):
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    (run / "inbox" / f"{task['id']}.json").write_text("{not json",
                                                      encoding="utf-8")
    report = go(run, mem, 1)
    assert [t for t, _ in report.rejected] == [task["id"]]


def test_an_artifact_for_the_wrong_task_is_rejected(run, mem, mktask):
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], search_artifact("T-999"))
    report = go(run, mem, 1)
    assert [t for t, _ in report.rejected] == [task["id"]]


def test_an_apply_error_is_a_rejection_not_a_crash(run, mem, mktask):
    """A hypothesizer citing a citation that does not exist passes gate 1
    and fails in the applier. It must cost the task an attempt, not the
    whole submit."""
    task = mktask(question="form claims", kind="hypothesize")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], {
        "task_id": task["id"],
        "hypotheses": [{"claim": "c", "supporting": ["C-404"],
                        "counter": [], "refutes": None}],
        "no_hypotheses_reason": None})
    report = go(run, mem, 1)
    assert [t for t, _ in report.rejected] == [task["id"]]
    assert "C-404" in dict(report.rejected)[task["id"]]


def test_a_task_is_abandoned_at_max_attempts(run, mem, mktask):
    """Spec section 4: 'At attempts == 3 the task is marked abandoned with
    its reason ... The loop never blocks on a task it cannot
    complete.'"""
    task = mktask(question="q", kind="search")
    mem.update(task["id"], attempts=2)
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], {"task_id": task["id"], "sources": "nope",
                                     "queries": ["a search query"],
                                     "no_sources_reason": None})
    report = go(run, mem, 1)
    stored = mem.read(task["id"])
    assert stored["status"] == "abandoned"
    assert stored["attempts"] == 3
    assert stored["abandoned_reason"]
    assert report.abandoned == [task["id"]]


def test_the_abandon_threshold_comes_from_run_yaml(run, mem, mktask):
    cfg = runconfig.load(run)
    cfg["config"]["max_attempts"] = 1
    runconfig.save(run, cfg)
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], {"task_id": task["id"], "sources": "nope",
                                     "queries": ["a search query"],
                                     "no_sources_reason": None})
    go(run, mem, 1)
    assert mem.read(task["id"])["status"] == "abandoned"


def test_an_abandoned_task_does_not_block_the_coverage_halt(run, mem, mktask):
    """The two mechanisms have to agree, or a failed task holds the run
    open forever."""
    cfg = runconfig.load(run)
    cfg["config"]["max_attempts"] = 1
    runconfig.save(run, cfg)
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], {"task_id": task["id"], "sources": "nope",
                                     "queries": ["a search query"],
                                     "no_sources_reason": None})
    go(run, mem, 1)
    assert Graph(mem).eventually_dispatchable() == []


# --- a dispatched task's own record is corrupt (fix round 1, C2) ------

def _strip_line(path, line_prefix):
    """Delete every line starting with `line_prefix` from a node file,
    leaving it parseable but schema-invalid. Mirrors
    tests/test_scheduler.py's and tests/test_fsck.py's helper of the same
    name."""
    text = "".join(
        line for line in path.read_text().splitlines(keepends=True)
        if not line.startswith(line_prefix))
    path.write_text(text)


def test_a_schema_invalid_dispatched_task_is_rejected_not_a_crash(
    run, mem, mktask
):
    """C2: graph.tasks keeps every task that merely PARSES (its own
    docstring), so a task dispatched while healthy that loses a required
    key before submit runs -- external corruption, or a race between
    `next` and the subagents actually finishing -- must not raise
    KeyError out of order()/_fail() before a single OTHER artifact in the
    tick is read. One corrupt record must cost only itself, not wedge the
    whole tick (tick_submitted never landing, `next` reprinting the same
    tick forever)."""
    corrupt = mktask(question="q", kind="search")
    healthy = mktask(question="q2", kind="search")
    dispatch(run, mem, 1, [corrupt["id"], healthy["id"]])
    _strip_line(mem.path_for(corrupt["id"]), "attempts:")
    write_artifact(run, healthy["id"], search_artifact(healthy["id"]))
    report = go(run, mem, 1)
    assert corrupt["id"] in [t for t, _ in report.rejected]
    assert report.applied == [healthy["id"]]
    assert journal.tick_submitted(journal.read(run), 1)


def test_a_schema_invalid_dispatched_task_leaves_the_inbox_meaningful(
    run, mem, mktask
):
    """M3, folded in here: the corrupt task's own artifact (if one was
    even written) must not stay in inbox/ looking like unfinished work —
    and the rejection must be on the journal, not silent."""
    corrupt = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [corrupt["id"]])
    _strip_line(mem.path_for(corrupt["id"]), "attempts:")
    write_artifact(run, corrupt["id"], search_artifact(corrupt["id"]))
    go(run, mem, 1)
    assert not (run / "inbox" / f"{corrupt['id']}.json").exists()
    assert list((run / "inbox" / "rejected").glob(f"{corrupt['id']}*.json"))
    events = journal.read(run)
    assert any(e["event"] == "artifact_rejected" and e.get("task") == corrupt["id"]
              for e in events)


# --- a non-UTF-8 artifact byte (fix round 1, C3) -----------------------

def test_a_non_utf8_artifact_is_rejected_not_a_crash(run, mem, mktask):
    """C3: path.read_text(encoding="utf-8") raises UnicodeDecodeError --
    a ValueError, but not a JSONDecodeError, so the original guard missed
    it. One stray byte from one subagent must be a rejection like any
    other bad artifact, not an unguarded exception that discards the
    whole tick and dies identically on every retry."""
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    (run / "inbox" / f"{task['id']}.json").write_bytes(b"\xff\xfe{not utf8")
    report = go(run, mem, 1)
    assert [t for t, _ in report.rejected] == [task["id"]]
    stored = mem.read(task["id"])
    assert stored["status"] == "pending"
    assert stored["attempts"] == 1
    assert journal.tick_submitted(journal.read(run), 1)


# --- the timeout path -------------------------------------------------

def test_a_missing_artifact_requeues_the_task(run, mem, mktask):
    """Spec section 4: 'Each dispatched subagent carries a timeout. On
    expiry the task is requeued rather than lost, so one hung fetch
    cannot stall a tick.' Absence is how code observes the expiry."""
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    report = go(run, mem, 1)
    assert report.timed_out == [task["id"]]
    stored = mem.read(task["id"])
    assert stored["status"] == "pending"
    assert stored["attempts"] == 1


def test_a_repeatedly_missing_artifact_is_eventually_abandoned(run, mem,
                                                              mktask):
    """Otherwise a permanently hanging task is dispatched forever."""
    cfg = runconfig.load(run)
    cfg["config"]["max_attempts"] = 1
    runconfig.save(run, cfg)
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    go(run, mem, 1)
    assert mem.read(task["id"])["status"] == "abandoned"


def test_one_missing_artifact_does_not_stop_the_others(run, mem, mktask):
    slow = mktask(question="slow", kind="search")
    quick = mktask(question="quick", kind="search")
    dispatch(run, mem, 1, [slow["id"], quick["id"]])
    write_artifact(run, quick["id"], search_artifact(quick["id"]))
    report = go(run, mem, 1)
    assert report.applied == [quick["id"]]
    assert report.timed_out == [slow["id"]]


# --- attempts must not double-count across a same-tick retry (I1) -----
#
# C2 and C3 make a mid-tick crash routine, not an edge case: a schema-
# invalid dispatched task or a non-UTF-8 artifact no longer aborts the
# tick, so a real deployment recovers by re-running the same submit far
# more often than "the artifact_applied record was dropped" alone would
# suggest. Timeout and reject must be exactly as idempotent as apply.

def _drop_tick_submitted(run):
    """Simulate a crash landing after everything else this call wrote,
    but before the tick's own commit record. The whole tick genuinely
    reprocesses on the next call, rather than hitting the already-
    submitted no-op."""
    surviving = [e for e in journal.read(run) if e["event"] != "tick_submitted"]
    journal.path_for(run).write_text(
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in surviving),
        encoding="utf-8")


def test_a_timed_out_task_is_not_recharged_on_a_same_tick_retry(run, mem,
                                                                 mktask):
    """Three genuine recovery re-runs of a still-slow task must not, on
    their own, burn all of max_attempts on the one real timeout."""
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    go(run, mem, 1)
    assert mem.read(task["id"])["attempts"] == 1
    for _ in range(2):
        _drop_tick_submitted(run)
        go(run, mem, 1)
    assert mem.read(task["id"])["attempts"] == 1


def test_a_rejected_task_is_not_recharged_on_a_same_tick_retry(run, mem,
                                                                mktask):
    """The reject path's own wrinkle: the bad artifact is moved OUT of
    the inbox the first time, so a retry with no journal guard would see
    'no file' and recharge it as a FRESH timeout stacked on top of the
    original reject -- a double charge of a different, wrong kind."""
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], {"task_id": task["id"], "sources": "nope",
                                     "queries": ["a search query"],
                                     "no_sources_reason": None})
    go(run, mem, 1)
    assert mem.read(task["id"])["attempts"] == 1
    for _ in range(2):
        _drop_tick_submitted(run)
        go(run, mem, 1)
    assert mem.read(task["id"])["attempts"] == 1


# --- idempotence ------------------------------------------------------

def test_re_running_the_same_submit_changes_nothing(run, mem, mktask):
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], search_artifact(task["id"]))
    go(run, mem, 1)
    before = mem.all_ids()
    report = go(run, mem, 1)
    assert mem.all_ids() == before
    assert report.skipped == [task["id"]]


def test_re_running_after_a_crash_before_the_journal_write_converges(
    run, mem, mktask
):
    """The case the journal fast path cannot cover, and the reason
    application is idempotent by natural key rather than by log. The
    artifact was applied but the record never landed, so the re-run
    genuinely re-applies it — and must produce the same graph.

    Fix round 1 finding, outside the reviewer's own list: dropping only
    the `artifact_applied` record left `tick_submitted` behind, so the
    second `go()` call hit the already-submitted no-op and never actually
    reprocessed anything — this test passed for years without ever
    exercising the recovery path its own docstring describes. Fixed by
    dropping `tick_submitted` too, so the whole tick genuinely
    reprocesses, and by asserting on `report.applied` so a future
    regression back to the vacuous form is itself caught (mem.all_ids()
    is unchanged either way — by a real re-apply that converges, or
    trivially by doing nothing at all)."""
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], search_artifact(task["id"]))
    go(run, mem, 1)
    before = mem.all_ids()
    # Simulate the crash: put the artifact back and drop the record --
    # tick_submitted too, or the second call below is a no-op and never
    # reaches the natural-key-dedup path this test exists to exercise.
    write_artifact(run, task["id"], search_artifact(task["id"]))
    surviving = [e for e in journal.read(run)
                 if e["event"] != "tick_submitted"
                 and not (e["event"] == "artifact_applied"
                         and e.get("task") == task["id"])]
    journal.path_for(run).write_text(
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in surviving),
        encoding="utf-8")
    report = go(run, mem, 1)
    assert not report.already_submitted
    assert report.applied == [task["id"]]
    assert mem.all_ids() == before


def test_a_submit_for_an_already_submitted_tick_is_a_no_op(run, mem, mktask):
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], search_artifact(task["id"]))
    go(run, mem, 1)
    report = go(run, mem, 1)
    assert report.already_submitted


def test_a_submit_for_a_superseded_tick_is_refused(run, mem, mktask):
    """Distinct from 'never dispatched': tick 1's own dispatch record
    genuinely exists, but the run has since moved on to tick 2 without
    tick 1 ever being submitted. A tick number that was never dispatched
    at all (see the never-dispatched test below) always satisfies BOTH
    conditions at once, which is exactly why that scenario alone cannot
    tell the two raises in `submit` apart — this one can, since only the
    tick-in-flight comparison fires here."""
    first = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [first["id"]])
    second = mktask(question="q2", kind="search")
    dispatch(run, mem, 2, [second["id"]])
    with pytest.raises(ValueError, match="tick"):
        go(run, mem, 1)


def test_a_submit_for_a_tick_that_was_never_dispatched_is_refused(run, mem):
    with pytest.raises(ValueError, match="dispatched") as excinfo:
        submit_mod.submit(run, 1, memory=mem)
    # M2: the in-flight tick number is named too -- after a typo in
    # --tick, it is the single most useful fact for finding the right one.
    assert "0" in str(excinfo.value)


# --- the post-application phases -------------------------------------

def test_submit_schedules_a_hypothesizer_once_a_branch_has_evidence(
    run, mem, mktask
):
    searcher = mktask(question="find", kind="search")
    extractor = mktask(question="read", kind="extract", parent=searcher["id"])
    mem.update(extractor["id"], inputs={"url": "https://a-example.com/p",
                                        "title": "P",
                                        "domain": "a-example.com"})
    dispatch(run, mem, 1, [extractor["id"]])
    write_artifact(run, extractor["id"], {
        "task_id": extractor["id"], "url": "https://a-example.com/p",
        "facts": [
            {"statement": "one", "quote": "The service reports 42ms at p99"},
            {"statement": "two", "quote": "Cold starts account for most of "
                                         "the tail"},
            {"statement": "three", "quote": "A third paragraph"},
        ],
        "published_at": None,
        "source_type": "primary",
        "no_facts_reason": None})
    go(run, mem, 1)
    assert [t for t in mem.list("task") if t["kind"] == "hypothesize"]


def test_submit_runs_fsck_and_reports_findings(run, mem, mktask):
    """Spec section 2: fsck 'runs automatically at the end of every
    submit.'"""
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], search_artifact(task["id"]))
    stray = mktask(question="dangling", depends_on=["T-777"])
    report = go(run, mem, 1)
    assert any("T-777" in f.message for f in report.findings)


def test_fsck_findings_do_not_roll_back_the_tick(run, mem, mktask):
    """fsck is reporting only. A pre-existing inconsistency must not
    discard a tick's real work."""
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], search_artifact(task["id"]))
    mktask(question="dangling", depends_on=["T-777"])
    go(run, mem, 1)
    assert mem.read(task["id"])["status"] == "done"


def test_submit_recomputes_confidence(run, mem, mktask, mkcitation, mkfact,
                                      mkhypothesis):
    """Hand-computed: three verified citations on three domains, verdict
    supported. base = 3/5 = 0.6, spread = 1.0, weight = 1.0 -> 0.6."""
    task = mktask(question="q", kind="search")
    ids = []
    for index in range(3):
        citation = mkcitation(url=f"https://d{index}-example.com/x",
                              domain=f"d{index}-example.com", quote=f"a quoted span {index}")
        mkfact(statement=f"f{index}", citations=[citation["id"]],
               task=task["id"])
        ids.append(citation["id"])
    hypothesis = mkhypothesis(supporting=ids, verdict="supported",
                              task=task["id"])
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], search_artifact(task["id"]))
    go(run, mem, 1)
    # 3 citations on 3 domains: min(1, 3/3) * 3/(3+1) = 0.75
    assert mem.read(hypothesis["id"])["confidence"] == 0.75


# --- phase gate: no follow-on scheduling once synthesizing ------------

def test_submit_schedules_follow_on_work_during_research(
    run, mem, mktask, mkcitation, mkfact, mkhypothesis
):
    """The behaviour that must NOT change. A hypothesis short of gate 3
    (min_citations=3, required_domains=2) gets an evidence-seeking search,
    which is the whole mechanism that drives a run toward corroboration."""
    cfg = runconfig.load(run)
    cfg["status"]["phase"] = "research"
    runconfig.save(run, cfg)

    root = mktask(question="root", kind="search")
    citation = mkcitation(url="https://d0-example.com/x",
                          domain="d0-example.com", task=root["id"])
    mkfact(statement="f0", citations=[citation["id"]], task=root["id"])
    hypothesis = mkhypothesis(claim="short of gate 3",
                              supporting=[citation["id"]], task=root["id"])

    other = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [other["id"]])
    write_artifact(run, other["id"], search_artifact(other["id"]))
    go(run, mem, 1)

    spawned_for_hypothesis = [
        t for t in mem.list("task")
        if t["kind"] == "search"
        and (t.get("inputs") or {}).get("for_hypothesis") == hypothesis["id"]
    ]
    assert spawned_for_hypothesis


def test_submit_schedules_nothing_new_once_the_phase_is_synthesize(
    run, mem, mktask, mkcitation, mkfact, mkhypothesis
):
    """The fix. `research synthesize` freezes an outline; anything found
    after that point is invisible to the report, so finding it is worse
    than useless — it puts a hypothesis in Appendix A that no chapter
    discusses, and makes Limitations count it from a stale view."""
    cfg = runconfig.load(run)
    cfg["status"]["phase"] = "synthesize"
    runconfig.save(run, cfg)

    root = mktask(question="root", kind="search")
    citation = mkcitation(url="https://d0-example.com/x",
                          domain="d0-example.com", task=root["id"])
    mkfact(statement="f0", citations=[citation["id"]], task=root["id"])
    hypothesis = mkhypothesis(claim="short of gate 3",
                              supporting=[citation["id"]], task=root["id"])

    other = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [other["id"]])
    write_artifact(run, other["id"], search_artifact(other["id"]))
    report = go(run, mem, 1)

    spawned_for_hypothesis = [
        t for t in mem.list("task")
        if t["kind"] == "search"
        and (t.get("inputs") or {}).get("for_hypothesis") == hypothesis["id"]
    ]
    assert spawned_for_hypothesis == []
    # The tick otherwise applied normally: steps 1-3 are untouched by the
    # gate, only step 4 (follow-on scheduling) is skipped.
    assert report.applied == [other["id"]]
    assert mem.read(other["id"])["status"] == "done"


def test_continue_reopens_research_so_the_freeze_can_be_lifted(
    run, mem, mktask, mkcitation, mkfact, mkhypothesis
):
    """The freeze has to be liftable, or the run is one-way.

    SKILL.md says "To pick up where you left off, `research continue` and
    then `research synthesize` again", and references/loop-protocol.md
    says "To resume research, `research continue` clears the halt and a
    fresh `research synthesize` recomputes the outline over everything
    found since". Neither could happen: `_continue_run` never touched
    `phase`, `scheduler.run` refuses to overwrite `synthesize`, and
    nothing anywhere wrote `phase = "research"` after synthesis -- so
    step 4 stayed skipped for the life of the run and "everything found
    since" was, by construction, nothing.

    Same scene as test_submit_schedules_nothing_new_once_the_phase_is
    _synthesize, with one `research continue` in the middle."""
    cfg = runconfig.load(run)
    cfg["status"]["phase"] = "synthesize"
    runconfig.save(run, cfg)

    root = mktask(question="root", kind="search")
    citation = mkcitation(url="https://d0-example.com/x",
                          domain="d0-example.com", task=root["id"])
    mkfact(statement="f0", citations=[citation["id"]], task=root["id"])
    hypothesis = mkhypothesis(claim="short of gate 3",
                              supporting=[citation["id"]], task=root["id"])

    assert research.main(["continue", "--root", str(run)]) == 0
    assert runconfig.load(run)["status"]["phase"] == "research"

    other = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [other["id"]])
    write_artifact(run, other["id"], search_artifact(other["id"]))
    go(run, mem, 1)

    spawned_for_hypothesis = [
        t for t in mem.list("task")
        if t["kind"] == "search"
        and (t.get("inputs") or {}).get("for_hypothesis") == hypothesis["id"]
    ]
    assert spawned_for_hypothesis


def test_an_outstanding_recheck_can_still_reject_a_citation_mid_synthesis(
    run, mem, mktask, mkcitation, mkfact
):
    """What keeps render.py's dangling-cite guard alive.

    That guard used to justify itself with "submit runs
    ensure_evidence_tasks on every submit, so research carries on
    alongside the writers" — a premise the synthesis freeze falsified.
    The guarded case is still reachable by a different route, and this
    pins it: a `recheck` seeded before the freeze is still dispatchable
    during synthesis (the phase gate is on step 4, not on the frontier),
    and applying its artifact flips a citation to `rejected` after a
    section citing it was already accepted. The danger here is the
    opposite of a dead guard — a maintainer who reads the stale premise
    concludes the case cannot occur and deletes a guard that still
    fires."""
    cfg = runconfig.load(run)
    cfg["status"]["phase"] = "synthesize"
    runconfig.save(run, cfg)

    extract = mktask(question="extract", kind="extract", status="done")
    citation = mkcitation(url="https://a-example.com/p",
                          domain="a-example.com", quote="a quoted span",
                          status="pending", task=extract["id"])
    mkfact(statement="f0", citations=[citation["id"]], task=extract["id"])
    recheck = mktask(question="re-read", kind="recheck", parent=extract["id"],
                     depth=1)
    mem.update(recheck["id"], inputs={"url": "https://a-example.com/p",
                                      "quotes": ["a quoted span"],
                                      "citations": [citation["id"]]})

    assert recheck["id"] in Graph(mem).frontier()

    dispatch(run, mem, 1, [recheck["id"]])
    write_artifact(run, recheck["id"], {
        "task_id": recheck["id"], "url": "https://a-example.com/p",
        "outcome": "read", "quotes": [{"index": 0, "present": False}],
        "note": "the passage has been edited since"})
    report = go(run, mem, 1)

    assert report.applied == [recheck["id"]]
    assert mem.read(citation["id"])["status"] == "rejected"


def test_the_freeze_does_not_stop_confidence_recomputation(
    run, mem, mktask, mkcitation, mkfact, mkhypothesis
):
    """Step 5 must still run. A verify artifact applied during synthesis
    still has to move the hypothesis's confidence, or the report prints a
    score that contradicts its own Appendix A."""
    cfg = runconfig.load(run)
    cfg["status"]["phase"] = "synthesize"
    runconfig.save(run, cfg)

    root = mktask(question="root", kind="search")
    ids = []
    for index in range(3):
        citation = mkcitation(url=f"https://d{index}-example.com/x",
                              domain=f"d{index}-example.com",
                              quote=f"a quoted span {index}", task=root["id"])
        mkfact(statement=f"f{index}", citations=[citation["id"]],
               task=root["id"])
        ids.append(citation["id"])
    hypothesis = mkhypothesis(claim="c", supporting=ids, task=root["id"])
    verifier = mktask(question="verify", kind="verify", parent=root["id"])
    mem.update(verifier["id"], inputs={"hypothesis": hypothesis["id"],
                                       "refutes": None})
    dispatch(run, mem, 1, [verifier["id"]])
    write_artifact(run, verifier["id"], {
        "task_id": verifier["id"], "hypothesis": hypothesis["id"],
        "verdict": "supported", "failing_citations": [],
        "reasoning": "clears the bar"})
    go(run, mem, 1)
    stored = mem.read(hypothesis["id"])
    # 3 citations on 3 domains: min(1, 3/3) * 3/(3+1) = 0.75
    assert stored["confidence"] == 0.75
    assert stored["status"] == "supported"


# --- cascade ordering -------------------------------------------------

def test_a_cascade_runs_after_every_artifact_in_the_tick_has_landed(
    run, mem, mktask, mkcitation, mkfact, mkhypothesis, mkassumption
):
    """The reason run_cascades is separate from apply_verify. A cascade
    firing mid-loop stales a task whose artifact is still queued; the
    later `done` write erases the flag and work resting on a refuted
    premise silently stands."""
    root = mktask(question="root", kind="decompose")
    hypothesizer = mktask(question="claims", kind="hypothesize",
                          parent=root["id"], depth=1)
    sibling = mktask(question="sibling search", kind="search",
                     parent=root["id"], depth=1)
    ids = []
    for index in range(3):
        citation = mkcitation(url=f"https://d{index}-example.com/x",
                              domain=f"d{index}-example.com", quote=f"a quoted span {index}")
        mkfact(statement=f"f{index}", citations=[citation["id"]],
               task=hypothesizer["id"])
        ids.append(citation["id"])
    hypothesis = mkhypothesis(claim="c", supporting=ids,
                              task=hypothesizer["id"])
    assumption = mkassumption(raised_by=root["id"])
    verifier = mktask(question="verify", kind="verify", parent=root["id"],
                      depth=1)
    mem.update(verifier["id"], inputs={"hypothesis": hypothesis["id"],
                                       "refutes": assumption["id"]})

    dispatch(run, mem, 1, [verifier["id"], sibling["id"]])
    write_artifact(run, verifier["id"], {
        "task_id": verifier["id"], "hypothesis": hypothesis["id"],
        "verdict": "contradicted", "failing_citations": [],
        "reasoning": "the quotes say the opposite"})
    write_artifact(run, sibling["id"], search_artifact(sibling["id"]))

    go(run, mem, 1)
    assert mem.read(assumption["id"])["status"] == "refuted"
    # The sibling completed inside the tick that refuted the premise its
    # branch rested on, so the cascade requeues it rather than leaving it
    # marked done.
    assert mem.read(sibling["id"])["status"] == "stale"


def test_a_recovery_re_run_does_not_reset_a_cascaded_stale_flag_to_done(
    run, mem, mktask, mkcitation, mkfact, mkhypothesis, mkassumption
):
    """C1 (fix round 1): the exact scenario above, replayed through the
    crash the brief models. Both the verifier (itself inside the refuted
    assumption's own subtree, per Graph.cascade's `affected` set) and the
    sibling come out of tick 1 `stale`, from the cascade. Drop both
    artifact_applied records and restore both artifacts, simulating a
    crash before the journal write landed for either. On the recovery
    re-run, apply_verify sees the assumption already refuted AND
    cascaded, so it returns no new cascade ids and step 3 runs nothing —
    step 2 marking a just-reprocessed task `done` is the only thing left
    that could still clobber the stale flag, and it must not."""
    root = mktask(question="root", kind="decompose")
    hypothesizer = mktask(question="claims", kind="hypothesize",
                          parent=root["id"], depth=1)
    sibling = mktask(question="sibling search", kind="search",
                     parent=root["id"], depth=1)
    ids = []
    for index in range(3):
        citation = mkcitation(url=f"https://d{index}-example.com/x",
                              domain=f"d{index}-example.com", quote=f"a quoted span {index}")
        mkfact(statement=f"f{index}", citations=[citation["id"]],
               task=hypothesizer["id"])
        ids.append(citation["id"])
    hypothesis = mkhypothesis(claim="c", supporting=ids,
                              task=hypothesizer["id"])
    assumption = mkassumption(raised_by=root["id"])
    verifier = mktask(question="verify", kind="verify", parent=root["id"],
                      depth=1)
    mem.update(verifier["id"], inputs={"hypothesis": hypothesis["id"],
                                       "refutes": assumption["id"]})

    dispatch(run, mem, 1, [verifier["id"], sibling["id"]])
    verify_artifact = {
        "task_id": verifier["id"], "hypothesis": hypothesis["id"],
        "verdict": "contradicted", "failing_citations": [],
        "reasoning": "the quotes say the opposite"}
    search_artifact_for_sibling = search_artifact(sibling["id"])
    write_artifact(run, verifier["id"], verify_artifact)
    write_artifact(run, sibling["id"], search_artifact_for_sibling)
    go(run, mem, 1)
    assert mem.read(assumption["id"])["status"] == "refuted"
    assert mem.read(assumption["id"])["cascaded"] is True
    assert mem.read(sibling["id"])["status"] == "stale"
    assert mem.read(verifier["id"])["status"] == "stale"

    # Simulate the crash the brief models: restore both artifacts, drop
    # both artifact_applied records AND tick_submitted -- dropping only
    # the former leaves the already-submitted no-op in place and the
    # second call below never reprocesses anything at all.
    write_artifact(run, verifier["id"], verify_artifact)
    write_artifact(run, sibling["id"], search_artifact_for_sibling)
    surviving = [e for e in journal.read(run)
                 if e["event"] != "tick_submitted"
                 and not (e["event"] == "artifact_applied"
                         and e.get("task") in (verifier["id"], sibling["id"]))]
    journal.path_for(run).write_text(
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in surviving),
        encoding="utf-8")
    report = go(run, mem, 1)
    assert not report.already_submitted
    assert sorted(report.applied) == sorted([verifier["id"], sibling["id"]])
    # This recovery pass genuinely reprocessed both artifacts: apply_verify
    # sees the assumption already refuted+cascaded and returns no NEW
    # cascade ids, so no cascade actually runs this pass either.
    assert report.cascaded == []

    # Direction 1 (the bug): recovery must not reset either task to `done`.
    assert mem.read(sibling["id"])["status"] == "stale"
    assert mem.read(verifier["id"])["status"] == "stale"


def test_step_2_still_marks_a_genuinely_new_completion_done(run, mem, mktask):
    """Direction 2: the guard added for C1 must not stop step 2 from
    doing its normal job. A task that is not `stale` at apply time is
    still marked `done`, same as before this fix."""
    task = mktask(question="find sources", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], search_artifact(task["id"]))
    go(run, mem, 1)
    assert mem.read(task["id"])["status"] == "done"


def test_fsck_runs_after_the_cascade_so_a_fresh_refutation_is_not_misreported(
    run, mem, mktask, mkcitation, mkfact, mkhypothesis, mkassumption
):
    """I2: fsck.check flags a refuted assumption with no `cascaded`
    marker as an error (see fsck.py) -- correct for an assumption a
    crash left genuinely stranded between the two, wrong for one this
    very tick refuted and cascaded in the normal course of steps 1 and 3.
    That is only true if fsck runs after the cascade, and (before this
    test) that boundary was defended by nothing but a comment: moving
    fsck.check ahead of run_cascades left all other tests green."""
    task = mktask(question="q", kind="hypothesize")
    ids = []
    for index in range(3):
        citation = mkcitation(url=f"https://d{index}-example.com/x",
                              domain=f"d{index}-example.com", quote=f"a quoted span {index}")
        mkfact(statement=f"f{index}", citations=[citation["id"]],
               task=task["id"])
        ids.append(citation["id"])
    hypothesis = mkhypothesis(claim="c", supporting=ids, task=task["id"])
    assumption = mkassumption(raised_by=task["id"])
    verifier = mktask(question="verify", kind="verify", parent=task["id"])
    mem.update(verifier["id"], inputs={"hypothesis": hypothesis["id"],
                                       "refutes": assumption["id"]})
    dispatch(run, mem, 1, [verifier["id"]])
    write_artifact(run, verifier["id"], {
        "task_id": verifier["id"], "hypothesis": hypothesis["id"],
        "verdict": "contradicted", "failing_citations": [],
        "reasoning": "the quotes say the opposite"})
    report = go(run, mem, 1)
    assert mem.read(assumption["id"])["status"] == "refuted"
    assert mem.read(assumption["id"])["cascaded"] is True
    assert not any("not cascaded" in f.message for f in report.findings)


def test_verify_artifacts_are_applied_after_evidence_artifacts(run, mem,
                                                              mktask):
    """Kind order. A verifier's citations must exist before its verdict
    is applied, even when both land in the same tick."""
    assert submit_mod.KIND_ORDER.index("extract") < \
        submit_mod.KIND_ORDER.index("hypothesize")
    assert submit_mod.KIND_ORDER.index("hypothesize") < \
        submit_mod.KIND_ORDER.index("verify")
    assert set(submit_mod.KIND_ORDER) == set(
        __import__("gates").ARTIFACT_KINDS)


# --- halting after a submit ------------------------------------------

def test_submit_reports_a_halt_without_dispatching_anything(run, mem,
                                                            mktask):
    """Spec section 4: submit 'recomputes the frontier, and prints the
    next action'. When a predicate fires, the next action is not another
    tick."""
    task = mktask(question="q", kind="decompose")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], {"task_id": task["id"], "children": [],
                                     "assumptions": []})
    report = go(run, mem, 1)
    assert report.halted is not None
    assert report.halted.reason == "coverage"


def test_a_halt_from_submit_writes_out_status_md(run, mem, mktask):
    task = mktask(question="q", kind="decompose")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], {"task_id": task["id"], "children": [],
                                     "assumptions": []})
    go(run, mem, 1)
    assert (run / "out" / "status.md").is_file()


# --- the CLI ----------------------------------------------------------

def test_the_cli_submits_a_tick(run, mem, mktask, capsys, monkeypatch):
    """No fetcher is built at all any more: apply_extract only seeds a
    `recheck` task now (gate 2 moved to that task's own agent), so a
    decompose-only tick like this one never had anything to re-fetch."""
    task = mktask(question="q", kind="decompose")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], {
        "task_id": task["id"],
        "children": [{"question": "child", "kind": "search",
                      "rationale": "needed", "depends_on_index": []}],
        "assumptions": []})
    assert research.main(["submit", "--root", str(run), "--tick", "1"]) == 0
    out = capsys.readouterr().out
    assert "applied 1" in out
    assert "research next" in out


def test_the_cli_reports_a_wrong_tick_without_a_traceback(run, mem, mktask,
                                                          capsys):
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    assert research.main(["submit", "--root", str(run), "--tick", "9"]) == 1
    assert "error" in capsys.readouterr().err.lower()


def test_the_cli_prints_the_rejection_reason(run, mem, mktask, capsys):
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], {"task_id": task["id"], "sources": "nope",
                                     "queries": ["a search query"],
                                     "no_sources_reason": None})
    research.main(["submit", "--root", str(run), "--tick", "1"])
    assert "rejected" in capsys.readouterr().out.lower()


def test_submit_on_an_uninitialised_directory_says_so(tmp_path, capsys):
    assert research.main(["submit", "--root", str(tmp_path / "nope"),
                          "--tick", "1"]) == 1
    assert "research init" in capsys.readouterr().err


# --- carry-forward: a task `next` declined to dispatch ------------------
#
# scheduler.build_packet skips a task it cannot build an input packet for
# (agent_input's verify branch guards a dangling/unparseable/schema-invalid
# hypothesis, see tests/test_scheduler.py) rather than dispatching it. That
# task never enters a `dispatched` record, so it never appears in
# journal.dispatched_for_tick's task_ids, and nothing in submit's per-task
# loop above ever touches it. It is schema-valid on its own terms, so
# Graph.undispatchable() cannot report it either — it just sits on
# graph.frontier(), "eventually dispatchable" forever, holding the
# coverage halt off with nothing anyone will ever do about it.
#
# Fix round 1 replaced the original mechanism here: rather than submit
# re-deriving `next`'s decision by re-running scheduler.agent_input over
# the whole frontier every tick (expensive, and capable of disagreeing
# with `next` — which stops at its own dispatch cap), scheduler.run now
# journals every skip it makes (`dispatch_skipped`, tick + task + reason)
# right alongside its `dispatched` record, and submit's step 6 simply
# charges an attempt from that journal entry. `dispatch_skip` below stands
# in for that part of `next`, the same way `dispatch` stands in for the
# rest of it.

def dispatch_skip(run, tick, task_id, reason):
    """Stand in for the part of `next` that declines to dispatch a task
    whose input packet it could not build — scheduler.build_packet's
    `packet.skipped`, journaled as `dispatch_skipped`."""
    journal.append(run, "dispatch_skipped", tick=tick, task=task_id,
                   reason=reason)


def test_a_task_next_declined_to_dispatch_costs_it_an_attempt(
    run, mem, mktask
):
    stuck = mktask(question="verify", kind="verify")
    mem.update(stuck["id"], inputs={"hypothesis": "H-999", "refutes": None})
    other = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [other["id"]])
    dispatch_skip(run, 1, stuck["id"], "hypothesis H-999 is dangling")
    write_artifact(run, other["id"], search_artifact(other["id"]))
    report = go(run, mem, 1)
    stored = mem.read(stuck["id"])
    assert stored["status"] == "pending"
    assert stored["attempts"] == 1
    assert [t for t, _ in report.unbuildable] == [stuck["id"]]
    assert "H-999" in dict(report.unbuildable)[stuck["id"]]


def test_a_repeatedly_declined_task_is_eventually_abandoned(run, mem,
                                                             mktask):
    """Otherwise a task `next` will never manage to build a packet for is
    silently skipped forever."""
    cfg = runconfig.load(run)
    cfg["config"]["max_attempts"] = 1
    runconfig.save(run, cfg)
    stuck = mktask(question="verify", kind="verify")
    mem.update(stuck["id"], inputs={"hypothesis": "H-999", "refutes": None})
    other = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [other["id"]])
    dispatch_skip(run, 1, stuck["id"], "hypothesis H-999 is dangling")
    write_artifact(run, other["id"], search_artifact(other["id"]))
    report = go(run, mem, 1)
    stored = mem.read(stuck["id"])
    assert stored["status"] == "abandoned"
    assert stored["abandoned_reason"]
    assert stuck["id"] in report.abandoned


def test_abandoning_a_declined_task_unblocks_the_coverage_halt(
    run, mem, mktask
):
    """The two mechanisms have to agree here too: a task the loop could
    never dispatch must not hold a finished run open forever."""
    cfg = runconfig.load(run)
    cfg["config"]["max_attempts"] = 1
    runconfig.save(run, cfg)
    stuck = mktask(question="verify", kind="verify")
    mem.update(stuck["id"], inputs={"hypothesis": "H-999", "refutes": None})
    task = mktask(question="q", kind="decompose")
    dispatch(run, mem, 1, [task["id"]])
    dispatch_skip(run, 1, stuck["id"], "hypothesis H-999 is dangling")
    write_artifact(run, task["id"], {"task_id": task["id"], "children": [],
                                     "assumptions": []})
    report = go(run, mem, 1)
    assert mem.read(stuck["id"])["status"] == "abandoned"
    assert Graph(mem).eventually_dispatchable() == []
    assert report.halted is not None
    assert report.halted.reason == "coverage"


def test_re_running_the_same_submit_does_not_double_charge_a_declined_task(
    run, mem, mktask
):
    """Idempotence for step 6 too: re-running an already-submitted tick
    must not re-read the journal and charge a second attempt."""
    stuck = mktask(question="verify", kind="verify")
    mem.update(stuck["id"], inputs={"hypothesis": "H-999", "refutes": None})
    other = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [other["id"]])
    dispatch_skip(run, 1, stuck["id"], "hypothesis H-999 is dangling")
    write_artifact(run, other["id"], search_artifact(other["id"]))
    go(run, mem, 1)
    report = go(run, mem, 1)
    assert report.already_submitted
    assert mem.read(stuck["id"])["attempts"] == 1


def test_a_crash_after_charging_but_before_tick_submitted_does_not_recharge(
    run, mem, mktask
):
    """I1's guard, extended to step 6: a crash between charging the
    attempt (writing task_unbuildable) and the tick's own tick_submitted
    record must not double-charge on the recovery re-run, even though the
    dispatch_skipped condition that caused it is, correctly, still true."""
    stuck = mktask(question="verify", kind="verify")
    mem.update(stuck["id"], inputs={"hypothesis": "H-999", "refutes": None})
    other = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [other["id"]])
    dispatch_skip(run, 1, stuck["id"], "hypothesis H-999 is dangling")
    write_artifact(run, other["id"], search_artifact(other["id"]))
    go(run, mem, 1)
    assert mem.read(stuck["id"])["attempts"] == 1
    # Simulate the crash: everything survives except tick_submitted, so
    # the early no-op return does not fire and the whole tick reprocesses.
    surviving = [e for e in journal.read(run)
                 if e["event"] != "tick_submitted"]
    journal.path_for(run).write_text(
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in surviving),
        encoding="utf-8")
    go(run, mem, 1)
    assert mem.read(stuck["id"])["attempts"] == 1


def test_a_declined_task_that_was_already_stale_stays_stale_on_requeue(
    run, mem, mktask
):
    """M4: _fail's normal action of requeuing to `pending` must not erase
    a `stale` flag a prior cascade already set on this task -- `stale`
    and `pending` are equally open for dispatch, so nothing about
    scheduling changes, but the provenance signal must survive."""
    stuck = mktask(question="verify", kind="verify", status="stale")
    mem.update(stuck["id"], inputs={"hypothesis": "H-999", "refutes": None})
    other = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [other["id"]])
    dispatch_skip(run, 1, stuck["id"], "hypothesis H-999 is dangling")
    write_artifact(run, other["id"], search_artifact(other["id"]))
    go(run, mem, 1)
    stored = mem.read(stuck["id"])
    assert stored["status"] == "stale"
    assert stored["attempts"] == 1


# --- coverage gap: KIND_ORDER is pinned, but is its *use* exercised? ---
#
# test_verify_artifacts_are_applied_after_evidence_artifacts above asserts
# on the KIND_ORDER constant directly, not through submit's behaviour, and
# every other test that dispatches an extract and a verify task together
# happens to create the extract task first, so its id already sorts first
# -- a plain `sorted(dispatched["task_ids"])` (no kind key at all) would
# pass every test in this file identically. This test is built so id order
# and kind order actively disagree: the verify task is created (and so
# gets the lower id) before the extract task, so a plain lexical sort
# would apply the verdict before the new evidence, not after. That
# divergence used to change the OUTCOME (see the test's own docstring for
# what it used to catch); now that gate 2 has moved out of apply_extract,
# it no longer does, and the test pins that it no longer does.

INDEPENDENT_QUOTE = ("Independent measurement confirms 42ms at p99 on an "
                     "unrelated deployment")


def test_a_same_tick_extract_cannot_rescue_a_verify_any_more(
    run, mem, mktask, mkcitation, mkfact, mkhypothesis
):
    """The old version of this test pinned KIND_ORDER's practical bite:
    apply_extract used to re-fetch and verify a citation inline, so
    running extract before verify in the same tick could flip a
    `pending` supporting citation to `verified` in time for the verify's
    own confidence check -- promoting a hypothesis that a plain id-order
    sort would have left at `proposed` forever (recompute_confidence is
    demote-only, see graph.py). Gate 2 has moved out of apply_extract
    entirely -- it is a `recheck` task's job now (Task 6) -- so a
    same-tick extract can, at best, leave the citation `pending`: kind
    order no longer has anything to buy here. Kept as a regression pin in
    the safe direction -- nothing, including a favourable dispatch order,
    promotes a hypothesis on a citation nobody has actually re-read."""
    root = mktask(question="root", kind="search")
    ids = []
    for index in range(2):
        citation = mkcitation(url=f"https://d{index}-example.com/x",
                              domain=f"d{index}-example.com", quote=f"a quoted span {index}",
                              status="verified", task=root["id"])
        mkfact(statement=f"f{index}", citations=[citation["id"]],
               task=root["id"])
        ids.append(citation["id"])
    stale_url = "https://d2-example.com/x"
    stale = mkcitation(url=stale_url, domain="d2-example.com",
                       quote=INDEPENDENT_QUOTE, status="pending",
                       task=root["id"])
    mem.update(stale["id"], quote_sha256=evidence.sha256_of(
        evidence.normalize(INDEPENDENT_QUOTE)))
    mkfact(statement="f2", citations=[stale["id"]], task=root["id"])
    ids.append(stale["id"])
    hypothesis = mkhypothesis(claim="c", supporting=ids, counter=[],
                              status="proposed", confidence=0.0, verdict=None,
                              task=root["id"])

    # Created in this order so the verify task's id sorts BEFORE the
    # extract task's id -- the opposite of KIND_ORDER's evidence-first
    # ranking, which is exactly what makes a plain id sort diverge from it.
    verifier = mktask(question="verify", kind="verify")
    mem.update(verifier["id"], inputs={"hypothesis": hypothesis["id"],
                                       "refutes": None})
    extractor = mktask(question="extract", kind="extract")
    mem.update(extractor["id"], inputs={"url": stale_url, "title": "T",
                                        "domain": "d2-example.com"})
    assert verifier["id"] < extractor["id"]

    dispatch(run, mem, 1, [verifier["id"], extractor["id"]])
    write_artifact(run, verifier["id"], {
        "task_id": verifier["id"], "hypothesis": hypothesis["id"],
        "verdict": "supported", "failing_citations": [],
        "reasoning": "clears the bar"})
    write_artifact(run, extractor["id"], {
        "task_id": extractor["id"], "url": stale_url,
        "facts": [{"statement": "independent confirmation",
                   "quote": INDEPENDENT_QUOTE}],
        "published_at": None,
        "source_type": "primary",
        "no_facts_reason": None})

    go(run, mem, 1)

    stored = mem.read(hypothesis["id"])
    assert stored["status"] == "proposed"
    # 2 citations on 2 domains: min(1, 2/3) * 2/(2+1) = 0.44
    assert stored["confidence"] == 0.44
    assert mem.read(stale["id"])["status"] == "pending"


# --- coverage gap: recheck's KIND_ORDER position, not just its shape --
#
# The two constant-shape assertions in
# test_verify_artifacts_are_applied_after_evidence_artifacts pin that
# `recheck` is a MEMBER of KIND_ORDER and sorts before `hypothesize`/
# `verify`, but neither one forces the sort key to actually be READ by
# submit's dispatch loop. Proven by mutation: replacing `order`'s
# `return (rank, task_id)` with `return (0, task_id)` at submit.py:300
# leaves the whole suite green without this test (see this test's own
# assertions below, run against that mutant, in the task-7 report). This
# test is built so id order and kind order actively disagree -- the
# verify task is created (and so gets the lower id) before the recheck
# task -- so a plain `sorted(task_ids)` would apply the stale verdict
# BEFORE the fresh re-check, not after.
#
# Position matters more here than for any earlier kind:
# Graph.recompute_confidence is demote-only (see graph.py), so
# apply_verify's `_verified_status` is the ONLY place `supported` is ever
# written, and it reads graph.supporting_domains at the moment IT runs,
# not at the end of the tick. If a `recheck` applies after the `verify`
# that depends on it, the citation verifies too late, the verdict is
# computed on the stale evidence, and recompute_confidence's later pass
# updates the NUMBER (it always recomputes) but cannot undo the STATUS --
# promotion is lost for good, not merely deferred a tick.

def test_a_recheck_landing_after_its_verify_in_id_order_is_still_applied_first(
    run, mem, mktask, mkcitation, mkfact, mkhypothesis
):
    """recheck must sort before hypothesize AND verify even though its
    task id sorts after the verify's."""
    extract = mktask(question="read the page", kind="extract", depth=2)
    domain_a = mkcitation(url="https://a-example.com/p", domain="a-example.com",
                          quote="a quoted span from domain a",
                          status="verified", task=extract["id"])
    domain_b = mkcitation(url="https://b-example.com/p", domain="b-example.com",
                          quote="a quoted span from domain b",
                          status="verified", task=extract["id"])
    stale_url = "https://c-example.com/p"
    stale_quote = "an independently confirmed span"
    stale = mkcitation(url=stale_url, domain="c-example.com",
                       quote=stale_quote, status="pending", task=extract["id"])
    mkfact(statement="a", citations=[domain_a["id"]], task=extract["id"])
    mkfact(statement="b", citations=[domain_b["id"]], task=extract["id"])
    mkfact(statement="c", citations=[stale["id"]], task=extract["id"])
    hypothesis = mkhypothesis(
        claim="c", supporting=[domain_a["id"], domain_b["id"], stale["id"]],
        status="proposed", confidence=0.0, verdict=None, task=extract["id"])

    # Created in this order so the verify task's id sorts BEFORE the
    # recheck task's id -- the opposite of KIND_ORDER's evidence-first
    # ranking, which is exactly what makes a plain id sort diverge from
    # it. Sorting by id alone would apply the verdict on stale evidence.
    verifier = mktask(question="verify", kind="verify", depth=2)
    mem.update(verifier["id"], inputs={"hypothesis": hypothesis["id"],
                                       "refutes": None})
    rechecker = mktask(question="re-read", kind="recheck", depth=2)
    mem.update(rechecker["id"], inputs={
        "url": stale_url, "quotes": [stale_quote], "citations": [stale["id"]]})
    assert verifier["id"] < rechecker["id"]

    dispatch(run, mem, 1, [verifier["id"], rechecker["id"]])
    write_artifact(run, verifier["id"], {
        "task_id": verifier["id"], "hypothesis": hypothesis["id"],
        "verdict": "supported", "failing_citations": [],
        "reasoning": "clears the bar"})
    write_artifact(run, rechecker["id"], {
        "task_id": rechecker["id"], "url": stale_url, "outcome": "read",
        "quotes": [{"index": 0, "present": True}], "note": None})

    go(run, mem, 1)

    assert mem.read(stale["id"])["status"] == gates.VERIFIED
    stored = mem.read(hypothesis["id"])
    assert stored["status"] == "supported"
    # 3 citations on 3 domains: min(1, 3/3) * 3/(3+1) = 0.75
    assert stored["confidence"] == 0.75


# --- C1: a crash between artifact_applied and the `done` write --------

def _drop_events(run, predicate):
    """Rewrite journal.jsonl without the records `predicate` selects.
    Stands in for the records a crash never got round to writing."""
    surviving = [e for e in journal.read(run) if not predicate(e)]
    journal.path_for(run).write_text(
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in surviving),
        encoding="utf-8")


def test_a_task_applied_in_a_crashed_pass_still_reaches_done(run, mem, mktask):
    """The crash window: `artifact_applied` is journaled, then the process
    dies before step 2's `done` write. The recovery re-run skips the task
    on the journal fast path — correctly, dedup is what makes recovery
    safe — and before the fix nothing else ever moved it. It stayed
    `running` forever: `frontier()` excludes `running`, so it is not
    dispatchable; `coverage_halt` sees a tick in flight, so it never
    fires; dependents never dispatch; fsck reports nothing, because
    `running` is a legal status. Reproduced at 18 of the 20 crash points
    in one ordinary tick.
    """
    task = mktask(question="find sources", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], search_artifact(task["id"]))

    # The crashed pass: it got as far as journaling the application.
    journal.append(run, "artifact_applied", tick=1, task=task["id"],
                   kind="search", created=[], reused=[], dropped=[],
                   spawned=[], cascaded=[], rejected_citations=[],
                   unverifiable_citations=[], reactivated_facts=[])
    assert mem.read(task["id"])["status"] == "running"

    report = go(run, mem, 1)
    assert report.skipped == [task["id"]]
    assert mem.read(task["id"])["status"] == "done"


def test_the_recovery_re_run_after_a_real_crash_finishes_the_task(
    run, mem, mktask, monkeypatch
):
    """The same property, driven through an actual crash rather than a
    hand-written journal: submit dies at the step-2 write, and the plain
    re-run — the documented recovery path — has to finish the job."""
    task = mktask(question="find sources", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], search_artifact(task["id"]))

    def boom(*args, **kwargs):
        raise RuntimeError("power cut")

    monkeypatch.setattr(submit_mod, "_finish", boom)
    with pytest.raises(RuntimeError):
        go(run, mem, 1)
    monkeypatch.undo()

    assert mem.read(task["id"])["status"] == "running"
    assert not journal.tick_submitted(journal.read(run), 1)

    go(run, mem, 1)
    assert mem.read(task["id"])["status"] == "done"
    assert journal.tick_submitted(journal.read(run), 1)


def test_a_recovered_task_that_a_cascade_staled_is_not_reset_to_done(
    run, mem, mktask
):
    """The guard the C1 fix must not break. `stale` outranks `done`: a
    task a cascade requeued because its output rested on a refuted
    premise must stay requeued, and the recovery path is exactly where
    that flag can be erased through the one door step 3 cannot see."""
    task = mktask(question="find sources", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], search_artifact(task["id"]))
    journal.append(run, "artifact_applied", tick=1, task=task["id"],
                   kind="search", created=[], reused=[], dropped=[],
                   spawned=[], cascaded=[], rejected_citations=[],
                   unverifiable_citations=[], reactivated_facts=[])
    mem.update(task["id"], status="stale")

    go(run, mem, 1)
    assert mem.read(task["id"])["status"] == "stale"


def test_a_recovered_task_that_was_abandoned_is_not_revived(run, mem, mktask):
    """`abandoned` is the deliberate terminal state after max_attempts.
    Graph.cascade refuses to revive one; so must this."""
    task = mktask(question="find sources", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], search_artifact(task["id"]))
    journal.append(run, "artifact_applied", tick=1, task=task["id"],
                   kind="search", created=[], reused=[], dropped=[],
                   spawned=[], cascaded=[], rejected_citations=[],
                   unverifiable_citations=[], reactivated_facts=[])
    mem.update(task["id"], status="abandoned",
               abandoned_reason="three rejected artifacts")

    go(run, mem, 1)
    assert mem.read(task["id"])["status"] == "abandoned"


def test_a_recovered_task_whose_file_went_schema_invalid_does_not_raise(
    run, mem, mktask
):
    """_finish is on the recovery path for a record nothing upstream has
    checked, and memory.update re-validates the merged record. A raise
    here is a bare traceback out of `research submit`, no
    `tick_submitted`, and every retry dying on the same line."""
    task = mktask(question="find sources", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    journal.append(run, "artifact_applied", tick=1, task=task["id"],
                   kind="search", created=[], reused=[], dropped=[],
                   spawned=[], cascaded=[], rejected_citations=[],
                   unverifiable_citations=[], reactivated_facts=[])
    path = mem.path_for(task["id"])
    path.write_text(
        "".join(line for line in path.read_text(encoding="utf-8").splitlines(True)
                if not line.startswith("depth:")),
        encoding="utf-8")

    go(run, mem, 1)   # must not raise
    assert journal.tick_submitted(journal.read(run), 1)


def test_a_cascade_from_a_crashed_pass_is_replayed_on_recovery(
    run, mem, mktask, mkhypothesis, mkassumption, mkfact
):
    """The other half of the fast path's cost. The applier's `cascaded`
    ids reach step 3 through the applier's return value, so skipping the
    applier skips the cascade too: a crash between `artifact_applied` and
    `run_cascades` left an assumption refuted-but-never-cascaded, and the
    work resting on it silently standing. They are replayed out of the
    journal record instead."""
    root = mktask(question="root", kind="decompose", status="done")
    doomed = mktask(question="rests on it", kind="search", parent=root["id"],
                    depth=1, status="done")
    assumption = mkassumption(statement="v3 is current", raised_by=root["id"],
                              status="refuted", blocks=[doomed["id"]])
    mem.update(assumption["id"], refuted_by=None)
    hypothesis = mkhypothesis(claim="v4 shipped", task=root["id"])
    verifier = mktask(question="verify", kind="verify")
    mem.update(verifier["id"], inputs={"hypothesis": hypothesis["id"],
                                       "refutes": assumption["id"]})
    dispatch(run, mem, 1, [verifier["id"]])

    # The crashed pass got as far as recording the application, with the
    # cascade it collected, and no further.
    journal.append(run, "artifact_applied", tick=1, task=verifier["id"],
                   kind="verify", created=[], reused=[], dropped=[],
                   spawned=[], cascaded=[assumption["id"]],
                   rejected_citations=[], unverifiable_citations=[],
                   reactivated_facts=[])

    report = go(run, mem, 1)
    assert report.cascaded == [assumption["id"]]
    assert mem.read(assumption["id"])["cascaded"] is True
    assert mem.read(doomed["id"])["status"] == "stale"


# --- C3: the per-tick path must not raise on a malformed journal ------

def test_a_dispatch_record_with_no_task_ids_does_not_wedge_submit(
    run, mem, mktask
):
    """journal.read() guarantees valid JSON and a dict, nothing about
    shape. Indexing `task_ids` raised KeyError before `tick_submitted`
    could land, so `next` reprinted the same in-flight tick and every
    submit died on the same line, forever."""
    task = mktask(question="q", kind="search")
    mem.update(task["id"], status="running")
    journal.append(run, "dispatched", tick=1, agents={}, models={})
    cfg = runconfig.load(run)
    cfg["status"]["tick"] = 1
    runconfig.save(run, cfg)

    report = go(run, mem, 1)   # must not raise
    assert report.applied == []
    assert journal.tick_submitted(journal.read(run), 1)


def test_a_dispatch_skipped_record_with_no_task_does_not_wedge_submit(
    run, mem, mktask
):
    """Step 6 read `record["task"]` unguarded, and it runs AFTER every
    artifact has been applied and every cascade has run — so the raise
    landed with the tick's real work committed and `tick_submitted`
    still unwritten."""
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], search_artifact(task["id"]))
    journal.append(run, "dispatch_skipped", tick=1, reason="no task field")

    report = go(run, mem, 1)   # must not raise
    assert report.applied == [task["id"]]
    assert journal.tick_submitted(journal.read(run), 1)


def test_an_artifact_applied_record_with_no_task_does_not_wedge_submit(
    run, mem, mktask
):
    task = mktask(question="q", kind="search")
    dispatch(run, mem, 1, [task["id"]])
    write_artifact(run, task["id"], search_artifact(task["id"]))
    journal.append(run, "artifact_applied", tick=1, kind="search")

    report = go(run, mem, 1)   # must not raise
    assert report.applied == [task["id"]]


# --- C4a: saturation is keyed on themes, produced by the real submit --

def test_the_saturation_branch_axis_comes_from_the_real_loop(run, mem, mktask):
    """The saturation tests all hand-wrote `root_branch` into the
    journal, so they pinned a state the system could not reach: `research
    init` seeds ONE task with `parent: None`, everything descends from
    it, and `Graph.root_branch` was therefore a constant function over
    any real run — `saturation_branches: 2` was unsatisfiable and
    saturation could never fire.

    This one produces the values through `submit` itself. Two themes
    under one seeded root, two dry completions, window 2: it halts.
    """
    cfg = runconfig.load(run)
    cfg["config"]["saturation_window"] = 2
    runconfig.save(run, cfg)

    root = mktask(question="the whole question", kind="decompose",
                  status="done")
    themes = [mktask(question=f"theme {i}", kind="search", parent=root["id"],
                     depth=1)["id"] for i in range(2)]
    # Real work outstanding, so coverage (checked first) does not fire and
    # this genuinely exercises saturation.
    mktask(question="still open", kind="search", parent=root["id"], depth=1)
    dispatch(run, mem, 1, themes)
    for task_id in themes:
        write_artifact(run, task_id, {"task_id": task_id, "sources": [],
                                      "queries": ["a search query"],
                                      "no_sources_reason": "nothing found"})

    report = go(run, mem, 1)
    branches = {c["root_branch"] for c in journal.completions(journal.read(run))}
    assert branches == set(themes)
    assert report.halted is not None
    assert report.halted.reason == "saturation"


# --- I1: promotion arithmetic has ONE source ---------------------------

def test_a_non_default_required_domains_is_honoured_end_to_end(
    run, mem, mktask, mkcitation, mkfact, mkhypothesis
):
    """INVERTED. `required_domains` left the arithmetic entirely, so
    there is no longer a score for the two to disagree ON — it is gate
    3's bar now, and test_gate_independence.py covers that.

    Kept rather than deleted because the disagreement it was written for
    is real and could recur on any value both sides read.

    Historical: Graph hardcoded required_domains=2 while
    apply._verified_status read the configured value. At required_domains 3 with two domains,
    gate 3 refuses the evidence and _verified_status scores 0.4 — but
    step 5's recompute, scoring against its own hardcoded 2, wrote 0.6
    and promoted. 0.6, computed against a bar nobody configured, is what
    persisted for predicates.min_hypothesis_confidence and render_status
    to read."""
    cfg = runconfig.load(run)
    cfg["config"]["required_domains"] = 3
    runconfig.save(run, cfg)

    root = mktask(question="root", kind="search")
    ids = []
    for index in range(2):
        citation = mkcitation(url=f"https://d{index}-example.com/x",
                              domain=f"d{index}-example.com",
                              quote=f"a quoted span {index}", task=root["id"])
        mkfact(statement=f"f{index}", citations=[citation["id"]],
               task=root["id"])
        ids.append(citation["id"])
    hypothesis = mkhypothesis(claim="two domains only", supporting=ids,
                              task=root["id"])
    verifier = mktask(question="verify", kind="verify")
    mem.update(verifier["id"], inputs={"hypothesis": hypothesis["id"],
                                       "refutes": None})
    dispatch(run, mem, 1, [verifier["id"]])
    write_artifact(run, verifier["id"], {
        "task_id": verifier["id"], "hypothesis": hypothesis["id"],
        "verdict": "supported", "failing_citations": [],
        "reasoning": "reads that way to me"})

    go(run, mem, 1)
    stored = mem.read(hypothesis["id"])
    # 2 citations on 2 domains: min(1, 2/3) * 2/(2+1) = 0.44,
    # and unchanged by required_domains now.
    assert stored["confidence"] == 0.44
    assert stored["status"] == "proposed"
