import pytest

import apply
import graph as graph_mod
import runconfig

SECTION = {"id": "S-001", "title": "Optical scattering", "hypotheses": [],
           "facts": [], "allowed_cite_keys": []}


@pytest.fixture
def writer(mem, mktask, mkcitation, tmp_path):
    citation = mkcitation(url="https://a-example.com/p",
                          domain="a-example.com",
                          quote="short wavelengths scatter")["id"]
    section = {**SECTION, "allowed_cite_keys": [citation]}
    # mktask has no `inputs=` keyword (see tests/conftest.py), so the
    # section payload is attached with a direct mem.update after creation,
    # mirroring tests/test_apply_outline.py's `seeded` fixture.
    task = mktask(question="write section S-001", kind="synthesize")
    task = mem.update(task["id"], inputs={"section": section})
    return {"mem": mem, "task": task, "citation": citation,
            "root": tmp_path / "research",
            "cfg": runconfig.default("why is the sky blue?")}


def _apply(writer, body, section="S-001"):
    artifact = {"task_id": writer["task"]["id"], "section": section,
                "body": body}
    return apply.apply_synthesize(
        writer["mem"], graph_mod.Graph(writer["mem"]), writer["cfg"],
        writer["task"]["id"], writer["task"], artifact, root=writer["root"])


def test_a_clean_body_is_written_to_sections(writer):
    body = ("Short wavelengths scatter more strongly in the atmosphere "
            "\\cite{%s}." % writer["citation"])
    _apply(writer, body)
    written = (writer["root"] / "sections" / "S-001.tex").read_text(
        encoding="utf-8")
    assert "\\cite{%s}" % writer["citation"] in written


def test_the_written_body_is_escaped(writer):
    # The literal "%" must be doubled to survive the `%` substitution below
    # unaltered -- an unescaped "40% over" reads as a space-flag "%o" (an
    # octal spec) to Python's own formatter and raises TypeError before the
    # test body under test ever runs.
    body = ("Costs fell 40%% over the period, which is a large move by any "
            "historical standard \\cite{%s}." % writer["citation"])
    _apply(writer, body)
    written = (writer["root"] / "sections" / "S-001.tex").read_text(
        encoding="utf-8")
    assert "40\\%" in written
    assert "40%" not in written.replace("40\\%", "")


def test_the_cite_command_survives_escaping(writer):
    """The whole trap in one assertion. If escape ran before gate 5, this
    file would contain \\textbackslash{}cite\\{...\\} and every citation in
    the report would be silently lost."""
    body = ("A claim about scattering in the upper atmosphere "
            "\\cite{%s}." % writer["citation"])
    _apply(writer, body)
    written = (writer["root"] / "sections" / "S-001.tex").read_text(
        encoding="utf-8")
    assert "\\textbackslash{}cite" not in written


def test_a_body_failing_gate_5_is_an_apply_error(writer):
    with pytest.raises(apply.ApplyError) as caught:
        _apply(writer, "An invented source is cited here \\cite{C-999}.")
    assert "C-999" in str(caught.value)


def test_a_failing_body_writes_no_section_file(writer):
    """A rejected artifact must leave nothing behind. A half-written
    sections/ is what render would silently pick up on the next run."""
    with pytest.raises(apply.ApplyError):
        _apply(writer, "An invented source is cited here \\cite{C-999}.")
    assert not (writer["root"] / "sections" / "S-001.tex").exists()


def test_answering_for_the_wrong_section_is_rejected(writer):
    """Without this, a synthesizer that echoed the wrong id would overwrite
    a sibling section's file with this section's prose."""
    with pytest.raises(apply.ApplyError, match="S-002"):
        _apply(writer, "Some perfectly acceptable prose here.",
               section="S-002")


def test_a_synthesize_task_with_no_section_is_an_apply_error(writer, mktask):
    task = mktask(question="write a section", kind="synthesize")
    with pytest.raises(apply.ApplyError, match="no section in its inputs"):
        apply.apply_synthesize(
            writer["mem"], graph_mod.Graph(writer["mem"]), writer["cfg"],
            task["id"], task,
            {"task_id": task["id"], "section": "S-001", "body": "x" * 60},
            root=writer["root"])


def test_reapplying_the_same_body_is_idempotent(writer):
    body = ("Short wavelengths scatter more strongly in the atmosphere "
            "\\cite{%s}." % writer["citation"])
    _apply(writer, body)
    first = (writer["root"] / "sections" / "S-001.tex").read_text(
        encoding="utf-8")
    _apply(writer, body)
    assert (writer["root"] / "sections" / "S-001.tex").read_text(
        encoding="utf-8") == first


def test_a_retry_overwrites_the_previous_body(writer):
    """The build-failure path re-opens the task and the new body replaces
    the old one. Appending would compile both."""
    first = "The first attempt at this section, which failed to build."
    second = "The second attempt at this section, which should replace it."
    _apply(writer, first)
    _apply(writer, second)
    written = (writer["root"] / "sections" / "S-001.tex").read_text(
        encoding="utf-8")
    assert "second attempt" in written and "first attempt" not in written
