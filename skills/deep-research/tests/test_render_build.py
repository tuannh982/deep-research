import shutil

import pytest

import graph as graph_mod
import render
import stubs

from test_render_assemble import built  # noqa: F401  — reuse the fixture


def test_a_successful_build_lands_a_pdf(built):
    result = render.build(built["root"], built["graph"], built["cfg"],
                          run=stubs.tectonic_stub(), today="2026-08-22")
    assert result.ok
    assert result.pdf.read_bytes().startswith(b"%PDF-")


def test_a_successful_build_also_leaves_the_tex(built):
    """report.tex is the artifact a human edits when the build is wrong."""
    result = render.build(built["root"], built["graph"], built["cfg"],
                          run=stubs.tectonic_stub(), today="2026-08-22")
    assert result.tex.read_text(encoding="utf-8").startswith("% Template")


def test_exit_zero_with_no_pdf_written_is_still_a_failure(built):
    """tectonic can report success and still leave nothing behind — the
    caller's contract is a PDF, not an exit code. Carried over from the
    Task 14 review: this branch (render.build's `pdf.is_file()` check
    after a 0 return code) had no test."""
    def no_pdf_stub(tex_path, out_dir):
        return stubs.CompletedStub(0)

    result = render.build(built["root"], built["graph"], built["cfg"],
                          run=no_pdf_stub, today="2026-08-22")
    assert result.ok is False
    assert "wrote no PDF" in result.error


def test_a_failed_build_still_emits_the_tex(built):
    """Spec section 7: 'the run emits report.tex plus a build report rather
    than nothing'."""
    result = render.build(built["root"], built["graph"], built["cfg"],
                          run=stubs.tectonic_stub(fail=True),
                          today="2026-08-22")
    assert not result.ok
    assert result.tex.is_file()
    assert result.pdf is None


def test_a_failed_build_writes_a_build_report(built):
    render.build(built["root"], built["graph"], built["cfg"],
                 run=stubs.tectonic_stub(fail=True), today="2026-08-22")
    report = (built["root"] / "out" / "build-report.md").read_text(
        encoding="utf-8")
    assert "Undefined control sequence" in report


def test_the_build_report_names_the_offending_line(built):
    result = render.build(built["root"], built["graph"], built["cfg"],
                          run=stubs.tectonic_stub(fail=True),
                          today="2026-08-22")
    assert "42" in result.offending_line


def test_a_failed_build_reopens_the_offending_section(mem, built, mktask):
    """The retry path. render finds the synthesize task for the section the
    error points at and marks it stale with the error attached."""
    # mktask has no inputs= keyword, so the section payload is attached via
    # a follow-up mem.update rather than at creation time.
    task = mktask(question="write section S-001", kind="synthesize",
                  status="done")["id"]
    mem.update(task, inputs={"section": {"id": "S-001", "title": "t",
                                         "hypotheses": [], "facts": [],
                                         "allowed_cite_keys": []}})
    reopened = render.reopen_section(mem, built["root"], "S-001",
                                     "Undefined control sequence")
    assert reopened == task
    after = mem.read(task)
    assert after["status"] == "stale"
    assert "Undefined control sequence" in after["inputs"]["build_error"]


def test_reopening_preserves_the_frozen_section(mem, built, mktask):
    """The section payload is what the retry writes from. Losing it would
    make the retry a blank dispatch."""
    task = mktask(question="write section S-001", kind="synthesize",
                  status="done")["id"]
    mem.update(task, inputs={"section": {"id": "S-001", "title": "Optics",
                                         "hypotheses": [], "facts": [],
                                         "allowed_cite_keys": ["C-001"]}})
    render.reopen_section(mem, built["root"], "S-001", "boom")
    assert mem.read(task)["inputs"]["section"]["allowed_cite_keys"] == ["C-001"]


def test_reopening_does_not_create_a_second_task(mem, built, mktask):
    """TASK_KEY includes canonical(inputs), so routing this through
    create_task would key on the changed inputs and seed a SECOND writer
    for the same section — two bodies competing for one .tex file."""
    task = mktask(question="write section S-001", kind="synthesize",
                  status="done")["id"]
    mem.update(task, inputs={"section": {"id": "S-001", "title": "t",
                                         "hypotheses": [], "facts": [],
                                         "allowed_cite_keys": []}})
    before = len(mem.ids("task"))
    render.reopen_section(mem, built["root"], "S-001", "boom")
    assert len(mem.ids("task")) == before


def test_reopening_an_unknown_section_reports_rather_than_raises(mem, built):
    assert render.reopen_section(mem, built["root"], "S-404", "boom") is None


@pytest.mark.real_tectonic
@pytest.mark.skipif(shutil.which("tectonic") is None,
                    reason="tectonic is not installed")
def test_the_generated_latex_actually_compiles(built):
    """The only test that shells out. tectonic 0.17.0 is installed here,
    so this RUNS; it skips only on a machine without the tool. The template
    was compiled by hand before this plan was written, so a failure here is
    a regression in what the implementer changed, not a surprise."""
    result = render.build(built["root"], built["graph"], built["cfg"],
                          today="2026-08-22")
    assert result.ok, result.error
    assert result.pdf.read_bytes().startswith(b"%PDF-")
