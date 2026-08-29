"""Gate 1. Cheapest gate, runs first, and the only thing standing between
a weaker model's output and the graph.

Every rejection test here is an adversarial case: a shape a plausible
model actually emits."""
import json

import jsonschema
import pytest

import gates

VALID = {
    "decompose": {
        "task_id": "T-001",
        "children": [
            {"question": "What is the p99?", "kind": "search",
             "rationale": "the parent asks about latency",
             "depends_on_index": []},
            {"question": "What is the p50?", "kind": "search",
             "rationale": "needed to interpret the p99",
             "depends_on_index": [0]},
        ],
        "assumptions": [
            {"statement": "v3 is the current release",
             "blocks_index": [0, 1]},
        ],
    },
    "search": {
        "task_id": "T-002",
        "sources": [
            {"url": "https://example.com/a", "title": "A",
             "relevance": 0.9, "why": "measures p99 directly"},
        ],
        "queries": ["a search query"],
        "no_sources_reason": None,
    },
    "extract": {
        "task_id": "T-003",
        "url": "https://example.com/a",
        "facts": [
            {"statement": "The service reports 42ms at p99.",
             "quote": "The service reports 42ms at p99."},
        ],
        "published_at": None,
        "source_type": "primary",
        "no_facts_reason": None,
    },
    "recheck": {
        "task_id": "T-001",
        "url": "https://a-example.com/scattering",
        "outcome": "read",
        "quotes": [{"index": 0, "present": True},
                   {"index": 1, "present": False}],
        "note": "The second span differs from the page by one word.",
    },
    "hypothesize": {
        "task_id": "T-004",
        "hypotheses": [
            {"claim": "Latency is dominated by cold starts.",
             "supporting": ["C-001", "C-002"], "counter": [],
             "refutes": None},
        ],
        "no_hypotheses_reason": None,
    },
    "verify": {
        "task_id": "T-005",
        "hypothesis": "H-001",
        "verdict": "supported",
        "failing_citations": [],
        "reasoning": "Both quotes state the claim directly.",
    },
    "outline": {
        "task_id": "T-006",
        "sections": [
            {"id": "S-001", "title": "Optical scattering",
             "hypotheses": ["H-001"], "facts": ["F-001", "F-002"]},
        ],
    },
    "synthesize": {
        "task_id": "T-007",
        "section": "S-001",
        "body": ("Short wavelengths scatter more strongly in the upper "
                 "atmosphere than long wavelengths do \\cite{C-001}, which "
                 "is why the daytime sky reads as blue rather than white "
                 "\\factref{F-001}."),
    },
}


def load(kind):
    return gates.artifact_schema(kind)


# --- every schema, structurally ---------------------------------------

@pytest.mark.parametrize("kind", gates.ARTIFACT_KINDS)
def test_the_schema_is_valid_and_closed(kind):
    schema = load(kind)
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("kind", gates.ARTIFACT_KINDS)
def test_the_valid_sample_passes(kind):
    assert gates.schema_check(kind, VALID[kind], VALID[kind]["task_id"]) is None


@pytest.mark.parametrize("kind", gates.ARTIFACT_KINDS)
def test_an_unknown_property_is_rejected(kind):
    """Closed schemas are what stop a model padding its output with
    fields nothing reads."""
    artifact = {**VALID[kind], "confidence": 0.9}
    error = gates.schema_check(kind, artifact, VALID[kind]["task_id"])
    assert error and "confidence" in error


@pytest.mark.parametrize("kind", gates.ARTIFACT_KINDS)
def test_a_missing_task_id_is_rejected(kind):
    artifact = {k: v for k, v in VALID[kind].items() if k != "task_id"}
    assert gates.schema_check(kind, artifact, "T-001")


@pytest.mark.parametrize("kind", gates.ARTIFACT_KINDS)
def test_a_task_id_naming_a_different_task_is_rejected(kind):
    """A subagent writing into another task's inbox file would apply its
    output to the wrong node's provenance."""
    error = gates.schema_check(kind, VALID[kind], "T-999")
    assert error and "T-999" in error


@pytest.mark.parametrize("kind", gates.ARTIFACT_KINDS)
def test_a_non_object_artifact_is_rejected(kind):
    assert gates.schema_check(kind, ["not", "an", "object"], "T-001")


# --- decompose --------------------------------------------------------

def test_a_decomposer_may_not_create_a_verify_task():
    """verify tasks come from a hypothesis, extract from a search result.
    A decomposer inventing one creates a task nothing can feed."""
    artifact = json.loads(json.dumps(VALID["decompose"]))
    artifact["children"][0]["kind"] = "verify"
    assert gates.schema_check("decompose", artifact, "T-001")


def test_a_decomposer_may_create_a_nested_decompose_task():
    artifact = json.loads(json.dumps(VALID["decompose"]))
    artifact["children"][0]["kind"] = "decompose"
    assert gates.schema_check("decompose", artifact, "T-001") is None


def test_a_child_without_a_rationale_is_rejected():
    """Spec's open risks: breadth is bounded only by the depth cap and
    the decomposer's obligation to justify each child against its
    parent. An unjustified child is that bound removed."""
    artifact = json.loads(json.dumps(VALID["decompose"]))
    del artifact["children"][0]["rationale"]
    assert gates.schema_check("decompose", artifact, "T-001")


def test_an_empty_rationale_is_rejected():
    artifact = json.loads(json.dumps(VALID["decompose"]))
    artifact["children"][0]["rationale"] = ""
    assert gates.schema_check("decompose", artifact, "T-001")


def test_a_sibling_dependency_must_be_an_index_not_an_id():
    """The decomposer has no graph access, so an id in this field is
    necessarily invented."""
    artifact = json.loads(json.dumps(VALID["decompose"]))
    artifact["children"][1]["depends_on_index"] = ["T-002"]
    assert gates.schema_check("decompose", artifact, "T-001")


def test_a_negative_sibling_index_is_rejected():
    artifact = json.loads(json.dumps(VALID["decompose"]))
    artifact["children"][1]["depends_on_index"] = [-1]
    assert gates.schema_check("decompose", artifact, "T-001")


def test_a_decompose_artifact_with_no_children_is_accepted():
    """A leaf question is a legitimate answer to 'decompose this'."""
    assert gates.schema_check(
        "decompose", {"task_id": "T-001", "children": [], "assumptions": []},
        "T-001") is None


# --- search -----------------------------------------------------------

def test_an_empty_source_list_with_no_reason_is_rejected():
    """Found-nothing and gave-up must be distinguishable, or the loop
    cannot tell which branch is exhausted."""
    assert gates.schema_check(
        "search",
        {"task_id": "T-002", "sources": [], "no_sources_reason": None},
        "T-002")


def test_an_empty_source_list_with_a_reason_is_accepted():
    assert gates.schema_check(
        "search",
        {"task_id": "T-002", "sources": [],
         "queries": ["a search query"],
         "no_sources_reason": "every result was a vendor blog"},
        "T-002") is None


def test_a_non_http_source_url_is_rejected():
    """Spec's non-goals: public web only. A file:// or internal URL is
    out of scope by construction."""
    artifact = json.loads(json.dumps(VALID["search"]))
    artifact["sources"][0]["url"] = "file:///etc/passwd"
    assert gates.schema_check("search", artifact, "T-002")


def test_a_relevance_above_one_is_rejected():
    artifact = json.loads(json.dumps(VALID["search"]))
    artifact["sources"][0]["relevance"] = 1.5
    assert gates.schema_check("search", artifact, "T-002")


def test_a_source_without_a_why_is_rejected():
    artifact = json.loads(json.dumps(VALID["search"]))
    del artifact["sources"][0]["why"]
    assert gates.schema_check("search", artifact, "T-002")


# --- extract ----------------------------------------------------------

def test_an_extract_artifact_must_name_the_url_it_read():
    artifact = json.loads(json.dumps(VALID["extract"]))
    del artifact["url"]
    assert gates.schema_check("extract", artifact, "T-003")


def test_a_fact_with_an_empty_quote_is_rejected():
    """Gate 2 has nothing to check against an empty quote, and an empty
    string is a substring of every page."""
    artifact = json.loads(json.dumps(VALID["extract"]))
    artifact["facts"][0]["quote"] = ""
    assert gates.schema_check("extract", artifact, "T-003")


def test_a_fact_with_a_whitespace_only_quote_is_rejected():
    """A single space satisfies minLength 1 but normalizes to empty,
    making it a substring of every page — the critical false-accept case.
    The pattern requires at least one non-whitespace character."""
    artifact = json.loads(json.dumps(VALID["extract"]))
    artifact["facts"][0]["quote"] = " "
    assert gates.schema_check("extract", artifact, "T-003")


def test_a_fact_with_a_tab_and_newline_quote_is_rejected():
    """All whitespace normalizes to empty, creating the false-accept."""
    artifact = json.loads(json.dumps(VALID["extract"]))
    artifact["facts"][0]["quote"] = "\t\n"
    assert gates.schema_check("extract", artifact, "T-003")


def test_a_fact_without_a_quote_is_rejected():
    artifact = json.loads(json.dumps(VALID["extract"]))
    del artifact["facts"][0]["quote"]
    assert gates.schema_check("extract", artifact, "T-003")


def test_an_empty_fact_list_with_no_reason_is_rejected():
    assert gates.schema_check(
        "extract",
        {"task_id": "T-003", "url": "https://example.com/a", "facts": [],
         "published_at": None,
         "source_type": "primary",
         "no_facts_reason": None},
        "T-003")


def test_an_empty_fact_list_with_a_reason_is_accepted():
    assert gates.schema_check(
        "extract",
        {"task_id": "T-003", "url": "https://example.com/a", "facts": [],
         "published_at": None,
         "source_type": "primary",
         "no_facts_reason": "the page is a login wall"},
        "T-003") is None


# --- hypothesize ------------------------------------------------------

def test_a_hypothesis_with_no_supporting_citations_is_rejected():
    """A claim with no evidence is not a hypothesis, and it would score
    0.0 and be demoted on the next recompute anyway."""
    artifact = json.loads(json.dumps(VALID["hypothesize"]))
    artifact["hypotheses"][0]["supporting"] = []
    assert gates.schema_check("hypothesize", artifact, "T-004")


def test_a_malformed_citation_id_is_rejected():
    artifact = json.loads(json.dumps(VALID["hypothesize"]))
    artifact["hypotheses"][0]["supporting"] = ["C-001", "not-an-id"]
    assert gates.schema_check("hypothesize", artifact, "T-004")


def test_refutes_must_name_an_assumption():
    artifact = json.loads(json.dumps(VALID["hypothesize"]))
    artifact["hypotheses"][0]["refutes"] = "H-002"
    assert gates.schema_check("hypothesize", artifact, "T-004")


def test_refutes_may_name_an_assumption():
    artifact = json.loads(json.dumps(VALID["hypothesize"]))
    artifact["hypotheses"][0]["refutes"] = "A-003"
    assert gates.schema_check("hypothesize", artifact, "T-004") is None


# --- verify -----------------------------------------------------------

def test_an_unknown_verdict_is_rejected():
    """Spec section 6 fixes the three verdicts and what each does to the
    graph. A fourth would have no transition."""
    artifact = {**VALID["verify"], "verdict": "probably"}
    assert gates.schema_check("verify", artifact, "T-005")


@pytest.mark.parametrize("verdict",
                         ["supported", "unsupported", "contradicted"])
def test_every_spec_verdict_is_accepted(verdict):
    artifact = {**VALID["verify"], "verdict": verdict}
    assert gates.schema_check("verify", artifact, "T-005") is None


def test_the_verified_hypothesis_must_be_a_hypothesis_id():
    artifact = {**VALID["verify"], "hypothesis": "F-001"}
    assert gates.schema_check("verify", artifact, "T-005")


def test_a_verdict_without_reasoning_is_rejected():
    """The reasoning goes into the journal. 'Why is H-012 refuted' has
    to be answerable three days later."""
    artifact = {k: v for k, v in VALID["verify"].items() if k != "reasoning"}
    assert gates.schema_check("verify", artifact, "T-005")


def test_failing_citations_must_be_citation_ids():
    artifact = {**VALID["verify"], "failing_citations": ["T-001"]}
    assert gates.schema_check("verify", artifact, "T-005")


# --- the gate itself --------------------------------------------------

def test_schema_is_protected_from_mutation():
    """artifact_schema returns a deep copy to prevent mutations of the
    cached schema from affecting future validations. If a caller does
    schema['additionalProperties'] = True, the next caller must see the
    original False."""
    schema1 = gates.artifact_schema("search")
    schema1["additionalProperties"] = True
    schema1["properties"]["sources"]["items"]["minLength"] = 999
    schema2 = gates.artifact_schema("search")
    assert schema2["additionalProperties"] is False
    assert "minLength" not in schema2["properties"]["sources"]["items"]


def test_a_bare_https_url_is_rejected():
    """https:// with nothing after the scheme must be rejected."""
    artifact = json.loads(json.dumps(VALID["search"]))
    artifact["sources"][0]["url"] = "https://"
    assert gates.schema_check("search", artifact, "T-002")


def test_an_https_url_with_embedded_space_is_rejected():
    """https:// example.com (space before domain) must be rejected."""
    artifact = json.loads(json.dumps(VALID["search"]))
    artifact["sources"][0]["url"] = "https:// example.com"
    assert gates.schema_check("search", artifact, "T-002")


def test_an_extract_artifact_rejects_bare_https():
    """Bare https:// in the URL field must be rejected."""
    artifact = json.loads(json.dumps(VALID["extract"]))
    artifact["url"] = "https://"
    assert gates.schema_check("extract", artifact, "T-003")


def test_an_extract_artifact_rejects_https_with_space():
    """https:// with embedded space must be rejected."""
    artifact = json.loads(json.dumps(VALID["extract"]))
    artifact["url"] = "https:// example.com"
    assert gates.schema_check("extract", artifact, "T-003")


def test_the_error_names_the_offending_path():
    """The message goes back into the retry prompt verbatim, so it has
    to say where the problem is, not just that there is one."""
    artifact = json.loads(json.dumps(VALID["search"]))
    artifact["sources"][0]["relevance"] = 5
    error = gates.schema_check("search", artifact, "T-002")
    assert "sources" in error and "relevance" in error


def test_an_unknown_kind_raises():
    """A task kind with no artifact schema is a scheduler bug, not a
    model failure, so it must not be reported as a rejected artifact."""
    with pytest.raises(KeyError):
        gates.schema_check("summarise", {}, "T-001")


def test_every_dispatchable_kind_has_an_artifact_schema():
    """runconfig.KIND_AGENT is what the scheduler dispatches, and
    everything it dispatches must be checkable on return. A kind with no
    artifact schema is a scheduler bug, not a model failure, and
    gates.artifact_schema raises rather than returning a rejection."""
    import runconfig
    assert set(runconfig.KIND_AGENT) == set(gates.ARTIFACT_KINDS)


# --- gate 1 refuses a degenerate quote too ----------------------------

@pytest.mark.parametrize("quote", ["a", "42", "the", "at p99", " " * 20,
                                   "​" * 20, "a" + "​" * 20])
def test_a_fact_whose_quote_carries_too_little_content_is_rejected(quote):
    """Gate 2 enforces this independently (it must not be foolable by
    whatever upstream permits), but gate 1 is free and runs first, so a
    degenerate quote never reaches an applier at all."""
    artifact = json.loads(json.dumps(VALID["extract"]))
    artifact["facts"][0]["quote"] = quote
    assert gates.schema_check("extract", artifact, "T-003")


def test_a_genuinely_terse_quote_passes_gate_one():
    artifact = json.loads(json.dumps(VALID["extract"]))
    artifact["facts"][0]["quote"] = "42ms at p99"
    assert gates.schema_check("extract", artifact, "T-003") is None
