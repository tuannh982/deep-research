"""next is a pure function of the graph plus run.yaml. Tick N always
dispatches the same set, and running it twice does not dispatch twice."""
import json

import pytest

import ids
import journal
import research
import runconfig
import scheduler
import workspace
from graph import Graph


@pytest.fixture
def run(workspace_root):
    """An initialised workspace whose memory is the shared `mem` store."""
    return workspace_root


@pytest.fixture
def cfg(run):
    return runconfig.load(run)


def build(mem, cfg, task_ids, events=(), tick=1):
    return scheduler.build_packet(mem, Graph(mem), cfg, list(events), tick,
                                  task_ids)


# --- ids.numeric ------------------------------------------------------

def test_numeric_orders_past_three_digits():
    """Memory.ids sorts lexicographically, so F-1000 sorts before F-999.
    Anywhere this plan needs "most recent", it needs this instead."""
    assert sorted(["F-999", "F-1000"], key=ids.numeric) == ["F-999", "F-1000"]


def test_numeric_rejects_an_unparseable_id():
    with pytest.raises(ValueError):
        ids.numeric("not-an-id")


# --- the frontier and the cap ----------------------------------------

def test_the_whole_ready_frontier_is_dispatched(mem, cfg, mktask):
    for index in range(4):
        mktask(question=f"q{index}")
    packet = build(mem, cfg, Graph(mem).frontier())
    assert [d.task_id for d in packet.dispatches] == [
        "T-001", "T-002", "T-003", "T-004"]


def test_build_packet_accepts_any_number_of_task_ids(mem, cfg, mktask):
    """Renamed from test_the_frontier_is_capped_by_max_parallel: that name
    claimed to exercise the cap, but `cfg["config"]["max_parallel"]` is
    read in exactly one place — inside `run()` — and this test never
    calls `run()`. It hands build_packet an already-sliced list of 3
    ids, which only proves build_packet can build a packet of an
    arbitrary size; it would pass identically with the cap deleted or
    wired to a different key. The cap itself is exercised end to end by
    test_next_caps_the_dispatch_at_the_configured_max_parallel below."""
    for index in range(10):
        mktask(question=f"q{index}")
    packet = build(mem, cfg, Graph(mem).frontier()[:3])
    assert len(packet.dispatches) == 3


def test_the_cap_is_a_prefix_of_the_sorted_frontier(mem, cfg, mktask):
    """Given the same graph, tick N must dispatch the same tasks. A
    prefix of a sorted list is the only selection rule that guarantees
    it; anything set-based or arrival-ordered does not."""
    for index in range(10):
        mktask(question=f"q{index}")
    frontier = Graph(mem).frontier()
    assert frontier == sorted(frontier)
    packet = build(mem, cfg, frontier[:2])
    assert [d.task_id for d in packet.dispatches] == ["T-001", "T-002"]


def test_dispatches_are_sorted_by_task_id(mem, cfg, mktask):
    for index in range(3):
        mktask(question=f"q{index}")
    packet = build(mem, cfg, ["T-003", "T-001", "T-002"])
    assert [d.task_id for d in packet.dispatches] == [
        "T-001", "T-002", "T-003"]


# --- the packet is self-contained ------------------------------------

def test_every_dispatch_carries_everything_needed_to_execute_it(
    mem, cfg, mktask
):
    """Spec section 4: 'which agent, its prompt file, its input, its
    output schema, where to write the result.' A missing field here is a
    tick that cannot survive a compaction."""
    task = mktask(question="find sources", kind="search")
    dispatch = build(mem, cfg, [task["id"]]).dispatches[0]
    assert dispatch.agent == "searcher"
    assert dispatch.agent_file == "agents/searcher.md"
    assert dispatch.schema_file == "schemas/artifact.search.json"
    assert dispatch.out_path == "inbox/T-001.json"
    assert dispatch.model == cfg["models"]["searcher"]
    assert dispatch.timeout_seconds == cfg["config"]["agent_timeout"]
    assert dispatch.input["task_id"] == task["id"]


def test_the_model_comes_from_run_yaml_not_from_code(mem, cfg, mktask):
    """Spec section 5: 'Model per agent is configured in run.yaml, not
    hardcoded.'"""
    task = mktask(question="q", kind="search")
    cfg["models"]["searcher"] = "opus"
    assert build(mem, cfg, [task["id"]]).dispatches[0].model == "opus"


@pytest.mark.parametrize("kind,agent", [
    ("decompose", "decomposer"), ("search", "searcher"),
    ("extract", "extractor"), ("hypothesize", "hypothesizer"),
    ("verify", "verifier"), ("outline", "outliner"),
])
def test_every_kind_maps_to_its_agent_and_schema(kind, agent):
    assert runconfig.KIND_AGENT[kind] == agent
    assert kind in scheduler.REQUIRED_INPUT_KEYS


def test_every_dispatchable_kind_has_required_input_keys():
    """A kind with no declared inputs would dispatch a subagent an empty
    packet and burn three attempts before anyone noticed.

    Checked against runconfig.KIND_AGENT — what build_packet actually
    calls dispatchable (`KIND_AGENT.get(task["kind"])`) — rather than
    gates.ARTIFACT_KINDS, which lags by design: a kind joins that table
    only with its applier. Every dispatchable kind now has a packet, so
    the exemption Task 6 needed is gone.
    """
    assert set(scheduler.REQUIRED_INPUT_KEYS) == set(runconfig.KIND_AGENT)


# --- per-kind inputs --------------------------------------------------

def test_a_decompose_input_carries_the_scope_and_the_siblings(
    mem, cfg, mktask
):
    cfg["scope"]["in_scope"] = ["latency"]
    parent = mktask(question="parent", kind="decompose")
    mktask(question="sibling A", parent=parent["id"], depth=1)
    target = mktask(question="target", kind="decompose",
                    parent=parent["id"], depth=1)
    payload = scheduler.agent_input(mem, Graph(mem), cfg, target["id"],
                                    mem.read(target["id"]))
    assert payload["scope"]["in_scope"] == ["latency"]
    assert payload["parent_question"] == "parent"
    assert "sibling A" in payload["siblings"]
    assert payload["depth"] == 1
    assert payload["max_depth"] == cfg["config"]["max_depth"]


def test_a_decompose_input_at_the_cap_says_no_children_are_possible(
    mem, cfg, mktask
):
    """Otherwise the decomposer proposes children that submit silently
    prunes, and its attempts are spent on work that cannot land."""
    cfg["config"]["max_depth"] = 2
    task = mktask(question="deep", kind="decompose", depth=2)
    payload = scheduler.agent_input(mem, Graph(mem), cfg, task["id"], task)
    assert payload["children_allowed"] is False


def test_a_search_input_carries_the_domains_already_seen(
    mem, cfg, mktask, mkcitation
):
    """Spec section 5: the searcher gets `seen_domains`. Sorted, and
    eTLD+1, so it is asked for genuinely new sources."""
    mkcitation(url="https://b-example.com/x", domain="b-example.com", quote="a quoted span one")
    mkcitation(url="https://a-example.com/y", domain="a-example.com", quote="a quoted span two")
    task = mktask(question="find more", kind="search")
    payload = scheduler.agent_input(mem, Graph(mem), cfg, task["id"], task)
    assert payload["seen_domains"] == ["a-example.com", "b-example.com"]


def test_an_extract_input_carries_exactly_one_url(mem, cfg, mktask):
    task = mktask(question="read it", kind="extract")
    mem.update(task["id"], inputs={"url": "https://a-example.com/p",
                                   "title": "P", "domain": "a-example.com"})
    payload = scheduler.agent_input(mem, Graph(mem), cfg, task["id"],
                                    mem.read(task["id"]))
    assert payload["url"] == "https://a-example.com/p"
    assert payload["title"] == "P"


def test_an_extract_task_with_no_url_raises_rather_than_dispatching(
    mem, cfg, mktask
):
    """A scheduler bug must be loud. Dispatching an extractor with no
    page would spend a real subagent call on nothing."""
    task = mktask(question="read it", kind="extract")
    with pytest.raises(ValueError, match="url"):
        scheduler.agent_input(mem, Graph(mem), cfg, task["id"], task)


@pytest.fixture
def branch(mem, mktask, mkcitation, mkfact, mkassumption):
    root = mktask(question="root", kind="decompose")
    worker = mktask(question="w", kind="extract", parent=root["id"], depth=1,
                    status="done")
    citations = [mkcitation(url=f"https://d{i}-example.com/x",
                            domain=f"d{i}-example.com", quote=f"quoted span {i}")
                 for i in range(3)]
    for index, citation in enumerate(citations):
        mkfact(statement=f"fact {index}", citations=[citation["id"]],
               task=worker["id"])
    mkassumption(statement="v3 is current", raised_by=root["id"])
    hypothesizer = mktask(question="form claims", kind="hypothesize",
                          parent=root["id"], depth=1)
    return {"root": root["id"], "worker": worker["id"],
            "hypothesizer": hypothesizer["id"],
            "citations": [c["id"] for c in citations]}


def test_a_hypothesize_input_carries_the_branch_s_facts_and_their_quotes(
    mem, cfg, branch
):
    payload = scheduler.agent_input(
        mem, Graph(mem), cfg, branch["hypothesizer"],
        mem.read(branch["hypothesizer"]))
    assert len(payload["facts"]) == 3
    first = payload["facts"][0]
    assert first["id"] == "F-001"
    assert first["citations"][0]["domain"] == "d0-example.com"
    assert first["citations"][0]["quote"] == "quoted span 0"


def test_a_hypothesize_input_carries_the_branch_s_open_assumptions(
    mem, cfg, branch
):
    """This is what lets the hypothesizer propose a `refutes` link. It
    cannot name an assumption it was never shown."""
    payload = scheduler.agent_input(
        mem, Graph(mem), cfg, branch["hypothesizer"],
        mem.read(branch["hypothesizer"]))
    assert payload["open_assumptions"] == [
        {"id": "A-001", "statement": "v3 is current"}]


def test_a_confirmed_assumption_is_not_offered_for_refutation(
    mem, cfg, branch
):
    mem.update("A-001", status="confirmed")
    payload = scheduler.agent_input(
        mem, Graph(mem), cfg, branch["hypothesizer"],
        mem.read(branch["hypothesizer"]))
    assert payload["open_assumptions"] == []


def test_a_hypothesize_input_excludes_another_branch_s_facts(
    mem, cfg, branch, mktask, mkcitation, mkfact
):
    """Spec section 5: 'a fact cluster from one branch'. Cross-branch
    facts would let the hypothesizer form claims the outline cannot
    place."""
    other = mktask(question="other root", kind="decompose")
    worker = mktask(question="ow", kind="extract", parent=other["id"],
                    depth=1, status="done")
    citation = mkcitation(url="https://z-example.com/x", domain="z-example.com",
                          quote="elsewhere on the web")
    mkfact(statement="not in this branch", citations=[citation["id"]],
           task=worker["id"])
    payload = scheduler.agent_input(
        mem, Graph(mem), cfg, branch["hypothesizer"],
        mem.read(branch["hypothesizer"]))
    assert all("not in this branch" != f["statement"]
               for f in payload["facts"])


def test_a_hypothesize_input_excludes_a_quarantined_fact(mem, cfg, branch):
    mem.update("F-001", status="quarantined")
    payload = scheduler.agent_input(
        mem, Graph(mem), cfg, branch["hypothesizer"],
        mem.read(branch["hypothesizer"]))
    assert len(payload["facts"]) == 2


def test_a_hypothesize_input_is_capped_and_says_what_it_dropped(
    mem, cfg, branch, mkcitation, mkfact
):
    """Spec section 5: subagents 'cannot bloat their context'. On a
    multi-day run a branch accumulates hundreds of facts, and an
    uncapped packet is the one place that promise breaks."""
    for index in range(scheduler.MAX_FACTS_IN_PACKET + 5):
        citation = mkcitation(url=f"https://x{index}-example.com/p",
                              domain=f"x{index}-example.com", quote=f"a quoted span {index}")
        mkfact(statement=f"extra {index}", citations=[citation["id"]],
               task=branch["worker"])
    payload = scheduler.agent_input(
        mem, Graph(mem), cfg, branch["hypothesizer"],
        mem.read(branch["hypothesizer"]))
    assert len(payload["facts"]) == scheduler.MAX_FACTS_IN_PACKET
    assert payload["facts_omitted"] == 8


def test_the_cap_prefers_a_fact_no_claim_has_used_yet(
    mem, cfg, branch, mkcitation, mkfact, mkhypothesis
):
    """The cap is what limits evidence reuse, and it is the ONLY thing
    that does: `_branch_of` resolves through `root_branch`, which
    root_branch's own docstring calls "a constant function on a real
    run" — init seeds one parentless task and everything descends from
    it — so the packet is already run-wide, across every theme. On a run
    with 500 facts the hypothesizer sees 40 of them.

    Recency is the wrong thing to spend that budget on. A fact whose
    citation already supports some claim is the one the hypothesizer
    least needs to see again; a fact nothing has used is exactly what a
    new or under-evidenced claim might be built from. So unused facts
    come first and recency is only the tiebreak.
    """
    used = mkcitation(url="https://used-example.com/p",
                      domain="used-example.com",
                      quote="a span already spoken for")["id"]
    old_unused = mkcitation(url="https://unused-example.com/p",
                            domain="unused-example.com",
                            quote="a span nothing has claimed")["id"]
    stale = mkfact(statement="OLD BUT UNUSED", citations=[old_unused],
                   task=branch["worker"])["id"]
    mkhypothesis(claim="uses it", supporting=[used], task=branch["worker"])
    # Flood the packet past the cap with facts that are all already used.
    for index in range(scheduler.MAX_FACTS_IN_PACKET + 5):
        mkfact(statement=f"recent but used {index}", citations=[used],
               task=branch["worker"])
    payload = scheduler.agent_input(
        mem, Graph(mem), cfg, branch["hypothesizer"],
        mem.read(branch["hypothesizer"]))
    kept = [fact["id"] for fact in payload["facts"]]
    assert len(kept) <= scheduler.MAX_FACTS_IN_PACKET
    assert stale in kept, "an unused fact was dropped for newer used ones"


def test_the_cap_still_reports_how_many_it_dropped(
    mem, cfg, branch, mkcitation, mkfact
):
    """`facts_omitted` is what tells the hypothesizer not to claim
    anything about totals. Reordering the selection must not break the
    count."""
    citation = mkcitation(url="https://x-example.com/p",
                          domain="x-example.com",
                          quote="a quoted span here")["id"]
    for index in range(scheduler.MAX_FACTS_IN_PACKET + 7):
        mkfact(statement=f"extra {index}", citations=[citation],
               task=branch["worker"])
    payload = scheduler.agent_input(
        mem, Graph(mem), cfg, branch["hypothesizer"],
        mem.read(branch["hypothesizer"]))
    assert len(payload["facts"]) == scheduler.MAX_FACTS_IN_PACKET
    assert payload["facts_omitted"] > 0


def test_the_cap_keeps_the_most_recent_facts_by_number_not_by_string(
    mem, cfg, branch, mkcitation, mkfact
):
    """Lexicographic order puts F-1000 before F-999, which would silently
    hand the hypothesizer the oldest facts once a branch passes a
    thousand."""
    for index in range(scheduler.MAX_FACTS_IN_PACKET + 2):
        citation = mkcitation(url=f"https://x{index}-example.com/p",
                              domain=f"x{index}-example.com", quote=f"a quoted span {index}")
        mkfact(statement=f"extra {index}", citations=[citation["id"]],
               task=branch["worker"])
    payload = scheduler.agent_input(
        mem, Graph(mem), cfg, branch["hypothesizer"],
        mem.read(branch["hypothesizer"]))
    kept = [ids.numeric(f["id"]) for f in payload["facts"]]
    all_facts = sorted(ids.numeric(i) for i in mem.ids("fact"))
    assert kept == sorted(kept)
    # The newest survived and the oldest were dropped — the opposite of
    # what a lexicographic sort would do once ids pass three digits.
    assert max(kept) == max(all_facts)
    assert min(kept) > min(all_facts)


def test_a_verify_input_is_the_claim_and_its_quotes_and_nothing_else(
    mem, cfg, branch, mkhypothesis, mktask
):
    """Spec section 5, verbatim: 'one claim + its quotes, nothing else'.
    No graph, no history, no other hypotheses — that is what makes gate 4
    adversarial rather than confirmatory."""
    hypothesis = mkhypothesis(claim="cold starts dominate",
                              supporting=branch["citations"],
                              task=branch["hypothesizer"])
    task = mktask(question="verify it", kind="verify",
                  parent=branch["hypothesizer"], depth=1)
    mem.update(task["id"], inputs={"hypothesis": hypothesis["id"],
                                   "refutes": None})
    payload = scheduler.agent_input(mem, Graph(mem), cfg, task["id"],
                                    mem.read(task["id"]))
    assert payload["claim"] == "cold starts dominate"
    assert payload["hypothesis"] == hypothesis["id"]
    assert len(payload["quotes"]) == 3
    assert set(payload) == {"task_id", "hypothesis", "claim", "quotes"}


def test_a_verify_input_labels_each_quote_with_its_stance(
    mem, cfg, branch, mkhypothesis, mkcitation, mktask
):
    """A counter quote has always reached the verifier — `supporting +
    counter` is what fills `quotes`. Unlabelled, though, verifier.md told
    it every quote was "offered in support of" the claim, so evidence
    AGAINST the claim was read as weak evidence for it, and the one agent
    positioned to weigh a dispute could not see there was one.
    """
    against = mkcitation(url="https://against-example.com/x",
                         domain="against-example.com",
                         quote="a quoted span arguing the other way")
    hypothesis = mkhypothesis(claim="cold starts dominate",
                              supporting=branch["citations"],
                              counter=[against["id"]],
                              task=branch["hypothesizer"])
    task = mktask(question="verify it", kind="verify",
                  parent=branch["hypothesizer"], depth=1)
    mem.update(task["id"], inputs={"hypothesis": hypothesis["id"],
                                   "refutes": None})
    payload = scheduler.agent_input(mem, Graph(mem), cfg, task["id"],
                                    mem.read(task["id"]))
    assert {q["id"]: q["stance"] for q in payload["quotes"]} == {
        branch["citations"][0]: "supporting",
        branch["citations"][1]: "supporting",
        branch["citations"][2]: "supporting",
        against["id"]: "counter",
    }


def test_a_citation_on_both_sides_is_labelled_counter_exactly_once(
    mem, cfg, branch, mkhypothesis, mktask
):
    """`supporting` and `counter` overlapping is incoherent, and
    apply_hypothesize rejects a NEW artifact that does it — but a
    hypothesis already on disk can carry the overlap, which is why
    apply_verify re-checks for it at the point of use. The packet has to
    agree with how apply.py resolves it: counter wins. Otherwise the same
    span arrives twice under two contradictory labels and the verdict
    turns on which one the model read last.
    """
    shared = branch["citations"][0]
    hypothesis = mkhypothesis(claim="cold starts dominate",
                              supporting=branch["citations"],
                              counter=[shared],
                              task=branch["hypothesizer"])
    task = mktask(question="verify it", kind="verify",
                  parent=branch["hypothesizer"], depth=1)
    mem.update(task["id"], inputs={"hypothesis": hypothesis["id"],
                                   "refutes": None})
    payload = scheduler.agent_input(mem, Graph(mem), cfg, task["id"],
                                    mem.read(task["id"]))
    assert [q["id"] for q in payload["quotes"]].count(shared) == 1
    assert {q["id"]: q["stance"] for q in payload["quotes"]}[shared] == (
        "counter")


def test_a_verify_task_with_no_hypothesis_raises(mem, cfg, mktask):
    task = mktask(question="verify", kind="verify")
    with pytest.raises(ValueError, match="hypothesis"):
        scheduler.agent_input(mem, Graph(mem), cfg, task["id"], task)


# --- outline: the outliner's input packet -----------------------------
#
# `mktask` (tests/conftest.py) takes no `inputs=` kwarg, unlike the brief's
# proposed fixture change — every other kind-specific test in this file
# (extract, verify) instead calls `mem.update(task["id"], inputs={...})`
# after creating the task, so these follow that same convention rather
# than widening the shared fixture.

FROZEN_OUTLINE = {
    "question": "why is the sky blue?",
    "sections": [
        {"id": "S-001", "theme": "T-002", "title": "optical scattering",
         "hypotheses": ["H-001"], "facts": ["F-001"]},
    ],
    "orphans": {"hypotheses": [], "facts": []},
    "empty_themes": [],
}


def test_outline_packet_carries_claims_not_bare_ids(mem, cfg, mktask, mkfact,
                                                     mkhypothesis):
    theme = mktask(question="optical scattering", depth=1)["id"]
    hypothesis = mkhypothesis(claim="short wavelengths scatter",
                              task=theme)["id"]
    fact = mkfact(statement="blue scatters more", task=theme)["id"]
    frozen = {**FROZEN_OUTLINE, "sections": [
        {**FROZEN_OUTLINE["sections"][0], "theme": theme,
         "hypotheses": [hypothesis], "facts": [fact]}]}
    task = mktask(question="produce the outline", kind="outline")
    mem.update(task["id"], inputs={"outline": frozen})

    payload = scheduler.agent_input(mem, Graph(mem), cfg, task["id"],
                                    mem.read(task["id"]))

    assert payload["sections"][0]["hypotheses"] == [
        {"id": hypothesis, "claim": "short wavelengths scatter"}]
    assert payload["sections"][0]["facts"] == [
        {"id": fact, "statement": "blue scatters more"}]


def test_outline_packet_does_not_leak_the_theme(mem, cfg, mktask):
    """`theme` is topology. apply_artifact refuses to read it back from the
    artifact, so sending it can only invite the model to change it."""
    task = mktask(question="produce the outline", kind="outline")
    mem.update(task["id"], inputs={"outline": FROZEN_OUTLINE})
    payload = scheduler.agent_input(mem, Graph(mem), cfg, task["id"],
                                    mem.read(task["id"]))
    assert "theme" not in payload["sections"][0]


def test_outline_packet_skips_a_dangling_hypothesis(mem, cfg, mktask):
    """A frozen outline names ids that a cascade may have deleted between
    seed and dispatch. Indexing would raise KeyError straight out of
    `research next`, which is the only way forward from an in-flight tick."""
    task = mktask(question="produce the outline", kind="outline")
    mem.update(task["id"], inputs={"outline": FROZEN_OUTLINE})
    payload = scheduler.agent_input(mem, Graph(mem), cfg, task["id"],
                                    mem.read(task["id"]))
    assert payload["sections"][0]["hypotheses"] == []


def test_an_outline_task_with_no_frozen_outline_is_rejected(mem, cfg, mktask):
    task = mktask(question="produce the outline", kind="outline")
    with pytest.raises(ValueError, match="no outline in its inputs"):
        scheduler.agent_input(mem, Graph(mem), cfg, task["id"], task)


def test_a_broken_outline_task_is_skipped_not_fatal(mem, cfg, mktask):
    """build_packet catches ValueError per task. One unbuildable task must
    not cost the whole tick."""
    mktask(question="produce the outline", kind="outline")
    healthy = mktask(question="a search", kind="search")["id"]
    packet = build(mem, cfg, Graph(mem).frontier())
    assert healthy in [d.task_id for d in packet.dispatches]
    assert any("outline" in reason for _, reason in packet.skipped)


# --- synthesize: the synthesizer's input packet ------------------------
#
# `mktask` takes no `inputs=` kwarg (see the outline section above), so
# these follow the same convention: create the task, then
# `mem.update(task["id"], inputs={...})`.

FROZEN_SECTION = {
    "id": "S-001", "title": "Optical scattering",
    "hypotheses": [{"id": "H-001", "claim": "Rayleigh explains it",
                    "confidence": 0.75, "status": "supported"}],
    "facts": [{"id": "F-001", "statement": "blue scatters more",
               "citations": [{"id": "C-001", "domain": "a-example.com",
                              "quote": "short wavelengths scatter",
                              "unverified": False}]}],
    "allowed_cite_keys": ["C-001"],
}


def test_synthesize_packet_forwards_the_frozen_section(mem, cfg, mktask):
    task = mktask(question="write section S-001", kind="synthesize")
    mem.update(task["id"], inputs={"section": FROZEN_SECTION})
    payload = scheduler.agent_input(mem, Graph(mem), cfg, task["id"],
                                    mem.read(task["id"]))
    assert payload["section"] == FROZEN_SECTION
    assert payload["question"] == cfg["question"]


def test_synthesize_packet_carries_no_build_error_by_default(mem, cfg,
                                                              mktask):
    task = mktask(question="write section S-001", kind="synthesize")
    mem.update(task["id"], inputs={"section": FROZEN_SECTION})
    payload = scheduler.agent_input(mem, Graph(mem), cfg, task["id"],
                                    mem.read(task["id"]))
    assert payload["build_error"] is None


def test_a_build_error_reaches_the_synthesizer_on_a_retry(mem, cfg, mktask):
    """render re-opens the offending section's task with the tectonic error
    in its inputs. If the packet drops it the retry is blind and produces
    the same broken LaTeX."""
    task = mktask(question="write section S-001", kind="synthesize")
    mem.update(task["id"], inputs={
        "section": FROZEN_SECTION,
        "build_error": "Undefined control sequence \\foo"})
    payload = scheduler.agent_input(mem, Graph(mem), cfg, task["id"],
                                    mem.read(task["id"]))
    assert "\\foo" in payload["build_error"]


def test_a_synthesize_task_with_no_section_is_rejected(mem, cfg, mktask):
    task = mktask(question="write a section", kind="synthesize")
    with pytest.raises(ValueError, match="no section in its inputs"):
        scheduler.agent_input(mem, Graph(mem), cfg, task["id"], task)


# --- a corrupt hypothesis must not cost the whole tick -----------------
#
# scripts/scheduler.py:198's `memory.read(hypothesis_id)` in the verify
# branch was unguarded, unlike every sibling read in this function. A
# dangling hypothesis id raised an uncaught KeyError straight out of
# research.main() as a traceback (the CLI only catches WorkspaceError and
# ValueError); a corrupt or schema-invalid one raised inside build_packet
# and took down the entire tick — zero dispatches, zero journal record,
# and healthy unrelated tasks in the same frontier left pending. The fix
# guards the read (like the citation reads around it) and turns the
# failure into a per-task skip in build_packet, not a raise that reaches
# run().

def _strip_line(path, line_prefix):
    """Delete every line starting with `line_prefix` from a node file,
    leaving it parseable but schema-invalid. Mirrors
    tests/test_fsck.py's helper of the same name."""
    text = "".join(
        line for line in path.read_text().splitlines(keepends=True)
        if not line.startswith(line_prefix)
    )
    path.write_text(text)


def test_a_verify_task_naming_a_dangling_hypothesis_raises_a_named_error(
    mem, cfg, mktask
):
    task = mktask(question="verify", kind="verify")
    mem.update(task["id"], inputs={"hypothesis": "H-999", "refutes": None})
    with pytest.raises(ValueError, match="H-999"):
        scheduler.agent_input(mem, Graph(mem), cfg, task["id"],
                              mem.read(task["id"]))


def test_a_verify_task_naming_an_unparseable_hypothesis_raises_a_named_error(
    mem, cfg, mktask, mkhypothesis
):
    hypothesis = mkhypothesis()
    mem.path_for(hypothesis["id"]).write_text("not a node at all\n")
    task = mktask(question="verify", kind="verify")
    mem.update(task["id"], inputs={"hypothesis": hypothesis["id"],
                                   "refutes": None})
    with pytest.raises(ValueError, match=hypothesis["id"]):
        scheduler.agent_input(mem, Graph(mem), cfg, task["id"],
                              mem.read(task["id"]))


def test_a_verify_task_naming_a_schema_invalid_hypothesis_names_both_ids(
    mem, cfg, mktask, mkhypothesis
):
    """Distinct from the dangling/unparseable cases above:
    memory.validate() already raises a ValueError subclass
    (ValidationError) whose own message already names the hypothesis id,
    so asserting only that would pass identically with or without
    agent_input's guard around it — it would not discriminate. What the
    guard adds is naming the VERIFY TASK's id too: a bare ValidationError
    from validating the hypothesis alone never mentions the task that
    referenced it."""
    hypothesis = mkhypothesis()
    _strip_line(mem.path_for(hypothesis["id"]), "confidence:")
    task = mktask(question="verify", kind="verify")
    mem.update(task["id"], inputs={"hypothesis": hypothesis["id"],
                                   "refutes": None})
    with pytest.raises(ValueError) as excinfo:
        scheduler.agent_input(mem, Graph(mem), cfg, task["id"],
                              mem.read(task["id"]))
    message = str(excinfo.value)
    assert task["id"] in message
    assert hypothesis["id"] in message


def test_build_packet_skips_a_task_with_a_corrupt_hypothesis_and_keeps_going(
    mem, cfg, mktask, mkhypothesis
):
    hypothesis = mkhypothesis()
    mem.path_for(hypothesis["id"]).write_text("not a node at all\n")
    bad = mktask(question="verify", kind="verify")
    mem.update(bad["id"], inputs={"hypothesis": hypothesis["id"],
                                  "refutes": None})
    healthy = mktask(question="q", kind="search")
    packet = build(mem, cfg, [bad["id"], healthy["id"]])
    assert [d.task_id for d in packet.dispatches] == [healthy["id"]]
    assert len(packet.skipped) == 1
    skipped_id, reason = packet.skipped[0]
    assert skipped_id == bad["id"]
    assert hypothesis["id"] in reason


def test_the_rendered_packet_names_a_skipped_task(mem, cfg, mktask,
                                                   mkhypothesis):
    hypothesis = mkhypothesis()
    mem.path_for(hypothesis["id"]).write_text("not a node at all\n")
    bad = mktask(question="verify", kind="verify")
    mem.update(bad["id"], inputs={"hypothesis": hypothesis["id"],
                                  "refutes": None})
    healthy = mktask(question="q", kind="search")
    packet = build(mem, cfg, [bad["id"], healthy["id"]])
    text = scheduler.render(packet, cfg, workspace.skill_dir(), "research")
    assert bad["id"] in text
    assert hypothesis["id"] in text
    assert "SKIPPED" in text


def test_next_dispatches_the_healthy_sibling_when_another_task_is_corrupt(
    run, mem, mktask, mkhypothesis, capsys
):
    """The end-to-end reproduction of the bug: before the fix, this raised
    an uncaught KeyError out of research.main() as a raw traceback."""
    hypothesis = mkhypothesis()
    mem.path_for(hypothesis["id"]).write_text("not a node at all\n")
    bad = mktask(question="verify", kind="verify")
    mem.update(bad["id"], inputs={"hypothesis": hypothesis["id"],
                                  "refutes": None})
    healthy = mktask(question="q", kind="search")
    assert research.main(["next", "--root", str(run)]) == 0
    out = capsys.readouterr().out
    assert bad["id"] in out
    record = journal.dispatched_for_tick(journal.read(run), 1)
    assert record["task_ids"] == [healthy["id"]]
    assert mem.read(bad["id"])["status"] == "pending"
    assert mem.read(healthy["id"])["status"] == "running"


def test_next_journals_a_skip_alongside_the_dispatch(
    run, mem, mktask, mkhypothesis, capsys
):
    """Fix round 1 (submit's Task 16 carry-forward, replaced): `next`
    already knows exactly which task it declined to dispatch and why —
    `packet.skipped` — and journaling that decision is what lets `submit`
    later charge an attempt against it without re-deriving the same
    judgement itself by re-walking the whole frontier."""
    hypothesis = mkhypothesis()
    mem.path_for(hypothesis["id"]).write_text("not a node at all\n")
    bad = mktask(question="verify", kind="verify")
    mem.update(bad["id"], inputs={"hypothesis": hypothesis["id"],
                                  "refutes": None})
    healthy = mktask(question="q", kind="search")
    assert research.main(["next", "--root", str(run)]) == 0
    events = journal.read(run)
    skips = [e for e in events if e["event"] == "dispatch_skipped"]
    assert len(skips) == 1
    assert skips[0]["tick"] == 1
    assert skips[0]["task"] == bad["id"]
    assert hypothesis["id"] in skips[0]["reason"]
    # The healthy dispatch is unaffected — the skip is recorded
    # alongside it, not instead of it.
    assert journal.dispatched_for_tick(events, 1)["task_ids"] == [healthy["id"]]


def test_an_all_skipped_frontier_consumes_a_tick_so_it_can_age_out(
    run, mem, mktask, mkhypothesis, capsys
):
    """An earlier round refused to consume a tick when every candidate was
    skipped, reasoning that nothing was really dispatched. That was a
    permanent livelock: the early return also skipped the
    `dispatch_skipped` journaling, so `submit` step 6 never charged an
    attempt, the task was never abandoned, `frontier()` still saw it as
    open, and four consecutive `next` calls changed nothing at all with
    real work outstanding and no halt.

    The tick IS consumed, and a `dispatched` record with an empty
    `task_ids` is written alongside the skips — without it `submit --tick
    1` raises "was never dispatched" and the skips age nothing out. The
    printed text points at that submit rather than saying there is
    nothing to do.
    """
    hypothesis = mkhypothesis()
    mem.path_for(hypothesis["id"]).write_text("not a node at all\n")
    bad = mktask(question="verify", kind="verify")
    mem.update(bad["id"], inputs={"hypothesis": hypothesis["id"],
                                  "refutes": None})
    assert research.main(["next", "--root", str(run)]) == 0
    assert runconfig.load(run)["status"]["tick"] == 1
    record = journal.dispatched_for_tick(journal.read(run), 1)
    assert record is not None and record["task_ids"] == []
    skips = [e for e in journal.read(run) if e["event"] == "dispatch_skipped"]
    assert [s["task"] for s in skips] == [bad["id"]]
    out = capsys.readouterr().out
    assert bad["id"] in out
    assert "research submit --tick 1" in out


def test_an_all_skipped_frontier_ages_out_and_is_abandoned(
    run, mem, mktask, mkhypothesis
):
    """The end-to-end termination property C5 is really about: a frontier
    nothing can build must reach `abandoned` in bounded time, not hold
    the run open forever. Three next/submit rounds, one per max_attempts.
    """
    hypothesis = mkhypothesis()
    mem.path_for(hypothesis["id"]).write_text("not a node at all\n")
    bad = mktask(question="verify", kind="verify")
    mem.update(bad["id"], inputs={"hypothesis": hypothesis["id"],
                                  "refutes": None})
    for tick in (1, 2, 3):
        assert research.main(["next", "--root", str(run)]) == 0
        assert research.main(["submit", "--root", str(run),
                              "--tick", str(tick)]) == 0
    assert mem.read(bad["id"])["status"] == "abandoned"


def test_an_all_skipped_frontier_consumes_a_tick_under_a_small_cap(
    run, mem, mktask, mkhypothesis
):
    """The same, walking past the cap to look for a healthy task: with
    only corrupt tasks in the frontier the walk finds nothing
    dispatchable, and must still record every skip so all of them age
    out — regardless of how small the cap is."""
    bad_ids = []
    for _ in range(2):
        hypothesis = mkhypothesis()
        mem.path_for(hypothesis["id"]).write_text("not a node at all\n")
        bad = mktask(question="verify", kind="verify")
        mem.update(bad["id"], inputs={"hypothesis": hypothesis["id"],
                                      "refutes": None})
        bad_ids.append(bad["id"])
    cfg = runconfig.load(run)
    cfg["config"]["max_parallel"] = 1
    runconfig.save(run, cfg)
    assert research.main(["next", "--root", str(run)]) == 0
    assert runconfig.load(run)["status"]["tick"] == 1
    record = journal.dispatched_for_tick(journal.read(run), 1)
    assert record is not None and record["task_ids"] == []
    skips = [e for e in journal.read(run) if e["event"] == "dispatch_skipped"]
    assert sorted(s["task"] for s in skips) == sorted(bad_ids)


def test_a_search_packet_carries_its_stance(mem, cfg, mktask):
    """A refute search and an ordinary search are the same task kind and
    the same agent, distinguished only by this. Without it in the packet
    the searcher cannot tell that its question — written by code as "find
    evidence that would show this claim is false" — is asking it to look
    for the opposite of what it usually looks for."""
    task = mktask(question="show this is false: X", kind="search")
    mem.update(task["id"], inputs={"for_hypothesis": "H-001",
                                   "stance": "against"})
    payload = scheduler.agent_input(mem, Graph(mem), cfg, task["id"],
                                    mem.read(task["id"]))
    assert payload["stance"] == "against"


def test_a_search_with_no_stance_is_for(mem, cfg, mktask):
    """Absent means `for`. EVERY search task written before this field
    existed has no stance in its inputs, and REQUIRED_INPUT_KEYS checks
    presence — so without the default, upgrading mid-run would fail to
    build a packet for every outstanding search at once. Same
    absent-means-default convention as schemas/assumption.json's
    `cascaded`."""
    task = mktask(question="ordinary search", kind="search")
    payload = scheduler.agent_input(mem, Graph(mem), cfg, task["id"],
                                    mem.read(task["id"]))
    assert payload["stance"] == "for"


# --- the first tick refuses an unscoped run ---------------------------
#
# runconfig.default writes three empty lists; the decomposer argues every
# child it proposes against `in_scope` (this module's decompose branch);
# and halt has NO budget condition, so an unscoped run is bounded only by
# max_depth. research-brainstorming/SKILL.md said "an empty scope is an
# unbounded run" and nothing checked. Same day-zero discipline as init's
# tectonic refusal: fail before the cost, not after three days of it.

def _unscope(root):
    """A workspace as `research init` leaves it, before any scoping."""
    cfg = runconfig.load(root)
    cfg["scope"] = {"in_scope": [], "out_of_scope": [], "success_criteria": []}
    runconfig.save(root, cfg)
    return root


def test_the_first_tick_refuses_an_empty_scope(run, mktask, capsys):
    _unscope(run)
    mktask(question="root", kind="decompose")
    assert research.main(["next", "--root", str(run)]) == 1
    assert runconfig.load(run)["status"]["tick"] == 0


def test_the_refusal_names_the_skill_that_fixes_it(run, mktask, capsys):
    """An error that says what is wrong and not what to do costs a round
    trip every time it fires."""
    _unscope(run)
    mktask(question="root", kind="decompose")
    research.main(["next", "--root", str(run)])
    message = capsys.readouterr().err
    assert "research-brainstorming" in message
    assert "--allow-empty-scope" in message


def test_allow_empty_scope_proceeds(run, mktask, capsys):
    """Mirrors init's --allow-missing-tectonic. Someone who means it can
    proceed; they have to type it."""
    _unscope(run)
    mktask(question="root", kind="decompose")
    assert research.main(
        ["next", "--root", str(run), "--allow-empty-scope"]) == 0
    assert runconfig.load(run)["status"]["tick"] == 1


def test_a_later_tick_does_not_check_the_scope(run, mktask, capsys):
    """Fires once, at tick 0. By tick 2 the task tree exists and the
    decision is no longer free — refusing then would strand a running
    graph over a choice that can no longer be unmade."""
    mktask(question="root", kind="decompose")
    assert research.main(["next", "--root", str(run)]) == 0
    _unscope(run)
    assert runconfig.load(run)["status"]["tick"] == 1
    assert research.main(["next", "--root", str(run)]) == 0


def test_a_populated_scope_dispatches_normally(run, mktask, capsys):
    """Guards the guard: a check that never passes is indistinguishable
    from one that always fires."""
    mktask(question="root", kind="decompose")
    assert research.main(["next", "--root", str(run)]) == 0
    assert runconfig.load(run)["status"]["tick"] == 1


def test_an_empty_frontier_still_does_not_consume_a_tick(run, mem, mktask,
                                                         capsys):
    """The guard that stays. A run waiting on one slow task must not burn
    tick numbers forever with no `dispatched` records to show for it —
    that is a different situation from an all-skipped frontier, because
    there is genuinely nothing for `submit` to act on."""
    task = mktask(question="in flight")
    mem.update(task["id"], status="running")
    assert research.main(["next", "--root", str(run)]) == 0
    assert runconfig.load(run)["status"]["tick"] == 0
    assert journal.dispatched_for_tick(journal.read(run), 1) is None


# --- the cap must be applied AFTER skip-filtering, not before ---------
#
# run() used to slice the frontier to `cap` ids and only then hand them to
# build_packet, which filters out the corrupt ones. If corrupt tasks fill
# the entire capped slice, a healthy task sitting later in the frontier is
# never even attempted. Reproduced with --serial (cap 1) and max_parallel
# 1: a corrupt verify task sorting ahead of a healthy search task made
# every `next` call report "dispatching 0", skip the corrupt one, never
# advance the tick, and never attempt the healthy task — an indefinite,
# easy-to-miss stall with real work outstanding. Round 1's "do not consume
# a tick" guard was doing its job (stopping a runaway); nothing else was
# happening either.

def test_serial_reaches_a_healthy_task_past_a_corrupt_one_sorting_first(
    run, mem, mktask, mkhypothesis, capsys
):
    """--serial caps the frontier at 1 dispatch. A corrupt task created
    first (and so sorting first) must not hide the healthy task created
    after it: the walk has to look past the corrupt one to find the
    dispatch --serial is capped at."""
    hypothesis = mkhypothesis()
    mem.path_for(hypothesis["id"]).write_text("not a node at all\n")
    bad = mktask(question="verify", kind="verify")
    mem.update(bad["id"], inputs={"hypothesis": hypothesis["id"],
                                  "refutes": None})
    healthy = mktask(question="q", kind="search")
    assert research.main(["next", "--root", str(run), "--serial"]) == 0
    out = capsys.readouterr().out
    assert bad["id"] in out
    record = journal.dispatched_for_tick(journal.read(run), 1)
    assert record is not None
    assert record["task_ids"] == [healthy["id"]]
    assert runconfig.load(run)["status"]["tick"] == 1


def test_a_cap_of_one_reaches_a_healthy_task_past_a_corrupt_one_sorting_first(
    run, mem, mktask, mkhypothesis
):
    """The same scenario as above, through an ordinary configured
    max_parallel=1 rather than --serial — both paths share one
    scheduler, and both must share this fix."""
    hypothesis = mkhypothesis()
    mem.path_for(hypothesis["id"]).write_text("not a node at all\n")
    bad = mktask(question="verify", kind="verify")
    mem.update(bad["id"], inputs={"hypothesis": hypothesis["id"],
                                  "refutes": None})
    healthy = mktask(question="q", kind="search")
    cfg = runconfig.load(run)
    cfg["config"]["max_parallel"] = 1
    runconfig.save(run, cfg)
    assert research.main(["next", "--root", str(run)]) == 0
    record = journal.dispatched_for_tick(journal.read(run), 1)
    assert record is not None
    assert record["task_ids"] == [healthy["id"]]
    assert runconfig.load(run)["status"]["tick"] == 1


def test_the_cap_counts_only_successful_dispatches_not_ids_walked(
    mem, cfg, mktask, mkhypothesis
):
    """3 corrupt tasks sort ahead of 3 healthy ones; cap=2. The cap must
    count only the successful dispatches, not the ids read to find them:
    exactly the first 2 healthy tasks are dispatched, all 3 corrupt ones
    are recorded as skipped, and the 3rd healthy task is never reached
    (neither dispatched nor skipped) because the cap was already met."""
    bad_ids = []
    for _ in range(3):
        hypothesis = mkhypothesis()
        mem.path_for(hypothesis["id"]).write_text("not a node at all\n")
        bad = mktask(question="verify", kind="verify")
        mem.update(bad["id"], inputs={"hypothesis": hypothesis["id"],
                                      "refutes": None})
        bad_ids.append(bad["id"])
    healthy_ids = [mktask(question=f"q{index}", kind="search")["id"]
                   for index in range(3)]
    packet = scheduler.build_packet(mem, Graph(mem), cfg, [], 1,
                                    bad_ids + healthy_ids, cap=2)
    assert [d.task_id for d in packet.dispatches] == healthy_ids[:2]
    assert [task_id for task_id, _ in packet.skipped] == bad_ids


# --- retry prompts ----------------------------------------------------

def test_a_previously_rejected_task_carries_its_validator_error(
    mem, cfg, mktask
):
    """Spec section 4: 'next re-emits the task with the validator error
    appended to its prompt.'"""
    task = mktask(question="q", kind="search")
    mem.update(task["id"], attempts=1)
    events = [{"event": "artifact_rejected", "task": task["id"],
               "error": "sources/0/relevance: 5 is greater than 1"}]
    dispatch = build(mem, cfg, [task["id"]], events).dispatches[0]
    assert "relevance" in dispatch.retry_error
    assert dispatch.attempt == 2


def test_a_task_that_never_failed_carries_no_error(mem, cfg, mktask):
    task = mktask(question="q", kind="search")
    dispatch = build(mem, cfg, [task["id"]]).dispatches[0]
    assert dispatch.retry_error is None
    assert dispatch.attempt == 1


def test_the_rendered_packet_includes_the_retry_error(mem, cfg, mktask):
    task = mktask(question="q", kind="search")
    events = [{"event": "artifact_rejected", "task": task["id"],
               "error": "sources/0/relevance: 5 is greater than 1"}]
    packet = build(mem, cfg, [task["id"]], events)
    text = scheduler.render(packet, cfg, workspace.skill_dir(), "research")
    assert "relevance" in text


# --- rendering --------------------------------------------------------

def test_the_rendered_packet_has_the_shape_the_spec_shows(mem, cfg, mktask):
    for index in range(2):
        mktask(question=f"q{index}", kind="search")
    packet = build(mem, cfg, ["T-001", "T-002"], tick=7)
    text = scheduler.render(packet, cfg, workspace.skill_dir(), "research")
    assert "TICK 7" in text
    assert "IN PARALLEL" in text
    assert "agents/searcher.md" in text
    assert "schemas/artifact.search.json" in text
    assert "research/inbox/T-001.json" in text
    assert "submit --tick 7" in text


def test_the_rendered_input_is_valid_json_on_one_line(mem, cfg, mktask):
    """The agent copies this into a subagent prompt. A pretty-printed
    dict split over twenty lines is a transcription error waiting to
    happen."""
    task = mktask(question="q", kind="search")
    packet = build(mem, cfg, [task["id"]])
    line = next(l for l in scheduler.render(
        packet, cfg, workspace.skill_dir(), "research").splitlines()
        if "task_id" in l)
    payload = json.loads(line[line.index("{"):])
    assert payload["task_id"] == task["id"]


def test_the_rendered_packet_names_the_model_per_dispatch(mem, cfg, mktask):
    mktask(question="q", kind="search")
    cfg["models"]["searcher"] = "haiku"
    packet = build(mem, cfg, ["T-001"])
    assert "haiku" in scheduler.render(packet, cfg, workspace.skill_dir(),
                                        "research")


def test_the_digest_appears_every_twenty_five_ticks(mem, cfg, mktask):
    mktask(question="q", kind="search")
    on = build(mem, cfg, ["T-001"], tick=scheduler.DIGEST_EVERY)
    off = build(mem, cfg, ["T-001"], tick=scheduler.DIGEST_EVERY + 1)
    assert on.digest is not None
    assert off.digest is None


def test_the_digest_never_stops_the_loop(mem, cfg, mktask):
    """Spec section 4: 'Notice, not gate — it never stops the loop.'"""
    mktask(question="q", kind="search")
    packet = build(mem, cfg, ["T-001"], tick=scheduler.DIGEST_EVERY)
    assert packet.dispatches


# --- the CLI: halts, checkpoints and tick idempotence ----------------

def test_next_dispatches_and_advances_the_tick(run, mem, mktask, capsys):
    mktask(question="q", kind="search")
    assert research.main(["next", "--root", str(run)]) == 0
    assert runconfig.load(run)["status"]["tick"] == 1
    assert "TICK 1" in capsys.readouterr().out


def test_next_marks_dispatched_tasks_running(run, mem, mktask):
    task = mktask(question="q", kind="search")
    research.main(["next", "--root", str(run)])
    assert mem.read(task["id"])["status"] == "running"


def test_next_journals_the_dispatch(run, mem, mktask):
    mktask(question="q", kind="search")
    research.main(["next", "--root", str(run)])
    record = journal.dispatched_for_tick(journal.read(run), 1)
    assert record["task_ids"] == ["T-001"]


def test_running_next_twice_reprints_the_same_packet(run, mem, mktask,
                                                     capsys):
    """The compaction recovery path. A second next must not dispatch a
    second time, and must not print an empty packet just because the
    first one took its tasks off the frontier."""
    mktask(question="q", kind="search")
    research.main(["next", "--root", str(run)])
    first = capsys.readouterr().out
    research.main(["next", "--root", str(run)])
    second = capsys.readouterr().out
    assert "T-001" in second
    assert "TICK 1" in second
    assert runconfig.load(run)["status"]["tick"] == 1
    assert len([e for e in journal.read(run)
                if e["event"] == "dispatched"]) == 1


def test_a_reprint_is_marked_as_one(run, mem, mktask, capsys):
    mktask(question="q", kind="search")
    research.main(["next", "--root", str(run)])
    capsys.readouterr()
    research.main(["next", "--root", str(run)])
    assert "already dispatched" in capsys.readouterr().out.lower()


def test_next_advances_once_the_tick_is_submitted(run, mem, mktask):
    """A real `submit` never leaves a dispatched task `running` once its
    tick is marked submitted — it either applies an artifact or requeues
    the task via the timeout path. So the state this test builds keeps
    T-002 blocked, not stuck `running`: T-001 is dispatched and finishes,
    which frees T-002 onto the frontier for the second `next`."""
    t1 = mktask(question="q", kind="search")
    mktask(question="q2", kind="search", depends_on=[t1["id"]])
    research.main(["next", "--root", str(run)])
    assert journal.dispatched_for_tick(journal.read(run), 1)["task_ids"] == \
        [t1["id"]]
    journal.append(run, "tick_submitted", tick=1)
    mem.update(t1["id"], status="done")
    research.main(["next", "--root", str(run)])
    assert runconfig.load(run)["status"]["tick"] == 2


def test_serial_caps_the_frontier_at_one(run, mem, mktask, capsys):
    """Spec section 4: '--serial caps frontier width at 1 for debugging;
    both paths share one scheduler.'"""
    for index in range(4):
        mktask(question=f"q{index}", kind="search")
    research.main(["next", "--root", str(run), "--serial"])
    assert journal.dispatched_for_tick(journal.read(run), 1)["task_ids"] == \
        ["T-001"]


def test_next_caps_the_dispatch_at_the_configured_max_parallel(run, mem,
                                                                mktask):
    """Drives the cap through the actual path that reads it —
    `cfg["config"]["max_parallel"]` is consulted in exactly one place,
    inside `run()` — unlike test_build_packet_accepts_any_number_of_
    task_ids, which hands build_packet an already-sliced list and would
    pass the same way if the cap were bypassed entirely."""
    for index in range(5):
        mktask(question=f"q{index}", kind="search")
    cfg = runconfig.load(run)
    cfg["config"]["max_parallel"] = 3
    runconfig.save(run, cfg)
    research.main(["next", "--root", str(run)])
    record = journal.dispatched_for_tick(journal.read(run), 1)
    assert record["task_ids"] == ["T-001", "T-002", "T-003"]


def test_next_halts_instead_of_dispatching(run, mem, mktask, capsys):
    task = mktask(question="q", kind="search")
    mem.update(task["id"], status="done")
    assert research.main(["next", "--root", str(run)]) == 0
    out = capsys.readouterr().out
    assert "HALT(coverage)" in out
    assert journal.dispatched_for_tick(journal.read(run), 1) is None


def test_a_halt_writes_out_status_md(run, mem, mktask):
    """Spec section 4: 'Any predicate firing writes out/status.md'."""
    task = mktask(question="q", kind="search")
    mem.update(task["id"], status="done")
    research.main(["next", "--root", str(run)])
    assert (run / "out" / "status.md").is_file()


def test_a_halt_is_recorded_in_run_yaml(run, mem, mktask):
    task = mktask(question="q", kind="search")
    mem.update(task["id"], status="done")
    research.main(["next", "--root", str(run)])
    assert runconfig.load(run)["status"]["halted"]["reason"] == "coverage"


def test_a_stored_halt_is_reprinted_rather_than_re_evaluated(run, mem,
                                                            mktask, capsys):
    """Halting is non-blocking: the user comes back days later. next must
    not quietly resume just because the graph changed under it."""
    task = mktask(question="q", kind="search")
    mem.update(task["id"], status="done")
    research.main(["next", "--root", str(run)])
    capsys.readouterr()
    mktask(question="new work", kind="search")
    research.main(["next", "--root", str(run)])
    out = capsys.readouterr().out
    assert "HALT" in out
    assert "research continue" in out


def test_a_pending_checkpoint_pauses_the_loop(run, mem, mktask, capsys):
    """Spec section 4: 'the loop pauses at the next tick and asks the
    user, rather than letting the model quietly decide it is
    satisfied.'"""
    mktask(question="q", kind="search")
    research.main(["signal", "--root", str(run), "checkpoint",
                   "--note", "check with me"])
    capsys.readouterr()
    assert research.main(["next", "--root", str(run)]) == 0
    out = capsys.readouterr().out
    assert "CHECKPOINT" in out
    assert "check with me" in out
    assert journal.dispatched_for_tick(journal.read(run), 1) is None


def test_a_checkpoint_is_checked_before_a_halt(run, mem, mktask, capsys):
    """The user asked a question. Answer it before announcing a
    conclusion."""
    task = mktask(question="q", kind="search")
    mem.update(task["id"], status="done")
    research.main(["signal", "--root", str(run), "checkpoint",
                   "--note", "ask me"])
    capsys.readouterr()
    research.main(["next", "--root", str(run)])
    assert "CHECKPOINT" in capsys.readouterr().out


def test_an_empty_frontier_with_work_in_flight_says_so(run, mem, mktask,
                                                        capsys):
    """Neither a halt nor a dispatch. Without this the operator sees an
    empty packet and no explanation.

    The state built here — a task `running` with no `dispatched` journal
    record for it — is not synthetic. `run()`'s dispatch loop marks every
    task `running` in a loop and only appends the `dispatched` record
    after that loop finishes; a crash in the gap between the two leaves
    exactly this. It is the compaction-recovery scenario one step earlier
    than the reprint case (dispatched record exists, no submit yet): here
    the crash landed before the record was even written, so there is
    nothing to reprint from, and the operator needs telling rather than
    an empty packet."""
    task = mktask(question="q", kind="search")
    mem.update(task["id"], status="running")
    assert research.main(["next", "--root", str(run)]) == 0
    assert "in flight" in capsys.readouterr().out


def test_next_on_an_uninitialised_directory_says_so(tmp_path, capsys):
    assert research.main(["next", "--root", str(tmp_path / "nope")]) == 1
    assert "research init" in capsys.readouterr().err


# --- C3: `next` is the only way forward from an in-flight tick, so it
# must not raise. Each of these was a KeyError straight out of
# research.main, with no way to reprint the packet and nothing to submit.

def test_next_reprints_a_tick_whose_task_file_has_been_deleted(
    run, mem, mktask, capsys
):
    """`task_ids` on the reprint path comes from a journal record, not
    from graph.frontier(), so it is NOT pre-filtered through
    valid_task_ids(). Between the dispatch and the reprint the file can be
    deleted; `graph.tasks[task_id]` was a bare KeyError."""
    healthy = mktask(question="q", kind="search")
    gone = mktask(question="doomed", kind="search")
    for task in (healthy, gone):
        mem.update(task["id"], status="running")
    journal.append(run, "dispatched", tick=1,
                   task_ids=sorted([healthy["id"], gone["id"]]),
                   agents={}, models={})
    cfg = runconfig.load(run)
    cfg["status"]["tick"] = 1
    runconfig.save(run, cfg)
    mem.path_for(gone["id"]).unlink()

    assert research.main(["next", "--root", str(run)]) == 0
    out = capsys.readouterr().out
    assert healthy["id"] in out
    assert gone["id"] in out
    assert "SKIPPED" in out


def test_next_reprints_a_tick_whose_task_file_is_unparseable(
    run, mem, mktask, capsys
):
    healthy = mktask(question="q", kind="search")
    garbled = mktask(question="garbled", kind="search")
    for task in (healthy, garbled):
        mem.update(task["id"], status="running")
    journal.append(run, "dispatched", tick=1,
                   task_ids=sorted([healthy["id"], garbled["id"]]),
                   agents={}, models={})
    cfg = runconfig.load(run)
    cfg["status"]["tick"] = 1
    runconfig.save(run, cfg)
    mem.path_for(garbled["id"]).write_text("garbage\n", encoding="utf-8")

    assert research.main(["next", "--root", str(run)]) == 0
    assert garbled["id"] in capsys.readouterr().out


def test_next_reprints_a_tick_whose_task_file_went_schema_invalid(
    run, mem, mktask, capsys
):
    """Parses, but has lost a required field. `task["kind"]` and
    `task["attempts"]` are both indexed below the guard."""
    task = mktask(question="q", kind="search")
    mem.update(task["id"], status="running")
    journal.append(run, "dispatched", tick=1, task_ids=[task["id"]],
                   agents={}, models={})
    cfg = runconfig.load(run)
    cfg["status"]["tick"] = 1
    runconfig.save(run, cfg)
    path = mem.path_for(task["id"])
    path.write_text(
        "".join(line for line in path.read_text(encoding="utf-8").splitlines(True)
                if not line.startswith("kind:")),
        encoding="utf-8")

    assert research.main(["next", "--root", str(run)]) == 0
    assert task["id"] in capsys.readouterr().out


def test_next_survives_a_dispatch_record_with_no_task_ids(run, mem, mktask,
                                                          capsys):
    """report._resume_run already guards this exact record for this exact
    reason — journal.read() cannot vouch for a record's shape. `next`
    needs it more: it is the only way forward from an in-flight tick, so
    a raise here is terminal."""
    task = mktask(question="q", kind="search")
    mem.update(task["id"], status="running")
    journal.append(run, "dispatched", tick=1, agents={}, models={})
    cfg = runconfig.load(run)
    cfg["status"]["tick"] = 1
    runconfig.save(run, cfg)

    assert research.main(["next", "--root", str(run)]) == 0
    out = capsys.readouterr().out
    assert "already dispatched" in out
    assert "research submit --tick 1" in out


# --- recheck: the rechecker's input packet -----------------------------

FROZEN_RECHECK = {"url": "https://a-example.com/p",
                  "quotes": ["first verbatim span", "second verbatim span"],
                  "citations": ["C-001", "C-002"]}


def test_recheck_packet_carries_the_url_and_the_quotes(mem, mktask, cfg):
    task = mktask(question="re-read the page", kind="recheck")
    mem.update(task["id"], inputs=FROZEN_RECHECK)
    task = mem.read(task["id"])
    payload = scheduler.agent_input(mem, Graph(mem), cfg, task["id"], task)
    assert payload["url"] == FROZEN_RECHECK["url"]
    assert payload["quotes"] == FROZEN_RECHECK["quotes"]


def test_recheck_packet_does_not_leak_the_citation_ids(mem, mktask, cfg):
    """The rechecker has no use for them, and a checker that knows which
    record its answer will update is a checker with a stake in the answer."""
    task = mktask(question="re-read the page", kind="recheck")
    mem.update(task["id"], inputs=FROZEN_RECHECK)
    task = mem.read(task["id"])
    payload = scheduler.agent_input(mem, Graph(mem), cfg, task["id"], task)
    assert "citations" not in payload


def test_a_recheck_task_with_no_url_is_rejected(mem, mktask, cfg):
    task = mktask(question="re-read the page", kind="recheck")
    with pytest.raises(ValueError, match="no url in its inputs"):
        scheduler.agent_input(mem, Graph(mem), cfg, task["id"], task)


def test_a_recheck_task_with_no_quotes_is_rejected(mem, mktask, cfg):
    task = mktask(question="re-read the page", kind="recheck")
    mem.update(task["id"], inputs={"url": "https://a-example.com/p",
                                   "quotes": [], "citations": []})
    task = mem.read(task["id"])
    with pytest.raises(ValueError, match="no quotes"):
        scheduler.agent_input(mem, Graph(mem), cfg, task["id"], task)
