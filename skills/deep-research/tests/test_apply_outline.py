import json

import pytest

import apply
import graph as graph_mod
import runconfig


@pytest.fixture
def seeded(mem, mktask, mkfact, mkhypothesis, mkcitation, tmp_path):
    root_task = mktask(question="why is the sky blue?", kind="decompose")["id"]
    theme = mktask(question="optical scattering", parent=root_task,
                   depth=1)["id"]
    citation = mkcitation(url="https://a-example.com/p", domain="a-example.com",
                          quote="short wavelengths scatter more", task=theme)["id"]
    blocked = mkcitation(url="https://b-example.com/p", domain="b-example.com",
                         quote="a rejected span here", status="rejected",
                         task=theme)["id"]
    fact = mkfact(statement="blue scatters more",
                  citations=[citation, blocked], task=theme)["id"]
    hypothesis = mkhypothesis(claim="Rayleigh explains it",
                              supporting=[citation], task=theme)["id"]
    frozen = {
        "question": "why is the sky blue?",
        "sections": [{"id": "S-001", "theme": theme, "title": "optical scattering",
                      "hypotheses": [hypothesis], "facts": [fact]}],
        "orphans": {"hypotheses": [], "facts": []},
        "empty_themes": [root_task],
    }
    # mktask has no `inputs=` keyword (see tests/conftest.py), so the
    # frozen outline is attached with a direct mem.update after creation.
    task = mktask(question="produce the outline", kind="outline")
    task = mem.update(task["id"], inputs={"outline": frozen})
    artifact = {"task_id": task["id"], "sections": [
        {"id": "S-001", "title": "How light scatters",
         "hypotheses": [hypothesis], "facts": [fact]}]}
    return {"mem": mem, "task": task, "artifact": artifact, "frozen": frozen,
            "root": tmp_path / "research", "citation": citation,
            "blocked": blocked, "fact": fact, "hypothesis": hypothesis,
            "cfg": runconfig.default("why is the sky blue?")}


def _apply(seeded, artifact=None):
    graph = graph_mod.Graph(seeded["mem"])
    return apply.apply_outline(
        seeded["mem"], graph, seeded["cfg"], seeded["task"]["id"],
        seeded["task"], artifact or seeded["artifact"], root=seeded["root"])


def test_applying_an_outline_writes_the_accepted_file(seeded):
    _apply(seeded)
    written = json.loads(
        (seeded["root"] / "out" / "outline.json").read_text(encoding="utf-8"))
    assert written["sections"][0]["title"] == "How light scatters"


def test_the_accepted_outline_keeps_the_computed_theme(seeded):
    _apply(seeded)
    written = json.loads(
        (seeded["root"] / "out" / "outline.json").read_text(encoding="utf-8"))
    assert written["sections"][0]["theme"] == seeded["frozen"]["sections"][0]["theme"]


def _sections_seeded(seeded, result):
    """{section id: frozen payload} over every task the apply spawned."""
    return {seeded["mem"].read(t)["inputs"]["section"]["id"]:
            seeded["mem"].read(t)["inputs"]["section"]
            for t in result.spawned}


def test_applying_an_outline_seeds_one_synthesize_task_per_section(seeded):
    result = _apply(seeded)
    payloads = _sections_seeded(seeded, result)
    assert set(payloads) == {"S-001", "S-999"}
    for task_id in result.spawned:
        assert seeded["mem"].read(task_id)["kind"] == "synthesize"


def test_the_cross_cutting_synthesis_section_is_seeded_too(seeded):
    """Spec section 7's document shape. It cannot be an outline section —
    validate requires each hypothesis exactly once and this needs all of
    them — so apply_outline adds it separately."""
    result = _apply(seeded)
    synthesis = _sections_seeded(seeded, result)["S-999"]
    assert synthesis["title"] == "Synthesis"
    assert [h["id"] for h in synthesis["hypotheses"]] == [seeded["hypothesis"]]
    assert synthesis["facts"] == []


def test_the_synthesis_section_may_cite_anything_the_themes_could(seeded):
    result = _apply(seeded)
    payloads = _sections_seeded(seeded, result)
    assert payloads["S-999"]["allowed_cite_keys"] == \
        payloads["S-001"]["allowed_cite_keys"]


def test_the_section_payload_carries_claims_and_statements(seeded):
    result = _apply(seeded)
    section = _sections_seeded(seeded, result)["S-001"]
    assert section["hypotheses"][0]["claim"] == "Rayleigh explains it"
    assert section["facts"][0]["statement"] == "blue scatters more"


def test_the_section_payload_carries_the_verdict_not_the_score(seeded):
    """The writer's job is to state a claim at the strength the evidence
    supports. It was handed `confidence`, and synthesizer.md rule 4 told
    it to calibrate against 0.9 and 0.5 — neither of which can occur.
    base = n/(n+2) reaches 0.9 only at 18 live citations, and 0.5 is below
    the 0.6 promotion floor, so no `supported` claim can ever sit there.
    Every promoted claim lands in 0.60–0.75 and the rubric had nothing to
    say about that band."""
    result = _apply(seeded)
    hypothesis = _sections_seeded(seeded, result)["S-001"]["hypotheses"][0]
    assert "confidence" not in hypothesis
    assert hypothesis["verdict"] == seeded["mem"].read(
        seeded["hypothesis"])["verdict"]
    assert hypothesis["status"] == "proposed"


def test_the_section_payload_flags_live_counter_evidence(
    seeded, mkcitation, mkfact
):
    """`status` already carries this for a PROMOTED claim — contested
    against supported — but not for a `proposed` one, and a writer
    describing an unsettled claim is exactly who needs to know something
    live argues against it.

    The counter citation is attached to a fact because Graph.live_citations
    has two conditions, not one: the citation must be `verified` AND some
    active fact must still cite it. A bare citation attached to no fact is
    not live, so a version of this test without the fact would assert
    False is False and prove nothing."""
    against = mkcitation(url="https://c-example.com/p",
                         domain="c-example.com",
                         quote="a countering span here")["id"]
    mkfact(statement="the tail persists without it", citations=[against])
    seeded["mem"].update(seeded["hypothesis"], counter=[against])
    result = _apply(seeded)
    hypothesis = _sections_seeded(seeded, result)["S-001"]["hypotheses"][0]
    assert hypothesis["disputed"] is True


def test_a_section_payload_is_undisputed_when_nothing_argues_against_it(
    seeded
):
    result = _apply(seeded)
    hypothesis = _sections_seeded(seeded, result)["S-001"]["hypotheses"][0]
    assert hypothesis["disputed"] is False


def test_a_counter_citation_that_is_not_live_is_not_a_dispute(
    seeded, mkcitation, mkfact
):
    """`disputed` asks whether opposition is LIVE, the same question
    _verified_status asks before writing `contested`. A counter citation
    rejected by gate 2 — its quote is not on the page — is not evidence
    against anything, and telling a writer to report a dispute resting on
    a fabricated quote is worse than telling it nothing.

    Attached to an active fact deliberately, so the ONLY thing keeping it
    out of live_citations is its `rejected` status. Unattached it would
    fail live_citations' other condition and this test would pass without
    ever exercising the one it names."""
    dead = mkcitation(url="https://c-example.com/p", domain="c-example.com",
                      quote="a countering span here", status="rejected")["id"]
    mkfact(statement="a claim resting on a bad quote", citations=[dead])
    seeded["mem"].update(seeded["hypothesis"], counter=[dead])
    result = _apply(seeded)
    hypothesis = _sections_seeded(seeded, result)["S-001"]["hypotheses"][0]
    assert hypothesis["disputed"] is False


def test_allowed_cite_keys_exclude_a_rejected_citation(seeded):
    """A rejected citation failed gate 2 — its quote is not on the page.
    Allowing it would let the report cite something that does not exist."""
    result = _apply(seeded)
    section = _sections_seeded(seeded, result)["S-001"]
    assert seeded["citation"] in section["allowed_cite_keys"]
    assert seeded["blocked"] not in section["allowed_cite_keys"]


def test_allowed_cite_keys_are_sorted(seeded):
    result = _apply(seeded)
    section = _sections_seeded(seeded, result)["S-001"]
    assert section["allowed_cite_keys"] == sorted(section["allowed_cite_keys"])


def test_an_unverifiable_citation_is_allowed_but_flagged(mem, seeded,
                                                         mkcitation):
    """Spec section 6: a 403 is flagged, not silently trusted, and Appendix D
    must be able to disclose it. Dropping it would hide the source."""
    walled = mkcitation(url="https://c-example.com/p", domain="c-example.com",
                        quote="behind a wall here", status="unverifiable")["id"]
    fact = mem.read(seeded["fact"])
    mem.update(seeded["fact"], citations=sorted(fact["citations"] + [walled]))

    result = _apply(seeded)
    section = _sections_seeded(seeded, result)["S-001"]
    assert walled in section["allowed_cite_keys"]
    flags = {c["id"]: c["unverified"] for c in section["facts"][0]["citations"]}
    assert flags[walled] is True
    assert flags[seeded["citation"]] is False


def test_an_outline_that_drops_a_finding_is_rejected(seeded):
    artifact = {"task_id": seeded["task"]["id"], "sections": [
        {"id": "S-001", "title": "t", "hypotheses": [], "facts": []}]}
    with pytest.raises(apply.ApplyError) as caught:
        _apply(seeded, artifact)
    assert seeded["hypothesis"] in str(caught.value)


def test_a_rejected_outline_writes_no_file_and_seeds_no_tasks(seeded):
    """True by construction today -- validate() raises before anything is
    written -- but nothing pins it, so a future reordering (write the file,
    then check) would go uncaught. A half-written out/outline.json is what
    render would silently pick up on the next run, and a synthesize task
    seeded from a rejected outline would dispatch a writer for a section
    that was never actually accepted. Mirrors
    test_apply_synthesize.test_a_failing_body_writes_no_section_file."""
    artifact = {"task_id": seeded["task"]["id"], "sections": [
        {"id": "S-001", "title": "t", "hypotheses": [], "facts": []}]}
    graph = graph_mod.Graph(seeded["mem"])
    before = {task_id for task_id, _ in graph.readable("task")}
    with pytest.raises(apply.ApplyError):
        _apply(seeded, artifact)
    assert not (seeded["root"] / "out" / "outline.json").exists()
    after = {task_id for task_id, _ in graph.readable("task")}
    assert after == before


def test_the_rejection_message_names_every_problem_at_once(seeded):
    """One retry has to be able to fix everything. Three attempts spent on
    three separate complaints is three wasted dispatches."""
    artifact = {"task_id": seeded["task"]["id"], "sections": [
        {"id": "S-001", "title": "t", "hypotheses": ["H-999"], "facts": []}]}
    with pytest.raises(apply.ApplyError) as caught:
        _apply(seeded, artifact)
    message = str(caught.value)
    assert "H-999" in message and seeded["hypothesis"] in message
    assert seeded["fact"] in message


def test_applying_the_same_outline_twice_is_idempotent(seeded):
    first = _apply(seeded)
    second = _apply(seeded)
    assert first.spawned == second.spawned
    assert second.created == []


def test_an_outline_task_with_no_frozen_input_is_an_apply_error(seeded,
                                                                mktask):
    task = mktask(question="produce the outline", kind="outline")
    graph = graph_mod.Graph(seeded["mem"])
    with pytest.raises(apply.ApplyError, match="no outline in its inputs"):
        apply.apply_outline(seeded["mem"], graph, seeded["cfg"], task["id"],
                            task, {"task_id": task["id"], "sections": []},
                            root=seeded["root"])
