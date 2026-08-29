"""A conditional stop has to compile to something code can evaluate.
That is what keeps the decision to stop out of the model's hands."""
import pytest

import predicates
import research
import runconfig
import signals
from graph import Graph


# --- the closed clause set -------------------------------------------

def test_a_well_formed_predicate_validates():
    predicates.validate({"all": [
        {"branch": "T-004", "tasks_resolved": True},
        {"branch": "T-004", "min_hypothesis_confidence": 0.7},
    ]})


def test_the_spec_s_own_example_validates():
    """Copied from spec section 4 verbatim. If this stops working the
    documented interface has drifted."""
    predicates.validate({"all": [
        {"branch": "T-004", "tasks_resolved": True},
        {"branch": "T-004", "min_hypothesis_confidence": 0.7},
    ]})


def test_an_any_predicate_validates():
    predicates.validate({"any": [{"min_facts": 10}]})


def test_a_predicate_with_neither_all_nor_any_is_refused():
    with pytest.raises(predicates.PredicateError):
        predicates.validate({"min_facts": 10})


def test_a_predicate_with_both_all_and_any_is_refused():
    with pytest.raises(predicates.PredicateError):
        predicates.validate({"all": [{"min_facts": 1}],
                             "any": [{"min_facts": 2}]})


def test_an_empty_clause_list_is_refused():
    """An empty `all` is vacuously true, which would stop the run
    instantly."""
    with pytest.raises(predicates.PredicateError):
        predicates.validate({"all": []})


def test_a_vibes_condition_is_refused():
    """Spec section 4's example of a request that must not compile."""
    with pytest.raises(predicates.PredicateError):
        predicates.validate({"all": [{"feels_complete": True}]})


def test_a_clause_with_two_conditions_is_refused():
    """One condition per clause, so `describe` is unambiguous and so
    evaluation has no precedence rules to get wrong."""
    with pytest.raises(predicates.PredicateError):
        predicates.validate({"all": [{"min_facts": 3, "min_domains": 2}]})


def test_a_clause_with_only_a_branch_is_refused():
    with pytest.raises(predicates.PredicateError):
        predicates.validate({"all": [{"branch": "T-001"}]})


def test_a_malformed_branch_id_is_refused():
    with pytest.raises(predicates.PredicateError):
        predicates.validate({"all": [{"branch": "F-001",
                                      "tasks_resolved": True}]})


def test_tasks_resolved_may_not_be_false():
    """`tasks_resolved: false` reads as "stop while work remains", which
    is what `research signal stop` is for."""
    with pytest.raises(predicates.PredicateError):
        predicates.validate({"all": [{"tasks_resolved": False}]})


def test_a_confidence_above_one_is_refused():
    with pytest.raises(predicates.PredicateError):
        predicates.validate({"all": [{"min_hypothesis_confidence": 1.5}]})


def test_a_zero_count_is_refused():
    """min_facts: 0 is always satisfied."""
    with pytest.raises(predicates.PredicateError):
        predicates.validate({"all": [{"min_facts": 0}]})


@pytest.mark.parametrize("condition,value", [
    ("tasks_resolved", True),
    ("min_hypothesis_confidence", 0.7),
    ("min_facts", 5),
    ("min_domains", 3),
    ("min_supported_hypotheses", 2),
])
def test_every_documented_condition_validates(condition, value):
    predicates.validate({"all": [{condition: value}]})


def test_every_condition_in_the_schema_is_evaluable():
    """A condition the schema accepts and evaluate() ignores would
    silently never fire — the worst possible failure for a stop
    signal."""
    assert set(predicates.CONDITIONS) == {
        "tasks_resolved", "min_hypothesis_confidence", "min_facts",
        "min_domains", "min_supported_hypotheses"}


# --- describe ---------------------------------------------------------

def test_describe_names_every_clause():
    text = predicates.describe({"all": [
        {"branch": "T-004", "tasks_resolved": True},
        {"min_facts": 20},
    ]})
    assert "T-004" in text and "20" in text and "all" in text.lower()


def test_describe_distinguishes_all_from_any():
    both = {"all": [{"min_facts": 1}]}, {"any": [{"min_facts": 1}]}
    assert predicates.describe(both[0]) != predicates.describe(both[1])


def test_describe_says_whole_run_when_there_is_no_branch():
    assert "whole run" in predicates.describe({"all": [{"min_facts": 1}]})


# --- evaluate ---------------------------------------------------------

@pytest.fixture
def branch(mem, mktask, mkcitation, mkfact, mkhypothesis):
    """One branch: a done root T-001, a done worker T-002, three facts on
    three domains, and one supported hypothesis at 0.6. The root is
    `done`, not the fixture's earlier default of `pending`: a decompose
    task transitions off `pending` once it dispatches and produces
    children, so a root still sitting at `pending` alongside a `done`
    child is a state the real system never produces."""
    root = mktask(question="root", kind="decompose", status="done")
    worker = mktask(question="w", kind="extract", parent=root["id"], depth=1,
                    status="done")
    citations = [mkcitation(url=f"https://d{i}-example.com/x",
                            domain=f"d{i}-example.com", quote=f"a quoted span {i}")
                 for i in range(3)]
    for index, citation in enumerate(citations):
        mkfact(statement=f"f{index}", citations=[citation["id"]],
               task=worker["id"])
    mkhypothesis(claim="c", supporting=[c["id"] for c in citations],
                 status="supported", confidence=0.6, verdict="supported",
                 task=worker["id"])
    return {"root": root["id"], "worker": worker["id"]}


def evaluate(mem, predicate):
    return predicates.evaluate(predicate, mem, Graph(mem))


def test_tasks_resolved_is_false_while_work_remains(mem, branch, mktask):
    mktask(question="pending work", parent=branch["root"], depth=1)
    assert not evaluate(mem, {"all": [{"branch": branch["root"],
                                       "tasks_resolved": True}]})


def test_tasks_resolved_is_true_when_the_branch_is_finished(mem, branch):
    assert evaluate(mem, {"all": [{"branch": branch["root"],
                                   "tasks_resolved": True}]})


def test_tasks_resolved_is_false_for_a_never_dispatched_root(mem, mktask):
    """A branch that is nothing but a freshly-created, still-pending root
    has run zero ticks of research: no children, no facts, no
    hypotheses. `tasks_resolved: True` here would let `research signal
    stop-when '{"all": [{"branch": ..., "tasks_resolved": true}]}'` fire
    the instant the branch is created — ending a run before it starts,
    silently, which is exactly the failure this predicate exists to
    prevent. The root itself is open work like any other task."""
    root = mktask(question="root", kind="decompose")
    assert not evaluate(mem, {"all": [{"branch": root["id"],
                                       "tasks_resolved": True}]})


def test_tasks_resolved_ignores_a_task_that_can_never_be_dispatched(
    mem, branch, mktask
):
    """A task waiting on a dependency that does not exist is not work in
    progress. Counting it would hold a conditional stop open forever —
    the same trap carry-forward (e) describes for the coverage halt."""
    mktask(question="stuck", parent=branch["root"], depth=1,
           depends_on=["T-909"])
    assert evaluate(mem, {"all": [{"branch": branch["root"],
                                   "tasks_resolved": True}]})


def test_tasks_resolved_is_scoped_to_its_branch(mem, branch, mktask):
    other = mktask(question="other root", kind="decompose")
    mktask(question="busy", parent=other["id"], depth=1)
    assert evaluate(mem, {"all": [{"branch": branch["root"],
                                   "tasks_resolved": True}]})
    assert not evaluate(mem, {"all": [{"branch": other["id"],
                                       "tasks_resolved": True}]})


def test_a_clause_with_no_branch_covers_the_whole_run(mem, branch, mktask):
    other = mktask(question="other root", kind="decompose")
    mktask(question="busy", parent=other["id"], depth=1)
    assert not evaluate(mem, {"all": [{"tasks_resolved": True}]})


def test_min_facts_counts_active_facts_in_the_branch(mem, branch):
    assert evaluate(mem, {"all": [{"branch": branch["root"],
                                   "min_facts": 3}]})
    assert not evaluate(mem, {"all": [{"branch": branch["root"],
                                       "min_facts": 4}]})


def test_min_facts_ignores_a_quarantined_fact(mem, branch):
    mem.update("F-001", status="quarantined")
    assert not evaluate(mem, {"all": [{"branch": branch["root"],
                                       "min_facts": 3}]})


def test_min_domains_counts_distinct_registrable_domains(mem, branch):
    assert evaluate(mem, {"all": [{"branch": branch["root"],
                                   "min_domains": 3}]})
    assert not evaluate(mem, {"all": [{"branch": branch["root"],
                                       "min_domains": 4}]})


def test_min_domains_counts_one_site_once(mem, branch, mkcitation, mkfact):
    """Same eTLD+1 rule as gate 3. A stop condition that could be
    satisfied by twelve pages of one blog would be worthless."""
    extra = mkcitation(url="https://d0-example.com/other", domain="d0-example.com",
                       quote="another quoted span")
    mkfact(statement="extra", citations=[extra["id"]], task=branch["worker"])
    assert not evaluate(mem, {"all": [{"branch": branch["root"],
                                       "min_domains": 4}]})


def test_min_supported_hypotheses_counts_only_supported(mem, branch,
                                                        mkhypothesis):
    assert evaluate(mem, {"all": [{"branch": branch["root"],
                                   "min_supported_hypotheses": 1}]})
    mkhypothesis(claim="proposed one", supporting=["C-001"],
                 task=branch["worker"])
    assert not evaluate(mem, {"all": [{"branch": branch["root"],
                                       "min_supported_hypotheses": 2}]})


def test_min_hypothesis_confidence_requires_every_hypothesis_to_clear_it(
    mem, branch, mkhypothesis
):
    assert evaluate(mem, {"all": [{"branch": branch["root"],
                                   "min_hypothesis_confidence": 0.6}]})
    mkhypothesis(claim="weak", supporting=["C-001"], confidence=0.1,
                 task=branch["worker"])
    assert not evaluate(mem, {"all": [{"branch": branch["root"],
                                       "min_hypothesis_confidence": 0.6}]})


def test_min_hypothesis_confidence_ignores_a_refuted_hypothesis(
    mem, branch, mkhypothesis
):
    """A refuted claim is a settled answer, not an under-evidenced one.
    Counting it would make the condition unsatisfiable the moment
    anything is disproven."""
    mkhypothesis(claim="wrong", supporting=["C-001"], status="refuted",
                 confidence=0.0, verdict="contradicted",
                 task=branch["worker"])
    assert evaluate(mem, {"all": [{"branch": branch["root"],
                                   "min_hypothesis_confidence": 0.6}]})


def test_min_hypothesis_confidence_is_false_with_no_hypotheses_at_all(
    mem, mktask
):
    """Vacuous truth here would stop a run before it formed a single
    claim."""
    root = mktask(question="root", kind="decompose")
    assert not evaluate(mem, {"all": [{"branch": root["id"],
                                       "min_hypothesis_confidence": 0.6}]})


def test_all_requires_every_clause(mem, branch):
    assert not evaluate(mem, {"all": [{"branch": branch["root"],
                                       "min_facts": 3},
                                      {"branch": branch["root"],
                                       "min_facts": 99}]})


def test_any_requires_one_clause(mem, branch):
    assert evaluate(mem, {"any": [{"branch": branch["root"],
                                   "min_facts": 99},
                                  {"branch": branch["root"],
                                   "min_facts": 3}]})


def test_evaluate_rejects_a_predicate_that_never_validated(mem):
    """Belt and braces: the loop must not silently treat an unknown
    condition as unsatisfied."""
    with pytest.raises(predicates.PredicateError):
        evaluate(mem, {"all": [{"feels_complete": True}]})


def test_a_branch_that_does_not_exist_never_fires(mem, branch):
    assert not evaluate(mem, {"all": [{"branch": "T-909",
                                       "min_facts": 1}]})


def test_evaluate_survives_a_corrupt_node(mem, branch):
    """Predicates run every tick. One bad file must not stop the loop
    from being able to stop."""
    mem.path_for("F-001").write_text("garbage\n", encoding="utf-8")
    assert not evaluate(mem, {"all": [{"branch": branch["root"],
                                       "min_facts": 3}]})


# --- research signal --------------------------------------------------

def test_signal_stop_sets_the_flag(workspace_root):
    assert research.main(["signal", "--root", str(workspace_root),
                          "stop"]) == 0
    assert runconfig.load(workspace_root)["signals"]["stop_requested"] is True


def test_signal_stop_when_records_the_predicate(workspace_root):
    code = research.main([
        "signal", "--root", str(workspace_root), "stop-when",
        "--json", '{"all": [{"min_facts": 20}]}'])
    assert code == 0
    stored = runconfig.load(workspace_root)["signals"]["stop_when"]
    assert stored == {"all": [{"min_facts": 20}]}


def test_signal_stop_when_echoes_the_compiled_predicate(workspace_root,
                                                        capsys):
    """Spec section 4: the agent echoes the compiled predicate back in
    chat. It can only do that if the command prints it."""
    research.main(["signal", "--root", str(workspace_root), "stop-when",
                   "--json", '{"all": [{"min_facts": 20}]}'])
    assert "20" in capsys.readouterr().out


def test_signal_stop_when_registers_a_confirmation_checkpoint(workspace_root):
    """Spec section 4: 'the loop does not resume until the user confirms
    it.' Prose cannot enforce that; an unresolved checkpoint can, because
    next refuses to dispatch while one exists."""
    research.main(["signal", "--root", str(workspace_root), "stop-when",
                   "--json", '{"all": [{"min_facts": 20}]}'])
    cfg = runconfig.load(workspace_root)
    pending = signals.pending_checkpoints(cfg)
    assert len(pending) == 1
    assert "confirm" in pending[0]["note"].lower()


def test_an_uncompilable_request_is_refused_and_becomes_a_checkpoint(
    workspace_root, capsys
):
    """Spec section 4, verbatim: a request that cannot compile 'is
    refused, and a checkpoint is registered instead: the loop pauses at
    the next tick and asks the user, rather than letting the model
    quietly decide it is satisfied.'"""
    code = research.main([
        "signal", "--root", str(workspace_root), "stop-when",
        "--json", '{"all": [{"feels_complete": true}]}'])
    cfg = runconfig.load(workspace_root)
    assert cfg["signals"]["stop_when"] is None
    assert len(signals.pending_checkpoints(cfg)) == 1
    assert "refused" in capsys.readouterr().out.lower()
    assert code == 0


def test_malformed_json_is_reported_without_a_traceback(workspace_root,
                                                        capsys):
    code = research.main(["signal", "--root", str(workspace_root),
                          "stop-when", "--json", "{not json"])
    assert code == 1
    assert "error" in capsys.readouterr().err.lower()


def test_signal_checkpoint_records_a_note(workspace_root):
    assert research.main(["signal", "--root", str(workspace_root),
                          "checkpoint", "--note", "check with me first"]) == 0
    cfg = runconfig.load(workspace_root)
    assert signals.pending_checkpoints(cfg)[0]["note"] == \
        "check with me first"


def test_a_checkpoint_records_the_tick_it_was_raised_at(workspace_root):
    cfg = runconfig.load(workspace_root)
    cfg["status"]["tick"] = 12
    runconfig.save(workspace_root, cfg)
    research.main(["signal", "--root", str(workspace_root), "checkpoint",
                   "--note", "n"])
    assert signals.pending_checkpoints(
        runconfig.load(workspace_root))[0]["raised_at_tick"] == 12


def test_a_resolved_checkpoint_is_not_pending(workspace_root):
    research.main(["signal", "--root", str(workspace_root), "checkpoint",
                   "--note", "n"])
    cfg = runconfig.load(workspace_root)
    cfg["signals"]["checkpoints"][0]["resolved"] = True
    runconfig.save(workspace_root, cfg)
    assert signals.pending_checkpoints(
        runconfig.load(workspace_root)) == []


def test_signal_on_an_uninitialised_directory_says_so(tmp_path, capsys):
    code = research.main(["signal", "--root", str(tmp_path / "nope"), "stop"])
    assert code == 1
    assert "research init" in capsys.readouterr().err
