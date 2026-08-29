import pytest

import confidence
from graph import Graph


def _strip_line(path, line_prefix):
    path.write_text("".join(
        line for line in path.read_text().splitlines(keepends=True)
        if not line.startswith(line_prefix)
    ))


@pytest.fixture
def scenario(mem, mktask, mkcitation, mkfact, mkassumption, mkhypothesis):
    """A pruned branch resting on one assumption, plus an untouched branch.

    T-001 raises A-001, and has children T-002 (done) and T-003 (done).
    F-001 comes from T-002 and cites C-001 (evil.com).
    F-002 comes from T-004, an unrelated root, and cites C-002 (good.com).
    H-001 rests on both citations.
    """
    root = mktask(question="root")                              # T-001
    child = mktask(parent=root["id"], depth=1, status="done")   # T-002
    mktask(parent=root["id"], depth=1, status="done")           # T-003
    other = mktask(question="unrelated root")                   # T-004
    tainted = mkcitation(domain="evil.com")                     # C-001
    clean = mkcitation(domain="good.com")                       # C-002
    mkfact(statement="tainted", citations=[tainted["id"]],
           task=child["id"])                                    # F-001
    mkfact(statement="clean", citations=[clean["id"]],
           task=other["id"])                                    # F-002
    assumption = mkassumption(raised_by=root["id"])             # A-001
    hypothesis = mkhypothesis(supporting=[tainted["id"], clean["id"]],
                              status="supported", confidence=0.5,
                              verdict="supported", task=root["id"])  # H-001
    return {"root": root, "child": child, "other": other,
            "assumption": assumption, "hypothesis": hypothesis}


def refute(mem, scenario):
    mem.update(scenario["assumption"]["id"], status="refuted", refuted_by="H-001")
    return Graph(mem).cascade(scenario["assumption"]["id"])


def test_cascade_refuses_an_assumption_that_is_not_refuted(mem, scenario):
    with pytest.raises(ValueError, match="open"):
        Graph(mem).cascade(scenario["assumption"]["id"])


def test_completed_tasks_in_the_subtree_go_stale(mem, scenario):
    result = refute(mem, scenario)
    assert result.stale_tasks == ["T-002", "T-003"]
    assert mem.read("T-002")["status"] == "stale"


def test_the_raising_task_itself_is_not_invalidated(mem, scenario):
    result = refute(mem, scenario)
    assert scenario["root"]["id"] not in result.stale_tasks


def test_an_unrelated_branch_is_untouched(mem, scenario):
    result = refute(mem, scenario)
    assert scenario["other"]["id"] not in result.stale_tasks
    assert mem.read("F-002")["status"] == "active"


def test_going_stale_resets_the_attempt_counter(mem, scenario):
    mem.update("T-002", attempts=2)
    refute(mem, scenario)
    assert mem.read("T-002")["attempts"] == 0


def test_an_abandoned_task_stays_abandoned(mem, scenario):
    mem.update("T-003", status="abandoned", abandoned_reason="3 failures")
    result = refute(mem, scenario)
    assert "T-003" not in result.stale_tasks
    assert mem.read("T-003")["status"] == "abandoned"


def test_facts_from_affected_tasks_are_quarantined(mem, scenario):
    result = refute(mem, scenario)
    assert result.quarantined_facts == ["F-001"]
    assert mem.read("F-001")["status"] == "quarantined"


def test_quarantining_removes_the_citation_from_the_live_set(mem, scenario):
    refute(mem, scenario)
    assert Graph(mem).live_citations() == {"C-002"}


def test_hypothesis_confidence_is_recomputed_without_dead_citations(mem, scenario):
    result = refute(mem, scenario)
    changed = dict((h[0], (h[1], h[2])) for h in result.recomputed_hypotheses)
    assert "H-001" in changed
    # one live citation, one domain: 1/(1+2) * min(1, 1/2) * 1.0 = 0.17
    assert changed["H-001"] == (0.5, 0.17)
    assert mem.read("H-001")["confidence"] == 0.17


def test_a_hypothesis_falling_below_the_threshold_returns_to_proposed(mem, scenario):
    refute(mem, scenario)
    assert mem.read("H-001")["status"] == "proposed"


def test_a_hypothesis_still_above_the_threshold_keeps_its_status(
    mem, mktask, mkcitation, mkfact, mkassumption, mkhypothesis
):
    root = mktask()
    doomed = mktask(parent=root["id"], depth=1, status="done")
    survivor = mktask(question="unrelated")
    tainted = mkcitation(domain="evil.com")
    keep = [mkcitation(domain=f"good{i}.com") for i in range(4)]
    mkfact(statement="tainted", citations=[tainted["id"]], task=doomed["id"])
    mkfact(statement="clean", citations=[c["id"] for c in keep],
           task=survivor["id"])
    assumption = mkassumption(raised_by=root["id"])
    # Authored by the survivor task, outside the affected set, so this
    # exercises the confidence threshold alone, not provenance demotion.
    mkhypothesis(supporting=[tainted["id"]] + [c["id"] for c in keep],
                 status="supported", confidence=0.71, verdict="supported",
                 task=survivor["id"])
    mem.update(assumption["id"], status="refuted")
    Graph(mem).cascade(assumption["id"])
    # four live citations across four domains:
    # min(1, 4/3) * 4/(4+1) * 1.0 = 0.8
    assert mem.read("H-001")["confidence"] == 0.8
    assert mem.read("H-001")["status"] == "supported"


def test_blocks_extends_the_affected_set_beyond_the_subtree(
    mem, mktask, mkassumption
):
    root = mktask()
    elsewhere = mktask(question="a different root", status="done")
    assumption = mkassumption(raised_by=root["id"], blocks=[elsewhere["id"]])
    mem.update(assumption["id"], status="refuted")
    result = Graph(mem).cascade(assumption["id"])
    assert elsewhere["id"] in result.stale_tasks


def test_cascade_is_idempotent(mem, scenario):
    refute(mem, scenario)
    second = Graph(mem).cascade(scenario["assumption"]["id"])
    assert second.stale_tasks == []
    assert second.quarantined_facts == []
    assert second.recomputed_hypotheses == []
    assert second.reopened_assumptions == []
    assert second.provenance_demoted_hypotheses == []


def test_cascade_invalidates_the_cached_task_view(mem, scenario):
    mem.update(scenario["assumption"]["id"], status="refuted", refuted_by="H-001")
    graph = Graph(mem)
    graph.frontier()  # prime the cache
    graph.cascade(scenario["assumption"]["id"])
    # Same instance: the staled tasks must be visible, and must re-enter the
    # frontier. That requeue is the whole point of marking them stale.
    assert graph.tasks["T-002"]["status"] == "stale"
    assert "T-002" in graph.frontier()


# --- review round 1 fixes ---------------------------------------------

def test_a_stable_score_still_gets_reconciled_against_a_new_threshold(
    mem, mkcitation, mkfact, mkhypothesis
):
    """A numerically-unchanged score must still have its status checked
    against the threshold in force — a status is not just a cache of the
    score at the time it was last computed."""
    citations = [mkcitation(domain="a.com"), mkcitation(domain="a.com"),
                 mkcitation(domain="b.com")]
    mkfact(statement="evidence", citations=[c["id"] for c in citations],
           task=None)
    mkhypothesis(supporting=[c["id"] for c in citations], status="supported",
                 confidence=0.67, verdict="supported", task=None)
    # three live citations, two domains:
    # min(1, 3/3) * 2/(2+1) * 1.0 = 0.67
    result = Graph(mem, promotion_threshold=0.8).recompute_confidence()
    hyp = mem.read("H-001")
    assert hyp["confidence"] == 0.67
    assert hyp["status"] == "proposed"
    # the score did not move, so it is not reported even though it wrote.
    assert result == []


def test_a_refuted_hypothesis_stays_refuted_through_a_cascade(
    mem, mktask, mkcitation, mkfact, mkassumption, mkhypothesis
):
    """A hypothesis already known false must not be quietly un-refuted and
    made re-promotable just because its supporting evidence thinned out."""
    root = mktask()
    child = mktask(parent=root["id"], depth=1, status="done")
    tainted = mkcitation(domain="evil.com")
    mkfact(statement="tainted", citations=[tainted["id"]], task=child["id"])
    assumption = mkassumption(raised_by=root["id"])
    mkhypothesis(supporting=[tainted["id"]], status="refuted",
                 confidence=0.9, verdict="contradicted", task=root["id"])
    mem.update(assumption["id"], status="refuted")
    Graph(mem).cascade(assumption["id"])
    hyp = mem.read("H-001")
    assert hyp["status"] == "refuted"
    assert hyp["confidence"] == 0.0


def test_invalidation_propagates_across_depends_on(
    mem, mktask, mkfact, mkassumption
):
    """depends_on is the data-flow relation: a done task in a separate
    branch that consumed an affected task's output must be re-opened too,
    not just tasks reachable via parent links or an explicit block."""
    root = mktask()                                              # T-001
    task_a = mktask(parent=root["id"], depth=1, status="done")   # T-002
    task_b = mktask(question="a separate root", status="done",
                     depends_on=[task_a["id"]])                   # T-003
    mkfact(statement="downstream claim", task=task_b["id"])      # F-001
    assumption = mkassumption(raised_by=root["id"])
    mem.update(assumption["id"], status="refuted")
    result = Graph(mem).cascade(assumption["id"])
    assert task_b["id"] in result.stale_tasks
    assert mem.read(task_b["id"])["status"] == "stale"
    assert mem.read("F-001")["status"] == "quarantined"


def test_a_hypothesis_authored_in_the_pruned_branch_is_demoted_by_provenance(
    mem, mktask, mkcitation, mkfact, mkassumption, mkhypothesis
):
    """A hypothesis' reasoning can rest on a refuted assumption even when
    its citations happen to remain live through an unrelated branch. It
    must be demoted on provenance alone, not only on decayed evidence."""
    root = mktask()
    child = mktask(parent=root["id"], depth=1, status="done")
    survivor = mktask(question="unrelated")
    citations = [mkcitation(domain=f"good{i}.com") for i in range(4)]
    mkfact(statement="clean", citations=[c["id"] for c in citations],
           task=survivor["id"])
    assumption = mkassumption(raised_by=root["id"])
    # four live citations across four domains: 4/6 * 1.0 * 1.0 = 0.67,
    # comfortably above the default 0.6 threshold — evidence alone would
    # not touch this hypothesis.
    mkhypothesis(supporting=[c["id"] for c in citations], status="supported",
                 confidence=0.67, verdict="supported", task=child["id"])
    mem.update(assumption["id"], status="refuted")
    result = Graph(mem).cascade(assumption["id"])
    assert mem.read("H-001")["status"] == "proposed"
    assert "H-001" in result.provenance_demoted_hypotheses


def test_a_nested_confirmed_assumption_inside_the_pruned_branch_reopens(
    mem, mktask, mkassumption
):
    """An assumption confirmed by work that is now known unsound must be
    reopened, not left confirmed — and one raised elsewhere is untouched."""
    root = mktask()                                              # T-001
    child = mktask(parent=root["id"], depth=1, status="done")    # T-002
    outside = mktask(question="outside")                         # T-003
    assumption = mkassumption(raised_by=root["id"])              # A-001
    nested = mkassumption(statement="nested", raised_by=child["id"],
                           status="confirmed")                    # A-002
    outside_assumption = mkassumption(statement="outside",
                                       raised_by=outside["id"],
                                       status="confirmed")        # A-003
    mem.update(assumption["id"], status="refuted")
    result = Graph(mem).cascade(assumption["id"])
    assert mem.read(nested["id"])["status"] == "open"
    assert mem.read(outside_assumption["id"])["status"] == "confirmed"
    assert result.reopened_assumptions == [nested["id"]]


# --- final review 2: the cascade must not wedge on a bad citation --------
#
# recompute_confidence() indexed straight into each supporting citation:
#
#     [self.memory.read(c)["domain"] for c in supporting]
#
# which raises KeyError on a citation id with no file behind it, and
# KeyError('domain') on one whose node is missing that field. Neither needs
# disk corruption to arise: citation.json's id pattern validates the SHAPE
# of a reference, never its existence, so a fact citing a citation whose
# write failed is a fully schema-valid store.
#
# This is worse than a crash in a reporting tool. Spec section 8 runs the
# cascade inside `submit`, and the throw lands AFTER the stale-marking and
# quarantine writes have already been committed — so the store advances,
# the rescore never happens, and every later submit dies on the same line
# with no repair path the design permits. Skip-and-continue matches the
# convention fsck.py already uses for this defect family: the dangling
# reference is fsck's to report, not the cascade's to die on.


@pytest.fixture
def broken_citation_scenario(mem, mktask, mkcitation, mkfact, mkassumption,
                             mkhypothesis):
    """The standard pruned branch, plus one clean surviving citation. The
    caller adds the defective reference before running the cascade."""
    root = mktask()                                              # T-001
    child = mktask(parent=root["id"], depth=1, status="done")    # T-002
    other = mktask(question="unrelated root")                    # T-003
    tainted = mkcitation(domain="evil.com")                      # C-001
    clean = mkcitation(domain="good.com")                        # C-002
    mkfact(statement="tainted", citations=[tainted["id"]],
           task=child["id"])                                     # F-001
    assumption = mkassumption(raised_by=root["id"])              # A-001
    return {"root": root, "child": child, "other": other,
            "tainted": tainted, "clean": clean, "assumption": assumption}


# One live citation on one domain: 1/(1+2) * min(1, 1/2) * 1.0 = 0.17.
SURVIVOR_SCORE = confidence.compute(["good.com"], "supported")


def test_cascade_completes_with_a_dangling_supporting_citation(
    mem, broken_citation_scenario, mkfact, mkhypothesis
):
    scene = broken_citation_scenario
    mkfact(statement="clean", citations=[scene["clean"]["id"], "C-404"],
           task=scene["other"]["id"])                            # F-002
    mkhypothesis(supporting=[scene["tainted"]["id"], scene["clean"]["id"],
                             "C-404"],
                 status="supported", confidence=0.5, verdict="supported",
                 task=scene["root"]["id"])
    mem.update(scene["assumption"]["id"], status="refuted")

    result = Graph(mem).cascade(scene["assumption"]["id"])  # must not raise

    assert result.stale_tasks == ["T-002"]
    assert mem.read("H-001")["confidence"] == SURVIVOR_SCORE
    assert ("H-001", 0.5, SURVIVOR_SCORE) in result.recomputed_hypotheses


def test_cascade_completes_with_a_citation_missing_its_domain(
    mem, broken_citation_scenario, mkcitation, mkfact, mkhypothesis
):
    scene = broken_citation_scenario
    fieldless = mkcitation(domain="nowhere.com")                 # C-003
    _strip_line(mem.path_for(fieldless["id"]), "domain:")
    mkfact(statement="clean",
           citations=[scene["clean"]["id"], fieldless["id"]],
           task=scene["other"]["id"])                            # F-002
    mkhypothesis(supporting=[scene["tainted"]["id"], scene["clean"]["id"],
                             fieldless["id"]],
                 status="supported", confidence=0.5, verdict="supported",
                 task=scene["root"]["id"])
    mem.update(scene["assumption"]["id"], status="refuted")

    result = Graph(mem).cascade(scene["assumption"]["id"])  # must not raise

    assert result.stale_tasks == ["T-002"]
    assert mem.read("H-001")["confidence"] == SURVIVOR_SCORE


def test_a_wedged_cascade_can_be_re_run_after_a_bad_citation(
    mem, broken_citation_scenario, mkfact, mkhypothesis
):
    """The original throw landed after the quarantine writes committed, so
    re-running submit could never get further. A second pass must be a
    clean no-op, exactly as it is for a healthy store."""
    scene = broken_citation_scenario
    mkfact(statement="clean", citations=[scene["clean"]["id"], "C-404"],
           task=scene["other"]["id"])
    mkhypothesis(supporting=[scene["clean"]["id"], "C-404"],
                 status="supported", confidence=0.5, verdict="supported",
                 task=scene["root"]["id"])
    mem.update(scene["assumption"]["id"], status="refuted")
    Graph(mem).cascade(scene["assumption"]["id"])

    second = Graph(mem).cascade(scene["assumption"]["id"])

    assert second.stale_tasks == []
    assert second.quarantined_facts == []
    assert second.recomputed_hypotheses == []


def test_recompute_is_defensive_even_when_handed_a_dangling_live_id(
    mem, mkcitation, mkfact, mkhypothesis
):
    """live_citations() is derived from the files on disk, so it will not
    normally hand recompute_confidence() an id with nothing behind it. Pin
    the guard directly anyway: it is the last line of defence for the one
    routine whose failure wedges every subsequent submit."""
    clean = mkcitation(domain="good.com")
    mkfact(statement="clean", citations=[clean["id"]], task=None)
    mkhypothesis(supporting=[clean["id"], "C-404"], status="supported",
                 confidence=0.5, verdict="supported", task=None)

    class Wider(Graph):
        def live_citations(self):
            return super().live_citations() | {"C-404"}

    Wider(mem).recompute_confidence()  # must not raise

    assert mem.read("H-001")["confidence"] == SURVIVOR_SCORE


# --- final review 5: liveness is a property of the citation too ----------
#
# live_citations() decided liveness purely from fact status and ignored the
# citation's own, so a rejected or unverifiable citation counted at full
# weight toward a promotion score. One rejected + one unverifiable + one
# verified citation scored 0.60 -- exactly on the default threshold.
#
# Spec section 6 gate 2 rejects only facts resting SOLELY on a failed
# citation, and explicitly keeps unverifiable ones ("A 403 or JS-wall marks
# the citation `unverifiable` rather than rejected"). Keeping the node is
# not the same as counting it: the spec's own words for that state are
# "flagged rather than silently trusted", and full weight in the promotion
# number is exactly silent trust. Gate 2 is the only independent check that
# a quote is really on the page, so a citation that never passed it cannot
# lift a claim over the threshold -- while remaining in the graph, in the
# bibliography, and in Appendix D. Erring toward under-promotion.


@pytest.mark.parametrize("status", ["pending", "rejected", "unverifiable"])
def test_a_citation_that_never_passed_gate_two_is_not_live(
    mem, mkcitation, mkfact, status
):
    unchecked = mkcitation(domain="unchecked.com", status=status)
    verified = mkcitation(domain="good.com", status="verified")
    mkfact(statement="e", citations=[unchecked["id"], verified["id"]],
           task=None)
    assert Graph(mem).live_citations() == {verified["id"]}


def test_a_verified_citation_on_a_quarantined_fact_is_still_not_live(
    mem, mkcitation, mkfact
):
    """Both conditions are necessary, not just the new one."""
    citation = mkcitation(domain="good.com", status="verified")
    mkfact(statement="e", citations=[citation["id"]], status="quarantined",
           task=None)
    assert Graph(mem).live_citations() == set()


def test_a_hypothesis_resting_only_on_unpassed_citations_scores_zero(
    mem, mkcitation, mkfact, mkhypothesis
):
    rejected = mkcitation(domain="a.com", status="rejected")
    unverifiable = mkcitation(domain="b.com", status="unverifiable")
    ids = [rejected["id"], unverifiable["id"]]
    mkfact(statement="e", citations=ids, task=None)
    mkhypothesis(supporting=ids, status="supported", confidence=0.5,
                 verdict="supported", task=None)

    Graph(mem).recompute_confidence()

    hypothesis = mem.read("H-001")
    assert hypothesis["confidence"] == 0.0
    assert hypothesis["status"] == "proposed"


def test_unpassed_citations_cannot_carry_a_claim_over_the_threshold(
    mem, mkcitation, mkfact, mkhypothesis
):
    """The measured regression: rejected + unverifiable + verified landed
    on 0.60, exactly the default promotion threshold, on the strength of
    two citations no gate ever confirmed."""
    citations = [mkcitation(domain="a.com", status="rejected"),
                 mkcitation(domain="a.com", status="unverifiable"),
                 mkcitation(domain="b.com", status="verified")]
    ids = [c["id"] for c in citations]
    mkfact(statement="e", citations=ids, task=None)
    mkhypothesis(supporting=ids, verdict="supported", task=None)

    graph = Graph(mem, promotion_threshold=0.6)
    graph.recompute_confidence()

    assert mem.read("H-001")["confidence"] < 0.6
    assert mem.read("H-001")["status"] == "proposed"


def test_a_rejected_citation_leaves_the_live_set_when_it_is_rejected(
    mem, mkcitation, mkfact, mkhypothesis
):
    """Gate 2 rejects a citation by writing its status, and the promotion
    score must follow on the next recompute without the fact being touched
    at all -- the fact survives because it does not rest solely on it."""
    doomed = mkcitation(domain="a.com", status="verified")
    keep = mkcitation(domain="b.com", status="verified")
    mkfact(statement="e", citations=[doomed["id"], keep["id"]], task=None)
    mkhypothesis(supporting=[doomed["id"], keep["id"]], verdict="supported",
                 task=None)
    before = Graph(mem).recompute_confidence()
    assert before  # it scored on two live citations

    mem.update(doomed["id"], status="rejected")

    assert Graph(mem).live_citations() == {keep["id"]}
    Graph(mem).recompute_confidence()
    assert mem.read("H-001")["confidence"] == confidence.compute(
        ["b.com"], "supported"
    )
    assert mem.read("F-001")["status"] == "active"


# --- required_domains is the run's, not a hardcoded 2 -----------------

def test_recompute_scores_against_the_graphs_own_required_domains(
    mem, mktask, mkcitation, mkfact, mkhypothesis
):
    """graph.py hardcoded required_domains=2 in the one place the
    persisted score is computed, while apply._verified_status read the
    configured value.

    INVERTED. `required_domains` no longer enters the score at all — it
    was the divisor in `spread = min(1, distinct/required_domains)`, the
    same term that made a third independent domain worthless. It is now
    gate 3's bar and only gate 3's bar, which is what it was always
    meant to be; test_gate_independence.py covers that it bites there.

    The original concern still holds and is still worth pinning: a Graph
    handed a non-default value must not silently disagree with the run.
    It now agrees by not reading it."""
    task = mktask(question="root")
    ids = []
    for index in range(2):
        citation = mkcitation(url=f"https://d{index}-example.com/x",
                              domain=f"d{index}-example.com",
                              quote=f"a quoted span {index}", task=task["id"])
        mkfact(statement=f"f{index}", citations=[citation["id"]],
               task=task["id"])
        ids.append(citation["id"])
    hypothesis = mkhypothesis(claim="c", supporting=ids, verdict="supported",
                              task=task["id"])

    # 2 citations on 2 domains: volume = min(1, 2/3) = 0.667, breadth =
    # 2/3, so 0.44 — and unchanged by required_domains.
    Graph(mem, required_domains=2).recompute_confidence()
    assert mem.read(hypothesis["id"])["confidence"] == 0.44

    Graph(mem, required_domains=4).recompute_confidence()
    assert mem.read(hypothesis["id"])["confidence"] == 0.44


# --- contested is re-evaluated, not decided once ----------------------

def test_a_counter_citation_that_becomes_live_makes_a_claim_contested(
    mem, mktask, mkcitation, mkfact, mkhypothesis
):
    """I3. `contested` was written once, at the moment a verdict landed,
    and never revisited. Over a multi-day run a counter citation becomes
    live all the time — its fact is reactivated, or its own gate-2 status
    moves from pending to verified on a re-check — and the hypothesis kept
    a `supported` badge with an undisclosed live dispute against it."""
    task = mktask(question="root")
    ids = []
    # Six supporting citations, not three. Opposition is weighed against
    # the volume of support now, so a claim at exactly the gate-3
    # minimum is DEMOTED by one live counter rather than contested by
    # it. This test is about `contested` being re-evaluated on every
    # recompute, so the claim has to carry enough support to reach that
    # state at all.
    for index in range(6):
        citation = mkcitation(url=f"https://d{index}-example.com/x",
                              domain=f"d{index}-example.com",
                              quote=f"a quoted span {index}", task=task["id"])
        mkfact(statement=f"f{index}", citations=[citation["id"]],
               task=task["id"])
        ids.append(citation["id"])
    against = mkcitation(url="https://counter-example.com/x",
                         domain="counter-example.com",
                         quote="a contradicting span", status="pending",
                         task=task["id"])
    mkfact(statement="the other side", citations=[against["id"]],
           task=task["id"])
    hypothesis = mkhypothesis(claim="c", supporting=ids, counter=[against["id"]],
                              status="supported", confidence=0.6,
                              verdict="supported", task=task["id"])

    # Not live yet: the counter citation has never passed gate 2.
    Graph(mem).recompute_confidence()
    assert mem.read(hypothesis["id"])["status"] == "supported"

    # A later re-check verifies it. The dispute is now real.
    mem.update(against["id"], status="verified")
    Graph(mem).recompute_confidence()
    assert mem.read(hypothesis["id"])["status"] == "contested"


def test_a_counter_citation_that_stops_being_live_restores_supported(
    mem, mktask, mkcitation, mkfact, mkhypothesis
):
    """The same rule in the other direction: `contested` is a statement
    about the evidence as it stands, so a dispute that is quarantined
    away must stop being reported."""
    task = mktask(question="root")
    ids = []
    # Six supporting citations, not three. Opposition is weighed against
    # the volume of support now, so a claim at exactly the gate-3
    # minimum is DEMOTED by one live counter rather than contested by
    # it. This test is about `contested` being re-evaluated on every
    # recompute, so the claim has to carry enough support to reach that
    # state at all.
    for index in range(6):
        citation = mkcitation(url=f"https://d{index}-example.com/x",
                              domain=f"d{index}-example.com",
                              quote=f"a quoted span {index}", task=task["id"])
        mkfact(statement=f"f{index}", citations=[citation["id"]],
               task=task["id"])
        ids.append(citation["id"])
    against = mkcitation(url="https://counter-example.com/x",
                         domain="counter-example.com",
                         quote="a contradicting span", task=task["id"])
    counter_fact = mkfact(statement="the other side",
                          citations=[against["id"]], task=task["id"])
    hypothesis = mkhypothesis(claim="c", supporting=ids, counter=[against["id"]],
                              status="contested", confidence=0.6,
                              verdict="supported", task=task["id"])
    mem.update(counter_fact["id"], status="quarantined")
    Graph(mem).recompute_confidence()
    assert mem.read(hypothesis["id"])["status"] == "supported"


def test_recompute_still_never_re_promotes_a_proposed_hypothesis(
    mem, mktask, mkcitation, mkfact, mkhypothesis
):
    """The guard the contested fix must not break. recompute stays
    demote-only for `proposed`: a rescore that could promote would
    immediately undo the cascade's provenance demotion — see
    apply._verified_status."""
    task = mktask(question="root")
    ids = []
    for index in range(3):
        citation = mkcitation(url=f"https://d{index}-example.com/x",
                              domain=f"d{index}-example.com",
                              quote=f"a quoted span {index}", task=task["id"])
        mkfact(statement=f"f{index}", citations=[citation["id"]],
               task=task["id"])
        ids.append(citation["id"])
    hypothesis = mkhypothesis(claim="c", supporting=ids, status="proposed",
                              confidence=0.0, verdict="supported",
                              task=task["id"])
    Graph(mem).recompute_confidence()
    stored = mem.read(hypothesis["id"])
    # 3 citations on 3 domains: min(1, 3/3) * 3/(3+1) = 0.75
    assert stored["confidence"] == 0.75
    assert stored["status"] == "proposed"
