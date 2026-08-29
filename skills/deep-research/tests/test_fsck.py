import pytest

import fsck
from graph import Graph


@pytest.fixture
def g(mem):
    return Graph(mem, max_depth=4)


def messages(findings):
    return " | ".join(f"{f.severity}:{f.node}:{f.message}" for f in findings)


def test_a_healthy_graph_has_no_findings(mem, g, mktask, mkcitation, mkfact):
    task = mktask()
    citation = mkcitation()
    mkfact(citations=[citation["id"]], task=task["id"])
    assert fsck.check(mem, g) == []


def test_a_dangling_dependency_is_an_error(mem, g, mktask):
    mktask(depends_on=["T-999"])
    findings = fsck.errors(fsck.check(mem, g))
    assert "depends_on dangling: T-999" in messages(findings)


def test_a_dangling_parent_is_an_error(mem, g, mktask):
    mktask(parent="T-999")
    assert "parent dangling: T-999" in messages(fsck.check(mem, g))


def test_a_task_past_the_depth_cap_is_an_error(mem, g, mktask):
    mktask(depth=9)
    assert "exceeds cap" in messages(fsck.check(mem, g))


def test_a_fact_with_no_citations_is_an_error(mem, g, mkfact):
    mkfact(citations=[])
    assert "no citations" in messages(fsck.check(mem, g))


def test_a_fact_citing_a_missing_citation_is_an_error(mem, g, mkfact):
    mkfact(citations=["C-404"])
    assert "citation dangling: C-404" in messages(fsck.check(mem, g))


def test_an_assumption_with_a_missing_raiser_is_an_error(mem, g, mkassumption):
    mkassumption(raised_by="T-404")
    assert "raised_by dangling: T-404" in messages(fsck.check(mem, g))


def test_a_hypothesis_citing_a_missing_citation_is_an_error(mem, g, mkhypothesis):
    mkhypothesis(supporting=["C-404"])
    assert "citation dangling: C-404" in messages(fsck.check(mem, g))


def test_a_dependency_cycle_is_an_error(mem, g, mktask):
    first = mktask()
    second = mktask(depends_on=[first["id"]])
    mem.update(first["id"], depends_on=[second["id"]])
    assert "dependency cycle" in messages(fsck.check(mem, g))


def test_an_unreferenced_citation_is_a_warning_not_an_error(mem, g, mkcitation):
    mkcitation()
    findings = fsck.check(mem, g)
    assert [f.severity for f in findings] == ["warning"]
    assert fsck.errors(findings) == []


def test_a_node_whose_frontmatter_id_disagrees_with_its_filename_is_an_error(
    mem, g, mktask
):
    task = mktask()
    path = mem.path_for(task["id"])
    path.write_text(path.read_text().replace("id: T-001", "id: T-777"))
    assert "frontmatter id" in messages(fsck.check(mem, g))


def test_an_unparseable_node_file_is_an_error(mem, g, mktask):
    task = mktask()
    mem.path_for(task["id"]).write_text("not a node at all\n")
    assert "unparseable" in messages(fsck.check(mem, g))


def test_a_node_violating_its_schema_is_an_error(mem, g, mktask):
    task = mktask()
    path = mem.path_for(task["id"])
    path.write_text(path.read_text().replace("status: pending", "status: vibing"))
    assert "status" in messages(fsck.errors(fsck.check(mem, g)))


def test_findings_are_sorted_by_severity_then_node(mem, g, mktask, mkcitation):
    mkcitation()             # warning
    mktask(parent="T-999")   # error
    findings = fsck.check(mem, g)
    assert [f.severity for f in findings] == ["error", "warning"]


# --- carry-forwards -------------------------------------------------------
#
# (A) graph.find_cycle() only walks depends_on, so a cycle among `parent`
# pointers is invisible to fsck. Graph.root_branch() already raises
# CycleError on exactly this condition.
#
# (B) A fact whose provenance.task is null can never be invalidated by the
# cascade (`None in affected` is always False), so it is permanently immune
# to the correctness machinery. The schema rightly allows null there;
# fsck should flag it anyway.


def test_a_parent_cycle_is_an_error(mem, g, mktask):
    first = mktask()
    second = mktask(parent=first["id"], depth=1)
    mem.update(first["id"], parent=second["id"], depth=1)
    assert "parent cycle" in messages(fsck.check(mem, g))


def test_an_acyclic_parent_tree_has_no_cycle_finding(mem, g, mktask):
    root = mktask()
    mktask(parent=root["id"], depth=1)
    findings = fsck.check(mem, g)
    assert "parent cycle" not in messages(findings)


def test_a_fact_with_null_provenance_task_is_an_error(mem, g, mkfact):
    mkfact(task=None)
    assert "provenance.task is null" in messages(fsck.errors(fsck.check(mem, g)))


def test_a_fact_with_a_real_provenance_task_is_not_flagged_for_provenance(
    mem, g, mktask, mkfact, mkcitation
):
    task = mktask()
    citation = mkcitation()
    mkfact(task=task["id"], citations=[citation["id"]])
    findings = fsck.check(mem, g)
    assert "provenance.task is null" not in messages(findings)


# --- review round 1: schema-invalid node must not crash check() ----------
#
# A node that parses (valid YAML, valid `type`) but fails its JSON Schema
# because a required key was deleted outright previously got a validation
# Finding and then sailed past the early return, and every check after that
# indexes required keys directly (task["depends_on"], fact["citations"],
# etc.) — a straight KeyError. The fix treats a schema-invalid node as
# unchecked (skipped by the cross-reference and cycle checks, which already
# have their own validation Finding for it) rather than as a reason to
# discard every other finding in the store, which the early-return path
# would do.


def _strip_line(path, line_prefix):
    text = "".join(
        line for line in path.read_text().splitlines(keepends=True)
        if not line.startswith(line_prefix)
    )
    path.write_text(text)


def test_a_task_missing_a_required_key_does_not_crash_check(mem, g, mktask):
    task = mktask()
    _strip_line(mem.path_for(task["id"]), "depends_on:")
    findings = fsck.check(mem, g)  # must not raise
    assert "depends_on" in messages(fsck.errors(findings))


def test_a_fact_missing_a_required_key_does_not_crash_check(mem, g, mkfact):
    fact = mkfact()
    _strip_line(mem.path_for(fact["id"]), "citations:")
    findings = fsck.check(mem, g)  # must not raise
    assert "citations" in messages(fsck.errors(findings))


def test_a_malformed_task_does_not_suppress_findings_on_the_rest_of_the_graph(
    mem, g, mktask
):
    malformed = mktask()
    _strip_line(mem.path_for(malformed["id"]), "depends_on:")
    mktask(depends_on=["T-999"])  # a different, well-formed task
    findings = fsck.check(mem, g)
    assert "depends_on dangling: T-999" in messages(findings)


def test_a_malformed_task_causes_cycle_detection_to_be_skipped_with_a_warning(
    mem, g, mktask
):
    malformed = mktask()
    _strip_line(mem.path_for(malformed["id"]), "depends_on:")
    mktask(depends_on=["T-999"])
    findings = fsck.check(mem, g)
    assert "cycle detection skipped" in messages(findings)


# --- review round 2: the invalid-skip guards themselves must not assume
# the one key a schema-invalid node cannot guarantee: `id`. `id` is
# required in every schema, so stripping the `id:` line is exactly the
# "parses fine, fails validation on a missing required key" corruption
# this whole feature exists to survive — and the `if node["id"] in
# invalid` guards dereferenced that very key on freshly-read, unvalidated
# content. The fix iterates filename ids (memory.ids(type)) and reads
# inside the loop, so the only id ever trusted is the one on disk.


def test_a_task_missing_its_id_key_does_not_crash_check(mem, g, mktask):
    task = mktask()
    _strip_line(mem.path_for(task["id"]), "id:")
    findings = fsck.check(mem, g)  # must not raise
    assert "id" in messages(fsck.errors(findings))


def test_a_fact_missing_its_id_key_does_not_crash_check(mem, g, mkfact):
    fact = mkfact()
    _strip_line(mem.path_for(fact["id"]), "id:")
    findings = fsck.check(mem, g)  # must not raise
    assert "id" in messages(fsck.errors(findings))


def test_an_id_stripped_task_does_not_suppress_findings_on_the_rest_of_the_graph(
    mem, g, mktask
):
    malformed = mktask()
    _strip_line(mem.path_for(malformed["id"]), "id:")
    mktask(depends_on=["T-999"])  # a different, well-formed task
    findings = fsck.check(mem, g)
    assert "depends_on dangling: T-999" in messages(findings)


# --- final review 6: the cascade's own two edges were unchecked ----------
#
# Spec section 2 says fsck revalidates "all cross-references", but the two
# it skipped were the two the invalidation cascade actually walks:
#
#   provenance.task  the field Graph.cascade matches on to quarantine
#                    facts and demote hypotheses. Dangling means the node
#                    is provenanced to a task that will never be in the
#                    affected set, so it is silently under-invalidated in
#                    exactly the way the null-provenance check already
#                    guards against.
#   assumption.blocks the field that extends the affected set beyond the
#                    parent subtree.
#
# A store with all five dangling returned check(...) == [] -- a clean bill
# of health for a graph whose correctness machinery could not reach it.


def test_a_dangling_provenance_task_on_a_task_is_an_error(mem, g, mktask):
    mktask(task="T-900")
    assert "provenance.task dangling: T-900" in messages(fsck.errors(fsck.check(mem, g)))


def test_a_dangling_provenance_task_on_a_citation_is_an_error(mem, g, mkcitation):
    mkcitation(task="T-901")
    assert "provenance.task dangling: T-901" in messages(fsck.errors(fsck.check(mem, g)))


def test_a_dangling_provenance_task_on_a_fact_is_an_error(mem, g, mkfact):
    mkfact(task="T-902")
    assert "provenance.task dangling: T-902" in messages(fsck.errors(fsck.check(mem, g)))


def test_a_dangling_provenance_task_on_a_hypothesis_is_an_error(mem, g, mkhypothesis):
    mkhypothesis(task="T-903")
    assert "provenance.task dangling: T-903" in messages(fsck.errors(fsck.check(mem, g)))


def test_a_dangling_provenance_task_on_an_assumption_is_an_error(
    mem, g, mktask, mkassumption
):
    task = mktask()
    assumption = mkassumption(raised_by=task["id"])
    path = mem.path_for(assumption["id"])
    path.write_text(path.read_text().replace("task: T-001", "task: T-904"))
    assert "provenance.task dangling: T-904" in messages(fsck.errors(fsck.check(mem, g)))


def test_a_dangling_blocks_entry_is_an_error(mem, g, mktask, mkassumption):
    task = mktask()
    mkassumption(raised_by=task["id"], blocks=["T-888", "F-777"])
    found = messages(fsck.errors(fsck.check(mem, g)))
    assert "blocks dangling: T-888" in found
    assert "blocks dangling: F-777" in found


def test_a_blocks_entry_that_resolves_is_not_flagged(mem, g, mktask, mkassumption):
    task = mktask()
    other = mktask(question="blocked")
    mkassumption(raised_by=task["id"], blocks=[other["id"]])
    assert "blocks dangling" not in messages(fsck.check(mem, g))


def test_a_null_provenance_task_is_not_reported_as_dangling(mem, g, mktask):
    mktask(task=None)
    assert "provenance.task dangling" not in messages(fsck.check(mem, g))


def test_a_provenance_task_that_resolves_is_not_flagged(mem, g, mktask, mkcitation):
    task = mktask()
    mkcitation(task=task["id"])
    assert "provenance.task dangling" not in messages(fsck.check(mem, g))


def test_a_node_with_a_dangling_provenance_task_and_no_depth_key_is_reported(
    mem, g, mktask
):
    """Round 2 fix: `mktask()` defaults `provenance.task` to None, so a
    plain `mktask()` here has no dangling provenance at all -- two
    attempts at this test passed without ever exercising the provenance
    check's own invalid-skip guard. Give the task BOTH a dangling
    provenance.task AND a stripped required field (`depth`, not `id`,
    because stripping `id` trips the frontmatter-id guard first and this
    test would never reach the code it names): the schema error must
    still be reported, and the invalid-skip convention must suppress the
    `provenance.task dangling` finding for this same node, exactly as it
    already does for depends_on/parent/citations."""
    task = mktask(task="T-900")
    _strip_line(mem.path_for(task["id"]), "depth:")
    findings = fsck.check(mem, g)  # must not raise
    text = messages(findings)
    assert "depth" in text
    assert "provenance.task dangling" not in text
    # The malformed task is skipped by the cross-reference pass, not
    # allowed to suppress it.
    assert "cycle detection skipped" in text


# --- fix round 3 (Task 12): a stalled invalidation is now reportable ------
#
# apply.run_cascades sets an assumption's `cascaded` field only once
# graph.cascade() has actually run for it. "refuted" and "refuted and
# cascaded" are otherwise indistinguishable from the node alone, and no
# sweeper looks for the gap -- a normal tick only ever feeds run_cascades
# the ids collected during that same tick. fsck is the backstop: an
# operator repairing a schema defect on a refuted assumption now also
# learns that a cascade is owed.


def test_a_refuted_assumption_with_no_cascade_marker_is_an_error(
    mem, g, mktask, mkassumption
):
    task = mktask()
    mkassumption(raised_by=task["id"], status="refuted")
    assert "refuted but not cascaded" in messages(fsck.errors(fsck.check(mem, g)))


def test_a_refuted_and_cascaded_assumption_is_not_flagged(
    mem, g, mktask, mkassumption
):
    task = mktask()
    assumption = mkassumption(raised_by=task["id"], status="refuted")
    mem.update(assumption["id"], cascaded=True)
    assert "refuted but not cascaded" not in messages(fsck.check(mem, g))


def test_an_open_assumption_is_not_flagged_for_cascading(mem, g, mktask, mkassumption):
    task = mktask()
    mkassumption(raised_by=task["id"], status="open")
    assert "refuted but not cascaded" not in messages(fsck.check(mem, g))
