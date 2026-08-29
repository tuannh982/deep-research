"""The journal is state, not decoration. Tick idempotence, retry prompts
and the saturation predicate all read it back."""
import journal


def test_append_writes_one_line_per_event(tmp_path):
    journal.append(tmp_path, "dispatched", tick=1)
    journal.append(tmp_path, "tick_submitted", tick=1)
    raw = journal.path_for(tmp_path).read_text(encoding="utf-8")
    assert raw.count("\n") == 2
    assert not raw.endswith("\n\n")


def test_append_stamps_the_event_name_and_a_timestamp(tmp_path):
    record = journal.append(tmp_path, "dispatched", tick=3)
    assert record["event"] == "dispatched"
    assert record["tick"] == 3
    assert record["ts"].endswith("Z")


def test_read_returns_records_in_append_order(tmp_path):
    for tick in range(3):
        journal.append(tmp_path, "dispatched", tick=tick)
    assert [r["tick"] for r in journal.read(tmp_path)] == [0, 1, 2]


def test_read_of_a_missing_journal_is_empty(tmp_path):
    assert journal.read(tmp_path) == []


def test_append_creates_the_file_if_init_did_not(tmp_path):
    journal.append(tmp_path / "deep", "x")
    assert journal.path_for(tmp_path / "deep").is_file()


def test_a_torn_final_line_is_skipped_not_fatal(tmp_path):
    """A crash mid-append can leave a partial line. The reader of the
    audit log must not be the thing that crashes on it."""
    journal.append(tmp_path, "dispatched", tick=1)
    with journal.path_for(tmp_path).open("a", encoding="utf-8") as handle:
        handle.write('{"event": "dispatch')
    records = journal.read(tmp_path)
    assert [r["tick"] for r in records] == [1]


def test_a_blank_line_is_skipped(tmp_path):
    journal.append(tmp_path, "x", tick=1)
    with journal.path_for(tmp_path).open("a", encoding="utf-8") as handle:
        handle.write("\n\n")
    assert len(journal.read(tmp_path)) == 1


def test_a_non_object_line_is_skipped(tmp_path):
    journal.append(tmp_path, "x", tick=1)
    with journal.path_for(tmp_path).open("a", encoding="utf-8") as handle:
        handle.write("[1, 2, 3]\n")
    assert len(journal.read(tmp_path)) == 1


def test_non_ascii_survives_the_round_trip(tmp_path):
    journal.append(tmp_path, "artifact_rejected", error="café — 日本語")
    assert journal.read(tmp_path)[0]["error"] == "café — 日本語"


def test_a_newline_in_a_field_does_not_break_the_line_framing(tmp_path):
    """Validator errors are multi-line. One record must stay one line."""
    journal.append(tmp_path, "artifact_rejected", error="line one\nline two")
    raw = journal.path_for(tmp_path).read_text(encoding="utf-8")
    assert raw.count("\n") == 1
    assert journal.read(tmp_path)[0]["error"] == "line one\nline two"


# --- derived views ----------------------------------------------------

def test_dispatched_for_tick_finds_the_record(tmp_path):
    journal.append(tmp_path, "dispatched", tick=1, task_ids=["T-001"])
    journal.append(tmp_path, "dispatched", tick=2, task_ids=["T-002"])
    events = journal.read(tmp_path)
    assert journal.dispatched_for_tick(events, 2)["task_ids"] == ["T-002"]


def test_dispatched_for_tick_is_none_when_the_tick_never_ran(tmp_path):
    journal.append(tmp_path, "dispatched", tick=1, task_ids=[])
    assert journal.dispatched_for_tick(journal.read(tmp_path), 9) is None


def test_dispatched_for_tick_returns_the_latest_when_a_tick_repeats(tmp_path):
    """next is idempotent by reprinting, but a hand-edited or replayed
    journal must not silently resurrect an older dispatch set."""
    journal.append(tmp_path, "dispatched", tick=1, task_ids=["T-001"])
    journal.append(tmp_path, "dispatched", tick=1, task_ids=["T-002"])
    events = journal.read(tmp_path)
    assert journal.dispatched_for_tick(events, 1)["task_ids"] == ["T-002"]


def test_tick_submitted_is_false_before_and_true_after(tmp_path):
    journal.append(tmp_path, "dispatched", tick=4)
    assert not journal.tick_submitted(journal.read(tmp_path), 4)
    journal.append(tmp_path, "tick_submitted", tick=4)
    assert journal.tick_submitted(journal.read(tmp_path), 4)


def test_applied_tasks_is_scoped_to_one_tick(tmp_path):
    journal.append(tmp_path, "artifact_applied", tick=1, task="T-001")
    journal.append(tmp_path, "artifact_applied", tick=2, task="T-002")
    events = journal.read(tmp_path)
    assert journal.applied_tasks(events, 1) == {"T-001"}
    assert journal.applied_tasks(events, 2) == {"T-002"}


def test_applied_tasks_is_empty_for_an_untouched_tick(tmp_path):
    assert journal.applied_tasks(journal.read(tmp_path), 7) == set()


def test_last_rejection_returns_the_most_recent_error(tmp_path):
    journal.append(tmp_path, "artifact_rejected", tick=1, task="T-001",
                   error="first")
    journal.append(tmp_path, "artifact_rejected", tick=2, task="T-001",
                   error="second")
    events = journal.read(tmp_path)
    assert journal.last_rejection(events, "T-001") == "second"


def test_last_rejection_is_none_for_a_task_that_never_failed(tmp_path):
    journal.append(tmp_path, "artifact_rejected", tick=1, task="T-001",
                   error="e")
    assert journal.last_rejection(journal.read(tmp_path), "T-002") is None


def test_last_rejection_is_cleared_by_a_later_success(tmp_path):
    """Otherwise a task that failed once carries the stale validator
    error in its prompt for the rest of the run."""
    journal.append(tmp_path, "artifact_rejected", tick=1, task="T-001",
                   error="bad")
    journal.append(tmp_path, "artifact_applied", tick=2, task="T-001")
    assert journal.last_rejection(journal.read(tmp_path), "T-001") is None


def test_completions_returns_task_completed_records_in_order(tmp_path):
    journal.append(tmp_path, "dispatched", tick=1)
    journal.append(tmp_path, "task_completed", tick=1, task="T-001",
                   root_branch="T-001", new_facts=2, new_domains=1)
    journal.append(tmp_path, "task_completed", tick=2, task="T-002",
                   root_branch="T-001", new_facts=0, new_domains=0)
    completions = journal.completions(journal.read(tmp_path))
    assert [c["task"] for c in completions] == ["T-001", "T-002"]


def test_read_survives_truncated_multibyte_utf8_tail(tmp_path):
    """A crash mid-append can leave a partial multi-byte character. The
    reader must not die on UnicodeDecodeError — it should skip the torn
    line and return intact earlier records."""
    journal.append(tmp_path, "artifact_rejected", tick=1, task="T-001",
                   error="first")
    path = journal.path_for(tmp_path)
    raw = path.read_bytes()
    # Manually append a partial record with incomplete multi-byte UTF-8.
    # 日 is 0xe6 0x97 0xa5 in UTF-8; truncate 1 byte into it.
    partial = b'{"event":"artifact_rejected","tick":2,"error":"\xe6"}'
    path.write_bytes(raw + b'\n' + partial)
    # read() must not raise UnicodeDecodeError; it should skip the torn line
    records = journal.read(tmp_path)
    assert len(records) == 1
    assert records[0]["tick"] == 1


def test_append_rejects_ts_in_fields(tmp_path):
    """append() manages ts; callers cannot override it via **fields."""
    import pytest
    with pytest.raises(ValueError, match="ts"):
        journal.append(tmp_path, "dispatched", tick=1, ts="override")
