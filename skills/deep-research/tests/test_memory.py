import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import memory as memory_mod

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"


def test_create_allocates_an_id_and_timestamps(mktask):
    task = mktask(question="first")
    assert task["id"] == "T-001"
    assert task["created_at"] == task["updated_at"]
    assert task["created_at"].endswith("Z")


def test_create_increments_ids(mktask):
    assert mktask()["id"] == "T-001"
    assert mktask()["id"] == "T-002"


def test_read_round_trips_a_created_node(mem, mktask):
    created = mktask(question="what is X?")
    assert mem.read(created["id"]) == created


def test_node_lands_in_the_directory_for_its_type(mem, mktask):
    task = mktask()
    assert mem.path_for(task["id"]) == mem.root / "memory" / "tasks" / "T-001.md"
    assert mem.path_for(task["id"]).is_file()


def test_ids_are_sorted(mem, mktask):
    for _ in range(3):
        mktask()
    assert mem.ids("task") == ["T-001", "T-002", "T-003"]


def test_ids_is_empty_before_anything_is_written(mem):
    assert mem.ids("fact") == []


def test_create_rejects_a_node_that_fails_its_schema(mem):
    with pytest.raises(memory_mod.ValidationError):
        mem.create("task", {
            "question": "q", "status": "vibing", "depends_on": [],
            "parent": None, "depth": 0, "kind": "search", "attempts": 0,
            "provenance": {"task": None, "agent": "decomposer"},
        })


def test_a_rejected_create_writes_nothing(mem):
    with pytest.raises(memory_mod.ValidationError):
        mem.create("task", {
            "question": "q", "status": "vibing", "depends_on": [],
            "parent": None, "depth": 0, "kind": "search", "attempts": 0,
            "provenance": {"task": None, "agent": "decomposer"},
        })
    assert mem.ids("task") == []


def test_validation_error_names_the_offending_field(mem):
    with pytest.raises(memory_mod.ValidationError, match="status"):
        mem.create("task", {
            "question": "q", "status": "vibing", "depends_on": [],
            "parent": None, "depth": 0, "kind": "search", "attempts": 0,
            "provenance": {"task": None, "agent": "decomposer"},
        })


def test_update_changes_a_field_and_bumps_updated_at(mem, mktask):
    task = mktask()
    updated = mem.update(task["id"], status="done")
    assert updated["status"] == "done"
    assert updated["created_at"] == task["created_at"]
    assert mem.read(task["id"])["status"] == "done"


def test_update_rejects_an_invalid_change(mem, mktask):
    task = mktask()
    with pytest.raises(memory_mod.ValidationError):
        mem.update(task["id"], depth=-1)
    assert mem.read(task["id"])["depth"] == 0


def test_update_cannot_change_the_persisted_id(mem, mktask):
    task = mktask()
    updated = mem.update(task["id"], id="T-999")
    assert updated["id"] == task["id"]
    assert mem.read(task["id"])["id"] == task["id"]
    assert not mem.exists("T-999")


def test_update_cannot_change_the_persisted_type(mem, mktask):
    task = mktask()
    updated = mem.update(task["id"], type="fact")
    assert updated["type"] == "task"
    assert mem.read(task["id"])["type"] == "task"


def test_update_preserves_created_at_even_if_overwritten(mem, mktask):
    task = mktask()
    updated = mem.update(task["id"], created_at="2000-01-01T00:00:00Z")
    assert updated["created_at"] == task["created_at"]
    assert mem.read(task["id"])["created_at"] == task["created_at"]


def test_update_rejects_an_unrecognized_key(mem, mktask):
    task = mktask()
    with pytest.raises(memory_mod.ValidationError):
        mem.update(task["id"], surprise=1)
    assert mem.read(task["id"]) == task


def test_read_of_a_missing_node_raises_keyerror(mem):
    with pytest.raises(KeyError):
        mem.read("T-404")


def test_all_ids_spans_every_type(mem, mktask, mkcitation):
    mktask()
    mkcitation()
    assert set(mem.all_ids()) == {"T-001", "C-001"}


def test_temp_files_are_not_visible_as_nodes(mem, mktask):
    mktask()
    (mem.dir_for("task") / "T-999.md.tmp").write_text("junk")
    assert mem.ids("task") == ["T-001"]


def test_create_skips_an_id_whose_file_already_exists(mem, mktask):
    mktask()
    # Simulate a lost race: T-002 appears between allocation and write.
    (mem.dir_for("task") / "T-002.md").write_text("placeholder")
    assert mktask()["id"] == "T-003"


# --- final review 1: the disk layer must be byte-faithful ----------------
#
# nodes.dumps/loads round-trip perfectly in memory, but Memory wrote with
# Path.write_text() and read with Path.read_text(), and both of those
# default to universal-newline translation and the *locale* encoding. The
# byte-fidelity work landed in the serializer and never reached the disk
# layer; nothing tested the composition, so the seam went unnoticed.
#
# This is not cosmetic. citation.quote is verbatim web text that gets
# hashed into quote_sha256, and web text is full of \r\n. A quote silently
# rewritten to \n on the way back off disk no longer matches its own hash,
# so gate 2's substring/hash comparison fails against the store's own copy.

# Every body carries at least evidence.MIN_QUOTE_CHARS characters of
# content, because schemas/citation.json now refuses a quote too short to
# be evidence. The point of these cases is the \r handling, not the
# length, so they are padded rather than exempted.
CARRIAGE_RETURN_BODIES = {
    "bare_cr": "line one\rline two",
    "crlf": "line one\r\nline two",
    "mixed": "alpha\r\nbeta\ngamma\r\n",
    "trailing_cr": "line one here\r",
}


@pytest.mark.parametrize("case", sorted(CARRIAGE_RETURN_BODIES))
def test_a_body_with_carriage_returns_survives_a_write_read_round_trip(
    mem, mkcitation, case
):
    quote = CARRIAGE_RETURN_BODIES[case]
    created = mkcitation(quote=quote)
    assert created["quote"] == quote
    assert mem.read(created["id"])["quote"] == quote


@pytest.mark.parametrize("case", sorted(CARRIAGE_RETURN_BODIES))
def test_carriage_returns_are_present_in_the_bytes_on_disk(mem, mkcitation, case):
    """Guards the write half independently of the read half: on POSIX a
    read-only fix would make the round-trip test above pass while the file
    itself had been rewritten."""
    quote = CARRIAGE_RETURN_BODIES[case]
    created = mkcitation(quote=quote)
    raw = mem.path_for(created["id"]).read_bytes()
    assert raw.endswith(quote.encode("utf-8") + b"\n")


def test_a_non_ascii_body_round_trips_and_is_utf8_on_disk(mem, mkcitation):
    quote = "l'État — 42 °C — “smart quotes” — 日本語 — \U0001f600"
    created = mkcitation(quote=quote)
    assert mem.read(created["id"])["quote"] == quote
    raw = mem.path_for(created["id"]).read_bytes()
    assert quote.encode("utf-8") in raw


_LOCALE_PROBE = textwrap.dedent("""
    import sys
    sys.path.insert(0, {scripts!r})
    from memory import Memory

    quote = "an em\\u2014dash"
    mem = Memory({root!r}, schema_dir={schemas!r})
    created = mem.create("citation", {{
        "url": "https://example.com/a", "domain": "example.com", "title": "t",
        "quote": quote, "quote_sha256": "0" * 64, "status": "verified",
        "fetched_at": None, "http_status": None,
        "provenance": {{"task": None, "agent": "extractor"}},
    }})
    assert mem.read(created["id"])["quote"] == quote, "round trip lost the em-dash"
""")


def test_non_ascii_survives_under_a_non_utf8_locale(tmp_path):
    """Path.write_text()/read_text() encode with the *locale* encoding, so
    a store that works on a developer laptop raises UnicodeEncodeError on a
    machine (or CI container, or cron job) running under LC_ALL=C. Probed in
    a subprocess because the interpreter resolves its locale encoding once,
    at startup, from the C library — it cannot be monkeypatched in-process.
    """
    env = dict(os.environ)
    env.update(LC_ALL="C", LANG="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")
    env.pop("PYTHONIOENCODING", None)
    script = _LOCALE_PROBE.format(
        scripts=str(SCRIPTS), root=str(tmp_path / "research"), schemas=str(SCHEMAS)
    )
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


# --- final review 8a: sortedness must be tested, not coincidental --------
#
# "Every function returning a collection of ids returns it sorted" is the
# root guarantee under frontier(), all_ids(), fsck's finding order and
# every cascade result list. It had one test, and that test created
# T-001..T-003 in ascending order and asserted they came back ascending --
# which passes on any filesystem that returns directory entries in
# creation order, sorted() or not. Deleting the sorted() in Memory.ids left
# all 162 tests green.
#
# Two tests below. The first forces a known-hostile order through the one
# call Memory.ids makes, so it discriminates on every filesystem. The
# second uses the real directory, and skips rather than fails if the
# filesystem hands back sorted order on its own -- in that case the
# property still holds, the test simply cannot prove anything.

UNSORTED_IDS = ["T-010", "T-002", "T-100", "T-001", "T-020", "T-003"]


def _seed(directory, node_ids):
    directory.mkdir(parents=True, exist_ok=True)
    for node_id in node_ids:
        (directory / f"{node_id}.md").write_text("placeholder")


def test_ids_sorts_a_directory_listing_that_arrives_out_of_order(
    mem, monkeypatch
):
    """Pins the guarantee independently of how the filesystem happens to
    enumerate: whatever order the listing arrives in, ids() sorts it."""
    _seed(mem.dir_for("task"), UNSORTED_IDS)
    real_glob = Path.glob

    def descending_glob(self, pattern):
        return iter(sorted(real_glob(self, pattern), reverse=True))

    monkeypatch.setattr(Path, "glob", descending_glob)
    assert mem.ids("task") == sorted(UNSORTED_IDS)


def test_ids_are_sorted_when_the_files_were_not_created_in_order(mem):
    directory = mem.dir_for("task")
    _seed(directory, UNSORTED_IDS)
    if [p.stem for p in directory.glob("*.md")] == sorted(UNSORTED_IDS):
        pytest.skip("this filesystem pre-sorts; the test cannot discriminate")
    assert mem.ids("task") == sorted(UNSORTED_IDS)


def test_all_ids_is_sorted_across_types(mem):
    _seed(mem.dir_for("task"), ["T-010", "T-002"])
    _seed(mem.dir_for("fact"), ["F-010", "F-002"])
    _seed(mem.dir_for("citation"), ["C-010", "C-002"])
    assert mem.all_ids() == ["C-002", "C-010", "F-002", "F-010",
                             "T-002", "T-010"]


# --- final review 8b: atomicity was entirely unverified ------------------
#
# "All writes are atomic: temp file in the same directory, then os.replace"
# is a stated global constraint, and replacing _atomic_write's body with a
# plain path.write_text(text) also left all 162 tests green. The three
# tests below pin the three things that claim actually asserts: a failed
# swap does not touch the destination, a failed swap does not leave a
# half-made node behind, and the swap is a same-directory rename (os.replace
# is only atomic within one filesystem, so a temp file in /tmp would not be).


def _explode(*args, **kwargs):
    raise OSError("simulated failure between write and replace")


def test_a_failed_replace_leaves_the_previous_version_byte_identical(
    mem, mktask, monkeypatch
):
    task = mktask(question="the original question")
    path = mem.path_for(task["id"])
    before = path.read_bytes()

    with monkeypatch.context() as patch:
        patch.setattr(memory_mod.os, "replace", _explode)
        with pytest.raises(OSError):
            mem.update(task["id"], question="a clobbering rewrite")

    assert path.read_bytes() == before
    assert mem.read(task["id"])["question"] == "the original question"


def test_a_failed_replace_during_create_leaves_no_node_behind(
    mem, monkeypatch
):
    with monkeypatch.context() as patch:
        patch.setattr(memory_mod.os, "replace", _explode)
        with pytest.raises(OSError):
            mem.create("task", {
                "question": "q", "status": "pending", "depends_on": [],
                "parent": None, "depth": 0, "kind": "search", "attempts": 0,
                "provenance": {"task": None, "agent": "decomposer"},
            })

    assert mem.ids("task") == []
    assert not mem.path_for("T-001").exists()


def test_the_swap_is_a_rename_within_the_destination_directory(mem, mktask,
                                                               monkeypatch):
    """os.replace is atomic only within a single filesystem, so the temp
    file has to be a sibling of its destination, never in /tmp."""
    swaps = []
    real_replace = memory_mod.os.replace

    def spy(src, dst):
        swaps.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(memory_mod.os, "replace", spy)
        task = mktask()

    assert len(swaps) == 1
    source, destination = swaps[0]
    assert destination == mem.path_for(task["id"])
    assert source.parent == destination.parent
    assert source != destination
    assert not source.exists()  # consumed by the rename
