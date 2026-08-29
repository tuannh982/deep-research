"""The prompts are the one part of this system written in prose, so they
are made checkable: each declares its schema and carries a worked example
that must validate against it."""
import json
import re
from pathlib import Path

import pytest
import yaml

import gates
import runconfig
import scheduler

AGENT_DIR = Path(__file__).resolve().parents[1] / "agents"

# Spec section 5's tool column, plus the `rechecker` this branch added.
# Five of the eight have no tools at all, and that is a security property
# as much as a context one — the count is asserted below rather than left
# to this comment, because it went stale the moment an eighth agent landed.
EXPECTED_TOOLS = {
    "decomposer": [],
    "searcher": ["WebSearch"],
    "extractor": ["WebFetch"],
    "hypothesizer": [],
    "verifier": [],
    "outliner": [],
    "synthesizer": [],
    # Gate 2. The only other agent with WebFetch, and it has it for the
    # opposite reason: the extractor reads a page to find things, this one
    # reads it to check something is already there.
    "rechecker": ["WebFetch"],
}

DISPATCHABLE = sorted(gates.ARTIFACT_KINDS)


def agent_for(kind):
    return runconfig.KIND_AGENT[kind]


def load(agent):
    text = (AGENT_DIR / f"{agent}.md").read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", text, re.DOTALL)
    assert match, f"{agent}.md has no frontmatter"
    return yaml.safe_load(match.group(1)), match.group(2)


def examples(body):
    return [json.loads(block) for block in
            re.findall(r"```json\n(.*?)\n```", body, re.DOTALL)]


@pytest.mark.parametrize("kind", DISPATCHABLE)
def test_every_dispatchable_kind_has_a_prompt_file(kind):
    assert (AGENT_DIR / f"{agent_for(kind)}.md").is_file()


@pytest.mark.parametrize("kind", DISPATCHABLE)
def test_the_frontmatter_declares_kind_schema_and_tools(kind):
    front, _ = load(agent_for(kind))
    assert front["kind"] == kind
    assert front["schema"] == f"schemas/artifact.{kind}.json"
    assert isinstance(front["tools"], list)


@pytest.mark.parametrize("kind", DISPATCHABLE)
def test_the_declared_schema_exists(kind):
    front, _ = load(agent_for(kind))
    assert (AGENT_DIR.parent / front["schema"]).is_file()


@pytest.mark.parametrize("kind", DISPATCHABLE)
def test_the_tools_match_the_spec_s_table(kind):
    """Spec section 5's tool column. A prompt quietly claiming a tool it
    should not have is a real escalation."""
    front, _ = load(agent_for(kind))
    assert front["tools"] == EXPECTED_TOOLS[agent_for(kind)]


def test_five_of_the_eight_agents_have_no_tools_at_all():
    """Spec section 5 said "five of seven"; this branch added the
    `rechecker`, which needs WebFetch to be gate 2 at all, so it is five
    of EIGHT. Counted here rather than recited in a comment: the ratio is
    a security property — an agent with an empty tool list cannot reach
    the network or the filesystem no matter what its prompt says — and
    the prose form of it went stale the moment an eighth agent landed."""
    # The prose ABOVE the table only. This test's own docstring quotes the
    # stale phrase to say what it used to be, and scanning the whole file
    # would match itself.
    prose = Path(__file__).read_text(encoding="utf-8").split("DISPATCHABLE =")[0]
    assert "of seven" not in prose, (
        "the tool-column count is stale: there are eight agents now")
    assert len(EXPECTED_TOOLS) == 8
    assert len([a for a, tools in EXPECTED_TOOLS.items() if not tools]) == 5
    assert sorted(a for a, tools in EXPECTED_TOOLS.items() if tools) == [
        "extractor", "rechecker", "searcher"]


@pytest.mark.parametrize("kind", DISPATCHABLE)
def test_the_worked_example_validates_against_its_schema(kind):
    """The load-bearing test in this file. A prompt whose own example
    would be rejected by gate 1 teaches the model to fail."""
    _, body = load(agent_for(kind))
    found = examples(body)
    assert found, f"{agent_for(kind)}.md has no ```json example"
    for example in found:
        error = gates.schema_check(kind, example, example["task_id"])
        assert error is None, f"{agent_for(kind)}.md example: {error}"


@pytest.mark.parametrize("kind", DISPATCHABLE)
def test_the_prompt_names_every_input_key_it_will_receive(kind):
    """An input field the scheduler sends and the prompt never mentions
    is a field the subagent will ignore."""
    _, body = load(agent_for(kind))
    for key in scheduler.REQUIRED_INPUT_KEYS[kind]:
        if key == "task_id":
            continue  # echoed, not reasoned about
        assert key in body, f"{agent_for(kind)}.md never mentions {key}"


@pytest.mark.parametrize("kind", DISPATCHABLE)
def test_no_prompt_mentions_reading_memory(kind):
    """Spec section 5: 'No subagent reads memory/.' The moment a prompt
    suggests it, the isolation that makes these safe on a small model is
    gone."""
    _, body = load(agent_for(kind))
    assert "research/memory" not in body
    assert "memory/" not in body


@pytest.mark.parametrize("kind", DISPATCHABLE)
def test_no_prompt_asks_the_model_for_a_confidence_number(kind):
    """Spec section 6: confidence is derived from evidence by
    confidence.py. 'No model ever sets a confidence value.'

    The synthesizer is exempt from the word itself, and only the word. Its
    job is to match prose to an ALREADY-COMPUTED score — a hypothesis at
    0.5 must read as unsettled and one at 0.9 as established — so it has to
    be told the number exists. It still cannot set one:
    artifact.synthesize.json has no confidence field, which is what
    test_no_artifact_schema_accepts_a_model_supplied_confidence pins.
    """
    if agent_for(kind) == "synthesizer":
        return
    _, body = load(agent_for(kind))
    assert "confidence" not in body.lower()


@pytest.mark.parametrize("kind", DISPATCHABLE)
def test_no_artifact_schema_accepts_a_model_supplied_confidence(kind):
    """The structural version of the rule above, and the one that actually
    binds: a model cannot set a score the schema will not carry."""
    schema = gates.artifact_schema(kind)
    assert "confidence" not in schema.get("properties", {})
    assert schema.get("additionalProperties") is False


@pytest.mark.parametrize("kind", DISPATCHABLE)
def test_the_prompt_tells_the_model_to_emit_json_only(kind):
    """The artifact is read with json.loads. Prose around it is a gate-1
    rejection and a wasted attempt."""
    _, body = load(agent_for(kind))
    assert "JSON" in body


def test_the_extractor_prompt_demands_a_verbatim_quote():
    """Gate 2 is a rechecker subagent comparing the quote against the live
    page. A paraphrase is indistinguishable from a fabrication, so the
    instruction has to be explicit."""
    _, body = load("extractor")
    assert "verbatim" in body.lower()
    assert "paraphrase" in body.lower()


def test_the_decomposer_prompt_demands_a_rationale_per_child():
    """Spec's open risks: breadth is bounded only by the depth cap and
    this obligation."""
    _, body = load("decomposer")
    assert "rationale" in body.lower()


def test_the_verifier_prompt_says_it_has_no_other_context():
    """What makes gate 4 adversarial rather than confirmatory.

    A bare 'only' check is nearly vacuous: verifier.md uses the word
    'only' several times for unrelated reasons ('Judge only what is in
    the quotes', 'You may only name ids from the input'), so that branch
    alone would still pass even if the isolation claim itself were
    deleted. Requiring both 'nothing else' and 'no other claims' pins it
    to the specific sentence that makes the isolation claim, not any
    stray use of 'only'.
    """
    _, body = load("verifier")
    # Collapse whitespace first: the prose wraps at ~78 columns, so the
    # phrase below can straddle a line break in the raw markdown.
    flat = " ".join(body.lower().split())
    assert "nothing else" in flat
    assert "no other claims" in flat


def test_the_verifier_prompt_explains_both_stances():
    """The packet labels every quote `supporting` or `counter`
    (scheduler.agent_input's verify branch). A prompt that names the field
    but not its two values leaves the model to guess what an unfamiliar
    label means on evidence that is meant to weigh against the claim —
    which is the failure the label was added to fix, one level up.
    """
    _, body = load("verifier")
    flat = " ".join(body.lower().split())
    assert "stance" in flat
    assert "counter" in flat and "supporting" in flat


def test_the_verifier_is_told_it_cannot_fail_a_counter_citation():
    """apply_verify DROPS a counter id named in `failing_citations` — it
    is not honoured and, deliberately, does not cost the task an attempt.
    Silently. So a verifier that keeps naming them never learns, and the
    dispute it was trying to dismiss stays live while it believes it
    dealt with it. Cheaper to say so in the prompt.

    Pinned to the counter-specific sentence, not to a bare
    "failing_citations" + "may only name ids from the input": that pair
    was already true of the prompt before any of this, so the loose form
    of this test passed while the rule it names went unsaid.
    """
    _, body = load("verifier")
    flat = " ".join(body.lower().split())
    assert "only name a `supporting` quote" in flat


def test_the_synthesizer_does_not_calibrate_against_unreachable_scores():
    """Rule 4 used to contrast "a hypothesis at 0.9" with "one at 0.5".
    confidence.compute's base term is n/(n+2), which reaches 0.9 only at
    18 live citations; 0.5 is below the 0.6 promotion floor, so no
    `supported` claim can ever be there. The writer was being told to
    hedge against a scale it would never see a value from, while every
    real promoted claim sat in 0.60-0.75 with no guidance at all.

    The score is no longer in its packet. Nothing here should read as if
    it were."""
    _, body = load("synthesizer")
    for unreachable in ("0.9", "0.5", "confidence"):
        assert unreachable not in body, unreachable


def test_the_synthesizer_is_told_how_to_treat_a_disputed_claim():
    """`disputed` is in the packet because `status` does not carry live
    opposition for a `proposed` claim. A field the prompt never mentions
    is a field the subagent will ignore, and this one is the difference
    between reporting a dispute and hiding one."""
    _, body = load("synthesizer")
    assert "disputed" in body


def test_the_searcher_prompt_explains_both_stances():
    """The packet carries `stance`, and a refute search reaches the same
    agent through the same kind. A prompt that names the field but not
    what `against` asks for leaves the model running its ordinary
    relevance search against a question phrased as a negation — which
    returns the sources that best SUPPORT the claim being challenged."""
    _, body = load("searcher")
    flat = " ".join(body.lower().split())
    assert "stance" in flat
    assert "against" in flat and "`for`" in flat


def test_the_searcher_prompt_says_to_avoid_seen_domains():
    """Gate 3 needs distinct registrable domains. A searcher that keeps
    returning the same site cannot supply them."""
    _, body = load("searcher")
    assert "seen_domains" in body


def test_the_synthesizer_is_told_not_to_emit_a_section_heading():
    """render emits \\section from the validated title. A heading in the
    body would produce two, and would let a synthesizer retitle its own
    section after the outline was validated."""
    text = (AGENT_DIR / "synthesizer.md").read_text(encoding="utf-8")
    assert "\\section" in text and "do not" in text.lower()


def test_the_outliner_is_told_it_may_not_drop_a_finding():
    text = (AGENT_DIR / "outliner.md").read_text(encoding="utf-8")
    assert "exactly once" in text


def test_the_outliner_is_told_it_may_not_empty_a_section():
    """outline.validate rejects an outline that leaves a section carrying
    nothing. The outliner is told so up front, so a legal-looking
    consolidation does not cost a retry to discover."""
    text = (AGENT_DIR / "outliner.md").read_text(encoding="utf-8")
    assert "emptying a section" in text


def test_no_prompt_file_is_enormous():
    """These go into a subagent's context on every dispatch, thousands of
    times over a run."""
    for kind in DISPATCHABLE:
        _, body = load(agent_for(kind))
        assert len(body.splitlines()) < 80, agent_for(kind)


def test_there_is_no_prompt_for_a_kind_nothing_dispatches():
    """A prompt with no agent name in runconfig.AGENTS is dead weight that
    will drift.

    This checks against the full agent registry rather than DISPATCHABLE
    (gates.ARTIFACT_KINDS) on purpose: outliner and synthesizer are named
    in runconfig.AGENTS and runconfig.KIND_AGENT already, because their
    packets are wired by Tasks 6 and 8, but their kinds only join
    ARTIFACT_KINDS in Tasks 7 and 10. Their prompt files are written ahead
    of that activation (Task 17), so checking against DISPATCHABLE here
    would fail on a prompt that is not dead weight at all — it is just
    early. Checking against the registry still catches an actually
    orphaned prompt: one with no agent name anywhere.
    """
    present = {path.stem for path in AGENT_DIR.glob("*.md")}
    assert present == set(runconfig.AGENTS)
