import json
from pathlib import Path

import jsonschema
import pytest

import apply as apply_mod
import nodes
from graph import Graph

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


def load(node_type):
    return json.loads((SCHEMA_DIR / f"{node_type}.json").read_text())


VALID = {
    "task": {
        "id": "T-001", "type": "task",
        "created_at": "2026-08-20T10:00:00Z", "updated_at": "2026-08-20T10:00:00Z",
        "status": "pending", "provenance": {"task": None, "agent": "decomposer"},
        "question": "q", "depends_on": [], "parent": None, "depth": 0,
        "kind": "search", "attempts": 0,
    },
    "fact": {
        "id": "F-001", "type": "fact",
        "created_at": "2026-08-20T10:00:00Z", "updated_at": "2026-08-20T10:00:00Z",
        "status": "active", "provenance": {"task": "T-001", "agent": "extractor"},
        "statement": "s", "citations": ["C-001"],
    },
    "assumption": {
        "id": "A-001", "type": "assumption",
        "created_at": "2026-08-20T10:00:00Z", "updated_at": "2026-08-20T10:00:00Z",
        "status": "open", "provenance": {"task": "T-001", "agent": "decomposer"},
        "statement": "s", "raised_by": "T-001", "blocks": [], "refuted_by": None,
    },
    "hypothesis": {
        "id": "H-001", "type": "hypothesis",
        "created_at": "2026-08-20T10:00:00Z", "updated_at": "2026-08-20T10:00:00Z",
        "status": "proposed", "provenance": {"task": "T-001", "agent": "hypothesizer"},
        "claim": "c", "supporting": [], "counter": [], "confidence": 0.0,
        "verdict": None,
    },
    "citation": {
        "id": "C-001", "type": "citation",
        "created_at": "2026-08-20T10:00:00Z", "updated_at": "2026-08-20T10:00:00Z",
        "status": "pending", "provenance": {"task": "T-001", "agent": "extractor"},
        "url": "https://example.com/a", "domain": "example.com", "title": "A",
        "quote": "a quoted span", "quote_sha256": "0" * 64, "fetched_at": None,
        "http_status": None,
    },
}


@pytest.mark.parametrize("node_type", nodes.NODE_TYPES)
def test_every_node_type_has_a_schema(node_type):
    schema = load(node_type)
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("node_type", nodes.NODE_TYPES)
def test_valid_sample_passes(node_type):
    jsonschema.validate(VALID[node_type], load(node_type))


@pytest.mark.parametrize("node_type", nodes.NODE_TYPES)
def test_unknown_property_is_rejected(node_type):
    bad = {**VALID[node_type], "surprise": 1}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, load(node_type))


@pytest.mark.parametrize("node_type", nodes.NODE_TYPES)
def test_body_field_is_required(node_type):
    bad = dict(VALID[node_type])
    del bad[nodes.BODY_FIELD[node_type]]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, load(node_type))


def test_task_rejects_an_unknown_status():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**VALID["task"], "status": "vibing"}, load("task"))


def test_task_rejects_a_malformed_dependency_id():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**VALID["task"], "depends_on": ["F-001"]}, load("task"))


def test_hypothesis_rejects_confidence_above_one():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**VALID["hypothesis"], "confidence": 1.5},
                            load("hypothesis"))


def test_citation_rejects_a_short_hash():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**VALID["citation"], "quote_sha256": "abc"},
                            load("citation"))


@pytest.mark.parametrize("node_type", nodes.NODE_TYPES)
def test_malformed_created_at_is_rejected(node_type):
    bad = {**VALID[node_type], "created_at": "not-a-real-timestamp"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, load(node_type))


def test_citation_accepts_a_null_fetched_at():
    jsonschema.validate({**VALID["citation"], "fetched_at": None}, load("citation"))


def test_citation_rejects_a_malformed_fetched_at():
    bad = {**VALID["citation"], "fetched_at": "not-a-real-timestamp"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, load("citation"))


# --- a citation node cannot store a quote too short to be evidence ----

@pytest.mark.parametrize("quote", ["a", "42", "the", "at p99", " " * 20,
                                   "​" * 20, "a" + "​" * 20])
def test_a_citation_with_too_little_content_in_its_quote_is_invalid(quote):
    """The node-level half of the same bar gate 1 and gate 2 enforce.
    memory.py validates on every create AND every update, so this is what
    makes a degenerate citation unstorable rather than merely
    discouraged."""
    node = {**VALID["citation"], "quote": quote}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(node, load("citation"))


def test_a_citation_with_a_genuinely_terse_quote_is_valid():
    node = {**VALID["citation"], "quote": "42ms at p99"}
    jsonschema.validate(node, load("citation"))


# --- http_status: not every server answers with a real HTTP status ----

def test_citation_accepts_a_non_standard_http_status(mem):
    """LinkedIn answers 999. The 599 cap made that a permanent tick wedge:
    memory.create raised out of submit and every retry died identically."""
    citation = mem.create("citation", {
        "url": "https://a-example.com/p", "domain": "a-example.com",
        "title": "t", "quote": "a quoted span", "quote_sha256": "0" * 64,
        "fetched_at": "2026-08-22T10:00:00Z", "http_status": 999,
        "status": "unverifiable",
        "provenance": {"task": None, "agent": "extractor"},
    })
    assert mem.read(citation["id"])["http_status"] == 999


# --- page_sha256: a store written before gate 2 became a subagent ------

def test_a_citation_carrying_page_sha256_survives_the_gate_2_relocation(
        mem, mkfact):
    """The migration guarantee. `additionalProperties` is false, and the
    pre-branch apply_extract wrote `page_sha256` on EVERY citation it
    created (git show a82fff9:scripts/apply.py). Dropping the field from
    the schema did not merely stop it being written -- it invalidated
    every citation already on disk, measured as:

        validates:          NO -> Additional properties are not allowed
        live_citations():   set()   <- a VERIFIED citation vanishes
        _citation_is_gone:  True    <- and its facts get quarantined

    which on an operator's multi-day run means one `submit` across the
    upgrade demotes every hypothesis to `proposed` and quarantines the
    facts, irreversibly: promotion only happens when a fresh `verify`
    verdict lands. Asserted here over a real store rather than over the
    schema alone, because it is the three consequences -- not the
    jsonschema call -- that the operator actually loses.
    """
    citation = mem.create("citation", {
        "url": "https://example.com/a", "domain": "example.com", "title": "t",
        "quote": "a quoted span", "quote_sha256": "0" * 64,
        "fetched_at": "2026-08-20T10:00:00Z", "http_status": 200,
        "status": "verified", "page_sha256": "b" * 64,
        "provenance": {"task": None, "agent": "extractor"},
    })
    mkfact(citations=[citation["id"]])
    graph = Graph(mem, max_depth=4, promotion_threshold=0.6,
                  required_domains=3)

    stored = mem.read(citation["id"])
    assert stored["page_sha256"] == "b" * 64
    mem.validate(stored)
    assert graph.live_citations() == {citation["id"]}
    assert apply_mod._citation_is_gone(mem, citation["id"]) is False


def test_page_sha256_is_optional_because_nothing_writes_it_any_more():
    """The other half: a citation the CURRENT writer produced has no
    `page_sha256` at all, so restoring the field must not put it back in
    `required`. Both shapes have to validate against one schema for the
    upgrade to be survivable in either direction."""
    schema = load("citation")
    assert "page_sha256" not in schema["required"]
    jsonschema.validate(VALID["citation"], schema)
    jsonschema.validate({**VALID["citation"], "page_sha256": "b" * 64}, schema)


def test_http_status_still_permits_the_null_it_is_now_always_written_as():
    """The mirror hazard. `http_status` IS in `required`, so it could not
    be dropped -- but nothing observes a status code any more, so every
    citation this codebase writes carries null. If the type ever narrowed
    to plain integer, apply_extract's own writer would raise out of
    memory.create on every single citation."""
    schema = load("citation")
    assert "http_status" in schema["required"]
    jsonschema.validate({**VALID["citation"], "http_status": None}, schema)


# --- artifact.outline and artifact.synthesize --------------------------

def _outline_artifact(**overrides):
    artifact = {
        "task_id": "T-050",
        "sections": [
            {"id": "S-001", "title": "Optical scattering",
             "hypotheses": ["H-001"], "facts": ["F-001", "F-002"]},
        ],
    }
    artifact.update(overrides)
    return artifact


def _synthesize_artifact(**overrides):
    artifact = {
        "task_id": "T-051",
        "section": "S-001",
        "body": ("Short-wavelength light scatters more strongly in the "
                 "atmosphere \\cite{C-001}, which is why the daytime sky "
                 "appears blue to a ground observer \\factref{F-001}."),
    }
    artifact.update(overrides)
    return artifact


def _validate(name, artifact):
    # Not gates.artifact_schema: that raises KeyError for any kind outside
    # gates.ARTIFACT_KINDS, and outline/synthesize deliberately do not join
    # ARTIFACT_KINDS until their appliers land (tasks 7 and 10) — see the
    # module docstring of gates.py. This loads the schema file directly,
    # the same way `load()` above does for every other node schema, so gate
    # 1's contract is tested without pre-empting that ordering.
    schema = load(f"artifact.{name}")
    jsonschema.validate(artifact, schema)


def test_a_well_formed_outline_artifact_validates():
    _validate("outline", _outline_artifact())


def test_an_outline_artifact_needs_at_least_one_section():
    with pytest.raises(jsonschema.ValidationError):
        _validate("outline", _outline_artifact(sections=[]))


def test_an_outline_section_title_may_not_be_blank():
    with pytest.raises(jsonschema.ValidationError):
        _validate("outline", _outline_artifact(sections=[
            {"id": "S-001", "title": "  ", "hypotheses": [], "facts": []}]))


def test_an_outline_section_id_must_look_like_a_section_id():
    with pytest.raises(jsonschema.ValidationError):
        _validate("outline", _outline_artifact(sections=[
            {"id": "T-001", "title": "t", "hypotheses": [], "facts": []}]))


def test_an_outline_artifact_rejects_an_unknown_field():
    """additionalProperties: false throughout. A model that invents a
    `rationale` key is a model that has drifted from its contract."""
    with pytest.raises(jsonschema.ValidationError):
        _validate("outline", _outline_artifact(rationale="because"))


def test_a_well_formed_synthesize_artifact_validates():
    _validate("synthesize", _synthesize_artifact())


def test_a_synthesize_body_may_not_be_trivially_short():
    with pytest.raises(jsonschema.ValidationError):
        _validate("synthesize", _synthesize_artifact(body="Too short."))


def test_a_synthesize_body_may_not_be_whitespace():
    with pytest.raises(jsonschema.ValidationError):
        _validate("synthesize", _synthesize_artifact(body=" " * 200))


def test_a_synthesize_artifact_must_name_its_section():
    with pytest.raises(jsonschema.ValidationError):
        _validate("synthesize", _synthesize_artifact(section="S-1"))


def test_task_kind_accepts_outline(mem, mktask):
    task = mktask(question="produce the outline", kind="outline")
    assert task["kind"] == "outline"


# --- artifact.recheck ---------------------------------------------------

def _recheck_artifact(**overrides):
    artifact = {
        "task_id": "T-042",
        "url": "https://a-example.com/p",
        "outcome": "read",
        "quotes": [{"index": 0, "present": True},
                   {"index": 1, "present": False}],
        "note": "",
    }
    artifact.update(overrides)
    return artifact


def _validate_recheck(artifact):
    jsonschema.validate(artifact, load("artifact.recheck"))
    # JSON Schema's uniqueItems compares whole array items, so two verdicts
    # that share an index but disagree on `present` are NOT caught by
    # `artifact.recheck.json`'s own `uniqueItems: true` — the objects
    # differ in `present`, so they are not "equal" items. Checking a
    # subset of an object's keys for uniqueness across an array with an
    # unbounded domain of values has no expression in standard JSON
    # Schema (no $data references, no custom keywords here), so it is
    # checked here instead, the same way gates.schema_check layers a
    # task_id check on top of jsonschema.validate. Whoever wires `recheck`
    # into gates.ARTIFACT_KINDS (Task 7) will need the equivalent check in
    # gates.py, because production artifacts reach gate 1 through
    # gates.schema_check, not through this test helper.
    indices = [quote["index"] for quote in artifact.get("quotes", [])]
    if len(indices) != len(set(indices)):
        raise jsonschema.ValidationError(
            "quotes: the same index is judged more than once")


def test_a_well_formed_recheck_artifact_validates():
    _validate_recheck(_recheck_artifact())


def test_a_blocked_page_needs_no_per_quote_verdicts():
    """A login wall means nothing can be judged. Demanding verdicts anyway
    would push the model into guessing."""
    _validate_recheck(_recheck_artifact(outcome="blocked", quotes=[]))


def test_an_unknown_outcome_is_rejected():
    with pytest.raises(jsonschema.ValidationError):
        _validate_recheck(_recheck_artifact(outcome="probably fine"))


def test_a_quote_verdict_must_carry_an_index_and_a_boolean():
    with pytest.raises(jsonschema.ValidationError):
        _validate_recheck(_recheck_artifact(quotes=[{"index": 0}]))


def test_the_same_quote_index_may_not_be_judged_twice():
    with pytest.raises(jsonschema.ValidationError):
        _validate_recheck(_recheck_artifact(quotes=[
            {"index": 0, "present": True}, {"index": 0, "present": False}]))


def test_a_recheck_artifact_may_not_echo_the_quote_text():
    """additionalProperties: false. If a rechecker could return the span,
    a mangled echo would be indistinguishable from a genuine absence."""
    with pytest.raises(jsonschema.ValidationError):
        _validate_recheck(_recheck_artifact(quotes=[
            {"index": 0, "present": True, "quote": "some text"}]))


def test_task_kind_accepts_recheck(mem, mktask):
    assert mktask(question="re-read the page", kind="recheck")["kind"] == "recheck"


# --- descriptions must name a mechanism that still exists -------------

def test_the_quote_description_names_the_bar_that_is_actually_enforced():
    """`quote`'s description credited gate 2 with enforcing
    MIN_QUOTE_CHARS "in code". Nothing in code is gate 2 any more -- it is
    the rechecker subagent's judgement, outside this process -- and the
    check it described is really apply_extract's, which drops a too-short
    quote before it can be written as a citation. A schema description is
    read by whoever is about to change the field; pointing them at a
    mechanism that no longer exists is how the check gets deleted."""
    description = load("citation")["properties"]["quote"]["description"]
    assert "gate 2 enforces in code" not in description
    assert "apply_extract" in description
    # Gate 1 still does enforce it, in schemas/artifact.extract.json.
    assert "artifact.extract.json" in description
