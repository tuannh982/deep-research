"""journal.jsonl — append-only audit log, and load-bearing loop state.

Spec section 8 asks for "every dispatch, gate result, and state
transition, so 'why is H-012 refuted' is answerable three days later".
Three mechanisms also read it back: tick idempotence (next must reprint
rather than re-dispatch after a compaction), retry prompts (the validator
error goes back into the next packet), and the saturation halt predicate
(the last N completions and their yield).

Deliberately not written through atomicio: rewriting the whole file per
event would be quadratic over a run with thousands of ticks. Appending one
line is the point of the format.

read() tolerates a damaged tail. A crash mid-append can leave a partial
line or a truncated multi-byte UTF-8 character, and the reader of the audit
log must not be the thing that dies on it — the same discipline fsck.py
follows for node files.
"""
import json
from pathlib import Path

import memory as memory_mod

FILENAME = "journal.jsonl"


def path_for(root):
    return Path(root) / FILENAME


def append(root, event, **fields):
    """Append one record. Returns it.

    Raises ValueError if fields contains 'ts' or 'event', which are
    managed by this function and must not be overwritten by the caller.
    """
    if "ts" in fields or "event" in fields:
        raise ValueError(
            "fields must not contain 'ts' or 'event'; these are "
            "managed by append()"
        )
    record = {"ts": memory_mod.utcnow(), "event": event, **fields}
    path = path_for(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # json.dumps never emits a raw newline (it escapes them), so one
    # record is always exactly one line even when a field holds a
    # multi-line validator error.
    line = json.dumps(record, sort_keys=True, ensure_ascii=False)
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(line + "\n")
    return record


def read(root):
    """Every intact record, in append order.

    Tolerates both damaged JSON and truncated multi-byte UTF-8. A crash
    mid-append can leave a partial line (JSONDecodeError) or a partial
    multi-byte character (UnicodeDecodeError), and this function survives
    both by decoding each line independently in binary mode.
    """
    path = path_for(root)
    if not path.is_file():
        return []
    records = []
    with path.open("rb") as handle:
        for raw in handle:
            try:
                line = raw.decode("utf-8").strip()
            except UnicodeDecodeError:
                continue  # torn multi-byte tail
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn tail, or a hand-edit
            if isinstance(record, dict):
                records.append(record)
    return records


def _matching(events, event, **match):
    return [
        record for record in events
        if record.get("event") == event
        and all(record.get(k) == v for k, v in match.items())
    ]


def dispatched_for_tick(events, tick):
    """The dispatch record for a tick, or None. Latest wins."""
    found = _matching(events, "dispatched", tick=tick)
    return found[-1] if found else None


def tick_submitted(events, tick):
    return bool(_matching(events, "tick_submitted", tick=tick))


def applied_records(events, tick):
    """{task id: the artifact_applied record} for this tick. Latest wins.

    Records with no `task` field are dropped rather than indexed: read()
    only guarantees a surviving record is valid JSON and a dict, not any
    particular shape (a hand-edit or an older format can be missing any
    key), and the reader of the audit log must not be the thing that
    dies on it — the same discipline read() itself follows for a damaged
    tail, and report._resume_run for 'task_ids'.
    """
    return {r["task"]: r
            for r in _matching(events, "artifact_applied", tick=tick)
            if isinstance(r.get("task"), str)}


def applied_tasks(events, tick):
    """Tasks whose artifact was already applied in this tick.

    submit's idempotence fast path. It is not the *correctness*
    guarantee — natural-key dedup in apply.py is, and it also covers a
    crash between the node writes and this record — but it is not
    behaviour-free either: a task on this list is not re-applied, so
    submit has to finish it by other means. See submit's own docstring.
    """
    return set(applied_records(events, tick))


def last_rejection(events, task_id):
    """The most recent validator error for a task, if it is still current.

    Cleared by a later success, so a task that failed once does not carry
    a stale error in its prompt for the rest of the run.
    """
    error = None
    for record in events:
        if record.get("task") != task_id:
            continue
        if record.get("event") == "artifact_rejected":
            error = record.get("error")
        elif record.get("event") == "artifact_applied":
            error = None
    return error


def completions(events):
    """task_completed records since the last resume, in order.

    The saturation window, and the `resumed` cut is what makes
    `research continue` able to clear a saturation halt at all.

    Saturation is not a stored flag: it is recomputed every `next` from
    this window. So clearing the halt in run.yaml changed nothing —
    `next` re-read the same dry completions and halted again, and no new
    completion could ever arrive because the halt is what stops anything
    being dispatched. A run with real work outstanding was stranded
    permanently, and the only exits were `research signal stop` or
    hand-editing the journal.

    Measured: a refutation in flight is a run of `recheck` and `verify`
    tasks, none of which yields a new fact or a new domain, so the window
    goes dry and fires with the counter-evidence still pending. The run
    then could not be resumed at all.

    `_continue_run`'s own comment about `stop_requested` reasons about
    exactly this shape — "leaving the flag set would halt again on the
    very next tick and make `continue` look broken" — and saturation had
    the same property with no remedy.
    """
    # Walked once, by position. Two `task_completed` records can be
    # byte-identical (a recovery re-run of the same tick), so anything
    # that located the cut by comparing record VALUES would drop the
    # wrong ones.
    cut = -1
    for index, record in enumerate(events):
        if record.get("event") == "resumed":
            cut = index
    return [record for index, record in enumerate(events)
            if index > cut and record.get("event") == "task_completed"]
