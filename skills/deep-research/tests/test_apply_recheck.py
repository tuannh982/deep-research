"""Gate 2 itself: apply_recheck turns a rechecker's verdicts into citation
statuses and settles the facts resting on them.

Task 5 deleted 27 tests from test_apply_evidence.py that exercised the old
inline gate 2 (HTTP status handling, the fetcher lifecycle, the stored
page file). Most were genuinely obsolete. Fourteen encoded behaviour that
still holds and simply moved here; see the plan's relocated-coverage
table and task-6-report.md for the row-by-row account.
"""
import pytest

import apply
import gates
import graph as graph_mod
import nodes
import runconfig


@pytest.fixture
def rechecked(mem, mktask, mkcitation, mkfact):
    """An extract task, two pending citations, and the re-check task for
    them — the state apply_extract leaves behind."""
    extract = mktask(question="read the page", kind="extract", depth=2)["id"]
    first = mkcitation(url="https://a-example.com/p", domain="a-example.com",
                       quote="first verbatim span", status="pending",
                       task=extract)["id"]
    second = mkcitation(url="https://a-example.com/p", domain="a-example.com",
                        quote="second verbatim span", status="pending",
                        task=extract)["id"]
    fact_one = mkfact(statement="one", citations=[first], task=extract)["id"]
    fact_two = mkfact(statement="two", citations=[second], task=extract)["id"]
    task = mktask(question="re-read the page", kind="recheck", depth=2)
    mem.update(task["id"], inputs={
        "url": "https://a-example.com/p",
        "quotes": ["first verbatim span", "second verbatim span"],
        "citations": [first, second]})
    return {"mem": mem, "task": mem.read(task["id"]),
            "first": first, "second": second,
            "fact_one": fact_one, "fact_two": fact_two,
            "cfg": runconfig.default("q")}


def _apply(rechecked, **overrides):
    artifact = {"task_id": rechecked["task"]["id"],
                "url": "https://a-example.com/p", "outcome": "read",
                "quotes": [{"index": 0, "present": True},
                           {"index": 1, "present": True}], "note": ""}
    artifact.update(overrides)
    mem = rechecked["mem"]
    return apply.apply_recheck(mem, graph_mod.Graph(mem), rechecked["cfg"],
                               rechecked["task"]["id"], rechecked["task"],
                               artifact)


def test_a_present_quote_verifies_its_citation(rechecked):
    _apply(rechecked)
    assert rechecked["mem"].read(rechecked["first"])["status"] == gates.VERIFIED


def test_an_absent_quote_rejects_its_citation(rechecked):
    _apply(rechecked, quotes=[{"index": 0, "present": False},
                              {"index": 1, "present": True}])
    assert rechecked["mem"].read(rechecked["first"])["status"] == gates.REJECTED


def test_the_verdict_lands_on_the_citation_the_index_names(rechecked):
    """The whole index scheme rests on this. An off-by-one here verifies a
    span nobody checked and rejects one that was on the page."""
    _apply(rechecked, quotes=[{"index": 0, "present": False},
                              {"index": 1, "present": True}])
    mem = rechecked["mem"]
    assert mem.read(rechecked["first"])["status"] == gates.REJECTED
    assert mem.read(rechecked["second"])["status"] == gates.VERIFIED


def test_a_blocked_page_marks_every_citation_unverifiable(rechecked):
    """Spec section 6: a login wall is flagged, not silently trusted and
    not treated as disproof."""
    _apply(rechecked, outcome="blocked", quotes=[])
    mem = rechecked["mem"]
    for key in ("first", "second"):
        assert mem.read(rechecked[key])["status"] == gates.UNVERIFIABLE


def test_a_page_that_is_gone_rejects_every_citation(rechecked):
    _apply(rechecked, outcome="gone", quotes=[])
    assert rechecked["mem"].read(rechecked["first"])["status"] == gates.REJECTED


def test_a_rejected_citation_quarantines_the_fact_resting_on_it(rechecked):
    _apply(rechecked, quotes=[{"index": 0, "present": False},
                              {"index": 1, "present": True}])
    mem = rechecked["mem"]
    assert mem.read(rechecked["fact_one"])["status"] == "quarantined"
    assert mem.read(rechecked["fact_two"])["status"] == "active"


def test_a_verified_citation_reactivates_a_quarantined_fact(rechecked):
    """Without this the invalidation cascade is permanently sterilising:
    it stales a branch's extract tasks precisely so the work is redone,
    and redoing it would re-verify the citation and then leave the fact
    quarantined for ever. Measured once: 12 staled tasks, zero active
    facts."""
    mem = rechecked["mem"]
    mem.update(rechecked["fact_one"], status="quarantined")
    _apply(rechecked)
    assert mem.read(rechecked["fact_one"])["status"] == "active"


def test_an_unverifiable_recheck_does_not_reactivate(rechecked):
    """A 403 is the absence of verification, not verification. Reactivating
    on one would let a site that merely started rate-limiting undo a
    quarantine."""
    mem = rechecked["mem"]
    mem.update(rechecked["fact_one"], status="quarantined")
    _apply(rechecked, outcome="blocked", quotes=[])
    assert mem.read(rechecked["fact_one"])["status"] == "quarantined"


def test_a_missing_verdict_is_an_apply_error(rechecked):
    with pytest.raises(apply.ApplyError, match="no verdict"):
        _apply(rechecked, quotes=[{"index": 0, "present": True}])


def test_the_same_index_judged_twice_is_an_apply_error(rechecked):
    """The schema cannot catch this: `uniqueItems` compares whole objects
    and these two genuinely differ. Without an explicit check the dict
    comprehension in apply_recheck silently keeps the LAST value, so a
    rechecker that contradicted itself would have one arbitrary half of
    its answer applied with no error. Verified: the comprehension alone
    yields {0: False} for the pair below."""
    with pytest.raises(apply.ApplyError, match="more than once"):
        _apply(rechecked, quotes=[{"index": 0, "present": True},
                                  {"index": 0, "present": False},
                                  {"index": 1, "present": True}])


def test_an_out_of_range_index_is_an_apply_error(rechecked):
    with pytest.raises(apply.ApplyError, match="index"):
        _apply(rechecked, quotes=[{"index": 0, "present": True},
                                  {"index": 1, "present": True},
                                  {"index": 7, "present": True}])


def test_answering_for_the_wrong_page_is_rejected(rechecked):
    """Otherwise a rechecker that followed a redirect and read something
    else has its verdict applied to citations it never saw."""
    with pytest.raises(apply.ApplyError, match="b-example.com"):
        _apply(rechecked, url="https://b-example.com/other")


def test_re_applying_the_same_verdict_is_idempotent(rechecked):
    first = _apply(rechecked)
    second = _apply(rechecked)
    mem = rechecked["mem"]
    assert mem.read(rechecked["first"])["status"] == gates.VERIFIED
    assert second.created == [] and first.spawned == second.spawned


# --- relocated coverage the fixture above cannot exercise --------------
#
# `rechecked` gives every fact exactly one citation, so it cannot pin the
# "solely" rule (a fact survives if ANY of its citations is still good)
# or the "all gone" counterpart, and it cannot exercise a citation this
# pass never touches at all. `_recheck_scene` builds a page with however
# many pending citations a test needs, still wired into one recheck task
# addressed by index.

def _recheck_scene(mem, mktask, mkcitation, quotes, statuses=None):
    """One extract task, one pending citation per quote in `quotes` (or
    the given `statuses[i]`), and the recheck task carrying all of them —
    the same shape `rechecked` builds above, but letting a test choose
    how many citations exist and (later) which facts cite which."""
    extract = mktask(question="read the page", kind="extract", depth=2)["id"]
    statuses = statuses or ["pending"] * len(quotes)
    citations = [mkcitation(url="https://a-example.com/p",
                            domain="a-example.com", quote=quote,
                            status=status, task=extract)["id"]
                 for quote, status in zip(quotes, statuses)]
    task = mktask(question="re-read the page", kind="recheck", depth=2)
    mem.update(task["id"], inputs={
        "url": "https://a-example.com/p", "quotes": list(quotes),
        "citations": citations})
    return extract, citations, mem.read(task["id"])


def _make_schema_invalid(mem, node_id, field):
    """Delete a required field: the node still PARSES but no longer
    VALIDATES. Mirrors test_apply_evidence.py's own _delete_field —
    _citation_is_gone must treat this the same as REJECTED (its own
    docstring says so), and the only way to exercise that branch is a
    citation the recheck pass never writes to, since apply_recheck's own
    memory.update on a citation it DOES touch would raise out of a merge
    that re-validates the whole record."""
    path = mem.path_for(node_id)
    data = nodes.loads(path.read_text(encoding="utf-8"))
    del data[field]
    path.write_text(nodes.dumps(data), encoding="utf-8")


def test_a_fact_resting_on_two_citations_where_only_one_is_rejected_stays_active(
    mem, mktask, mkcitation, mkfact
):
    """The 'solely' rule: spec section 6 rejects a fact resting SOLELY on
    a failed citation. `rechecked`'s facts each cite one span apiece,
    which cannot tell 'quarantines regardless' from 'quarantines only
    when nothing else is left' — this fact cites both spans on the page,
    and only one dies."""
    extract, (first, second), task = _recheck_scene(
        mem, mktask, mkcitation,
        ["first verbatim span", "second verbatim span"])
    fact = mkfact(statement="both", citations=[first, second],
                 task=extract)["id"]
    artifact = {"task_id": task["id"], "url": "https://a-example.com/p",
                "outcome": "read",
                "quotes": [{"index": 0, "present": False},
                           {"index": 1, "present": True}], "note": ""}
    apply.apply_recheck(mem, graph_mod.Graph(mem), runconfig.default("q"),
                        task["id"], task, artifact)
    assert mem.read(first)["status"] == gates.REJECTED
    assert mem.read(second)["status"] == gates.VERIFIED
    assert mem.read(fact)["status"] == "active"


def test_a_fact_resting_on_two_now_rejected_citations_is_quarantined(
    mem, mktask, mkcitation, mkfact
):
    """...and the counterpart: once every citation a fact cites is gone,
    the 'solely' rule no longer saves it."""
    extract, (first, second), task = _recheck_scene(
        mem, mktask, mkcitation,
        ["first verbatim span", "second verbatim span"])
    fact = mkfact(statement="both", citations=[first, second],
                 task=extract)["id"]
    artifact = {"task_id": task["id"], "url": "https://a-example.com/p",
                "outcome": "read",
                "quotes": [{"index": 0, "present": False},
                           {"index": 1, "present": False}], "note": ""}
    apply.apply_recheck(mem, graph_mod.Graph(mem), runconfig.default("q"),
                        task["id"], task, artifact)
    assert mem.read(fact)["status"] == "quarantined"


def test_a_fact_resting_on_a_not_rejected_citation_stays_active(
    mem, mktask, mkcitation, mkfact
):
    """_citation_is_gone must key on `status == REJECTED`, not
    `status != VERIFIED`: a citation this recheck never touches is still
    `pending`, not gone, and a fact citing it alongside a citation this
    pass DOES reject must survive on it."""
    extract, (first,), task = _recheck_scene(
        mem, mktask, mkcitation, ["first verbatim span"])
    untouched = mkcitation(url="https://a-example.com/other",
                           domain="a-example.com", quote="an untouched span",
                           status="pending", task=extract)["id"]
    fact = mkfact(statement="both", citations=[first, untouched],
                 task=extract)["id"]
    artifact = {"task_id": task["id"], "url": "https://a-example.com/p",
                "outcome": "read",
                "quotes": [{"index": 0, "present": False}], "note": ""}
    apply.apply_recheck(mem, graph_mod.Graph(mem), runconfig.default("q"),
                        task["id"], task, artifact)
    assert mem.read(first)["status"] == gates.REJECTED
    assert mem.read(untouched)["status"] == "pending"
    assert mem.read(fact)["status"] == "active"


def test_a_fact_is_quarantined_when_its_citation_goes_schema_invalid_then_rejected(
    mem, mktask, mkcitation, mkfact
):
    """_citation_is_gone treats an unreadable citation the same as a
    rejected one: a citation whose file has lost a required field cannot
    vouch for anything, so a fact resting on it plus a citation this pass
    actually rejects has no live evidence left at all."""
    extract, (first,), task = _recheck_scene(
        mem, mktask, mkcitation, ["first verbatim span"])
    corrupted = mkcitation(url="https://a-example.com/other",
                           domain="a-example.com", quote="a corrupted span",
                           status="pending", task=extract)["id"]
    _make_schema_invalid(mem, corrupted, "domain")
    fact = mkfact(statement="both", citations=[first, corrupted],
                 task=extract)["id"]
    artifact = {"task_id": task["id"], "url": "https://a-example.com/p",
                "outcome": "read",
                "quotes": [{"index": 0, "present": False}], "note": ""}
    apply.apply_recheck(mem, graph_mod.Graph(mem), runconfig.default("q"),
                        task["id"], task, artifact)
    assert mem.read(fact)["status"] == "quarantined"


def test_an_unverifiable_citation_scores_nothing(rechecked, mkhypothesis):
    """Spec section 6's flag-not-trust rule has teeth: gate 3 must not
    count a citation apply_recheck marked unverifiable. Graph-level
    coverage of the filtering rule itself already exists in
    test_graph_cascade.py; this pins that apply_recheck's own `blocked`
    outcome actually produces the status that filtering rests on.

    Started `verified`, not `pending`: `supporting_domains` returns []
    for `pending` too, so a `pending` starting point would make this
    assertion hold before apply_recheck ever runs, and it would survive a
    mutation turning the applier into a complete no-op for non-`read`
    outcomes. Starting `verified` forces the test to observe an actual
    verified -> unverifiable transition."""
    mem = rechecked["mem"]
    mem.update(rechecked["first"], status=gates.VERIFIED)
    hypothesis = mkhypothesis(supporting=[rechecked["first"]],
                              verdict="supported")
    domains_before = graph_mod.Graph(mem).supporting_domains(hypothesis["id"])
    assert domains_before == ["a-example.com"]
    _apply(rechecked, outcome="blocked", quotes=[])
    domains = graph_mod.Graph(mem).supporting_domains(hypothesis["id"])
    assert domains == []


def test_a_previously_unverifiable_citation_is_upgraded_on_a_later_pass(
    rechecked
):
    """A site that was rate-limiting yesterday can verify today: gate 2
    must not treat a prior `unverifiable` as a terminal state."""
    mem = rechecked["mem"]
    mem.update(rechecked["first"], status=gates.UNVERIFIABLE)
    _apply(rechecked)
    assert mem.read(rechecked["first"])["status"] == gates.VERIFIED


def test_a_403_leaves_an_unverifiable_citation_and_keeps_the_fact(rechecked):
    """The row test_a_blocked_page_marks_every_citation_unverifiable above
    only checks the citation half. `outcome: blocked` must never enter
    the quarantine branch in _settle_facts at all: `unverifiable` is not
    in the `rejected` set that branch tests against, so an ACTIVE fact
    resting on a citation that merely could not be re-read must stay
    active, not be swept into quarantine alongside a genuine rejection."""
    mem = rechecked["mem"]
    _apply(rechecked, outcome="blocked", quotes=[])
    assert mem.read(rechecked["fact_one"])["status"] == "active"
    assert mem.read(rechecked["fact_two"])["status"] == "active"


def test_a_404_rejects_the_citation_and_its_fact(rechecked):
    """test_a_page_that_is_gone_rejects_every_citation above only checks
    the citation half. `outcome: gone` rejects every citation on the
    page, and _settle_facts must still quarantine the fact resting
    (solely) on one."""
    mem = rechecked["mem"]
    _apply(rechecked, outcome="gone", quotes=[])
    assert mem.read(rechecked["first"])["status"] == gates.REJECTED
    assert mem.read(rechecked["fact_one"])["status"] == "quarantined"


def test_the_cascade_and_the_redo_together_restore_the_branch(
    mem, tmp_path, mktask, mkassumption
):
    """Integration, not unit: the invalidation cascade stales a branch's
    extract tasks precisely so the work is redone, and the redo has two
    halves. Redoing the extract alone lands back on the same citation and
    fact by natural key and revives neither of them — pinned at the unit
    level by
    test_redoing_the_extract_alone_does_not_resurrect_a_quarantined_fact
    in test_apply_evidence.py, which asserts that as what does NOT
    happen. What was missing before this task's reactivation existed was
    the SECOND half: the re-seeded recheck task actually being applied.
    Measured once: 12 staled extract tasks, redone, produced zero active
    facts, because nothing ever completed that second half."""
    cfg = runconfig.default("q")
    root = mktask(question="root", kind="decompose", status="done")
    reader = mktask(question="read it", kind="extract", parent=root["id"],
                    depth=1, status="done")
    extract_artifact = {
        "task_id": reader["id"], "url": "https://a-example.com/p",
        "facts": [{"statement": "one", "quote": "first verbatim span"}],
        "published_at": None,
        "source_type": "primary",
        "no_facts_reason": None,
    }
    apply.apply_extract(mem, graph_mod.Graph(mem), cfg, reader["id"],
                        reader, extract_artifact, root=tmp_path)
    fact_id = mem.ids("fact")[0]
    recheck = next(t for t in mem.list("task") if t["kind"] == "recheck")
    citation_id = recheck["inputs"]["citations"][0]

    # The re-check has already run once, as it has in the real sequence
    # this test is named for: the citation was verified, the fact was
    # active, and only THEN was the assumption underneath refuted. Its
    # attempts are spent, which is what makes the reset below load-bearing.
    mem.update(recheck["id"], status="done", attempts=2)

    assumption = mkassumption(raised_by=root["id"], status="refuted")
    graph_mod.Graph(mem).cascade(assumption["id"])
    assert mem.read(fact_id)["status"] == "quarantined"
    assert mem.read(reader["id"])["status"] == "stale"
    # The half this test is named for but never checked. The whole redo
    # turns on the cascade re-queueing the RE-CHECK as well as the
    # extract: `done` is in graph.STALEABLE_TASK_STATUSES, so a re-check
    # that already landed goes back to `stale` with its attempts reset and
    # is dispatched again. Narrow that tuple and every assertion below
    # still passes -- the artifact is hand-applied here -- while a real
    # run reproduces "12 staled tasks, zero active facts", because nothing
    # would ever dispatch the second half.
    staled = mem.read(recheck["id"])
    assert staled["status"] == "stale"
    assert staled["attempts"] == 0
    assert recheck["id"] in graph_mod.Graph(mem).frontier()

    # First half of the redo: apply_extract again. Dedup by natural key
    # lands on the same citation and the same fact -- neither is revived.
    apply.apply_extract(mem, graph_mod.Graph(mem), cfg, reader["id"],
                        mem.read(reader["id"]), extract_artifact,
                        root=tmp_path)
    assert mem.read(fact_id)["status"] == "quarantined"

    # Second half: the recheck task is applied and gate 2 verifies the
    # quote again. This is the half that used to be missing.
    recheck = mem.read(recheck["id"])
    apply.apply_recheck(mem, graph_mod.Graph(mem), cfg, recheck["id"],
                        recheck, {
                            "task_id": recheck["id"],
                            "url": "https://a-example.com/p",
                            "outcome": "read",
                            "quotes": [{"index": 0, "present": True}],
                            "note": "",
                        })
    assert mem.read(citation_id)["status"] == gates.VERIFIED
    assert mem.read(fact_id)["status"] == "active"


def test_a_schema_invalid_target_citation_is_dropped_not_fatal(rechecked):
    """memory.update reads the stored node, merges, validates and writes.
    A citation that is one of THIS re-check's own targets but whose file
    has gone schema-invalid (a hand edit, or damage from some unrelated
    bug) makes that validate() raise -- not an ApplyError, so it would
    escape submit's per-artifact guard, take down the whole tick, and die
    identically on every retry because the file on disk never changes.
    fsck is what reports a corrupt node; a gate must not be the thing
    that dies on one. The other citation in the same re-check must still
    get its verdict."""
    mem = rechecked["mem"]
    # schemas/citation.json sets additionalProperties: false, so an extra
    # key invalidates the file while it still parses -- same technique as
    # test_outline.py's test_a_schema_invalid_theme_task_does_not_crash_the_outline.
    path = mem.path_for(rechecked["second"])
    path.write_text(path.read_text(encoding="utf-8").replace(
        "type: citation", "type: citation\nbogus_field: 1"),
        encoding="utf-8")

    result = _apply(rechecked)

    assert mem.read(rechecked["first"])["status"] == gates.VERIFIED
    assert any(what == "citation" and rechecked["second"] in why
              for what, why in result.dropped)


def test_a_later_pass_can_downgrade_an_already_verified_citation(rechecked):
    """Relocated-coverage row 6: 'a re-check can downgrade a fact an
    earlier tick made active.' Every other test in this file starts its
    citation at the birth status apply_extract gives it, `pending` -- so
    none of them exercise a SECOND re-check pass reversing a verdict an
    EARLIER pass already landed. Proven load-bearing: a one-line
    monotonicity guard

        if memory.read(citation_id)["status"] == gates.VERIFIED:
            continue

    dropped in front of the citation update leaves the rest of this file
    green, because nothing else ever hands apply_recheck a citation that
    starts `verified`."""
    mem = rechecked["mem"]
    mem.update(rechecked["first"], status=gates.VERIFIED)
    _apply(rechecked, quotes=[{"index": 0, "present": False},
                              {"index": 1, "present": True}])
    assert mem.read(rechecked["first"])["status"] == gates.REJECTED
    assert mem.read(rechecked["fact_one"])["status"] == "quarantined"


def test_a_fact_is_not_reactivated_on_a_verdict_that_did_not_persist(
    rechecked
):
    """A quarantined fact must move on what is actually true on disk, not
    on what this pass MEANT to write. If the citation update is dropped
    (here: the file went schema-invalid between the extract and this
    re-check), the raw file never changes -- reactivating the fact anyway
    would move it to `active` while its only citation is still whatever
    it was before, or gone outright through the KeyError branch. Measured
    by the reviewer before this test existed: dropped == [('citation',
    'C-002 no longer exists or is unreadable')], reactivated ==
    ['F-002'], and the citation's own file was still `pending` on disk."""
    mem = rechecked["mem"]
    mem.update(rechecked["fact_two"], status="quarantined")
    path = mem.path_for(rechecked["second"])
    path.write_text(path.read_text(encoding="utf-8").replace(
        "type: citation", "type: citation\nbogus_field: 1"),
        encoding="utf-8")

    result = _apply(rechecked)

    assert any(what == "citation" and rechecked["second"] in why
              for what, why in result.dropped)
    assert rechecked["fact_two"] not in result.reactivated_facts
    assert mem.read(rechecked["fact_two"])["status"] == "quarantined"
