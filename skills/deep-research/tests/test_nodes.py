import pytest

import nodes


def test_round_trip_preserves_every_field():
    data = {
        "id": "T-001",
        "type": "task",
        "created_at": "2026-08-20T10:00:00Z",
        "updated_at": "2026-08-20T10:00:00Z",
        "status": "pending",
        "provenance": {"task": None, "agent": "decomposer"},
        "question": "What is the p99 latency of X?",
        "depends_on": [],
        "parent": None,
        "depth": 0,
        "kind": "search",
        "attempts": 0,
    }
    assert nodes.loads(nodes.dumps(data)) == data


def test_body_field_lands_in_the_markdown_body_not_frontmatter():
    text = nodes.dumps({
        "id": "F-007", "type": "fact",
        "created_at": "2026-08-20T10:00:00Z",
        "updated_at": "2026-08-20T10:00:00Z",
        "status": "active",
        "provenance": {"task": "T-001", "agent": "extractor"},
        "statement": "The service reports 42ms at p99.",
        "citations": ["C-003"],
    })
    front, body = text.split("\n---\n", 1)
    assert "statement" not in front
    assert body.strip() == "The service reports 42ms at p99."


def test_body_may_itself_contain_a_horizontal_rule():
    data = {
        "id": "H-002", "type": "hypothesis",
        "created_at": "2026-08-20T10:00:00Z",
        "updated_at": "2026-08-20T10:00:00Z",
        "status": "proposed",
        "provenance": {"task": "T-004", "agent": "hypothesizer"},
        "claim": "line one\n---\nline two",
        "supporting": [], "counter": [], "confidence": 0.0, "verdict": None,
    }
    assert nodes.loads(nodes.dumps(data))["claim"] == "line one\n---\nline two"


def test_type_of_maps_id_prefix_to_node_type():
    assert nodes.type_of("A-012") == "assumption"
    assert nodes.type_of("C-100") == "citation"


def test_type_of_rejects_an_unknown_prefix():
    with pytest.raises(nodes.NodeFormatError):
        nodes.type_of("Z-001")


def test_loads_rejects_a_file_without_frontmatter():
    with pytest.raises(nodes.NodeFormatError):
        nodes.loads("just some text\n")


def test_loads_rejects_an_unknown_node_type():
    with pytest.raises(nodes.NodeFormatError):
        nodes.loads("---\nid: X-1\ntype: widget\n---\nbody\n")


def test_body_with_leading_and_trailing_spaces_round_trips():
    data = {
        "id": "C-001", "type": "citation",
        "created_at": "2026-08-20T10:00:00Z",
        "updated_at": "2026-08-20T10:00:00Z",
        "status": "archived",
        "provenance": {"task": "T-001", "agent": "extractor"},
        "quote": "  quoted text with spaces  ",
        "source": "example.com",
    }
    assert nodes.loads(nodes.dumps(data))["quote"] == "  quoted text with spaces  "


def test_body_with_trailing_blank_lines_round_trips():
    data = {
        "id": "C-002", "type": "citation",
        "created_at": "2026-08-20T10:00:00Z",
        "updated_at": "2026-08-20T10:00:00Z",
        "status": "archived",
        "provenance": {"task": "T-001", "agent": "extractor"},
        "quote": "Multi-line quote.\n\n",
        "source": "example.com",
    }
    assert nodes.loads(nodes.dumps(data))["quote"] == "Multi-line quote.\n\n"


def test_whitespace_only_body_round_trips():
    data = {
        "id": "C-003", "type": "citation",
        "created_at": "2026-08-20T10:00:00Z",
        "updated_at": "2026-08-20T10:00:00Z",
        "status": "archived",
        "provenance": {"task": "T-001", "agent": "extractor"},
        "quote": "   ",
        "source": "example.com",
    }
    assert nodes.loads(nodes.dumps(data))["quote"] == "   "


def test_empty_string_body_round_trips():
    data = {
        "id": "C-004", "type": "citation",
        "created_at": "2026-08-20T10:00:00Z",
        "updated_at": "2026-08-20T10:00:00Z",
        "status": "archived",
        "provenance": {"task": "T-001", "agent": "extractor"},
        "quote": "",
        "source": "example.com",
    }
    assert nodes.loads(nodes.dumps(data))["quote"] == ""


def test_loads_malformed_yaml_frontmatter_raises_node_format_error():
    with pytest.raises(nodes.NodeFormatError):
        nodes.loads("---\nid: [unclosed list\n---\nbody\n")


def test_loads_bare_scalar_frontmatter_raises_node_format_error():
    with pytest.raises(nodes.NodeFormatError):
        nodes.loads("---\njust a string\n---\nbody\n")


def test_loads_yaml_list_frontmatter_raises_node_format_error():
    with pytest.raises(nodes.NodeFormatError):
        nodes.loads("---\n- item1\n- item2\n---\nbody\n")
