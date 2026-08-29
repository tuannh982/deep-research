import pytest

import gates
import graph as graph_mod

SECTION = {"id": "S-001", "title": "t", "hypotheses": [], "facts": [],
           "allowed_cite_keys": ["C-001", "C-002"]}


@pytest.fixture
def live(mem, mkcitation, mkfact):
    """A graph where C-001/C-002 are citable and F-001 is active."""
    first = mkcitation(url="https://a-example.com/p", domain="a-example.com",
                       quote="a verified span here")["id"]
    second = mkcitation(url="https://b-example.com/p", domain="b-example.com",
                        quote="another verified span")["id"]
    fact = mkfact(statement="a live fact", citations=[first])["id"]
    return graph_mod.Graph(mem), {"c1": first, "c2": second, "f1": fact}


def test_a_clean_section_body_passes(live):
    graph, ids = live
    body = ("Short wavelengths scatter more strongly in the atmosphere "
            "\\cite{%s}." % ids["c1"])
    assert gates.report_section(body, SECTION, graph) is None


def test_an_invented_cite_key_is_rejected(live):
    graph, _ = live
    body = "Something was claimed here \\cite{C-999}."
    message = gates.report_section(body, SECTION, graph)
    assert message is not None and "C-999" in message


def test_a_cite_key_outside_the_allowed_set_is_rejected(live):
    """C-002 exists and is citable, but was not assigned to this section.
    Spec section 6 diffs against the allowed set 'passed into that
    synthesizer call', not against everything in the graph."""
    graph, ids = live
    section = {**SECTION, "allowed_cite_keys": [ids["c1"]]}
    body = "A claim resting elsewhere \\cite{%s}." % ids["c2"]
    message = gates.report_section(body, section, graph)
    assert message is not None and ids["c2"] in message


def test_a_citation_rejected_since_the_section_was_frozen_is_caught(mem, live):
    """The live check resolved question 3 promises. allowed_cite_keys was
    frozen when the section was seeded; gate 2 can have rejected the
    citation since, and a rejected quote is not on the page."""
    graph, ids = live
    mem.update(ids["c1"], status="rejected")
    body = "A claim on a dead source \\cite{%s}." % ids["c1"]
    message = gates.report_section(body, SECTION, graph_mod.Graph(mem))
    assert message is not None and ids["c1"] in message


def test_a_factref_that_does_not_resolve_is_rejected(live):
    graph, _ = live
    body = "The rate rose to 12 percent \\factref{F-999}."
    message = gates.report_section(body, SECTION, graph)
    assert message is not None and "F-999" in message


def test_a_factref_to_a_quarantined_fact_is_rejected(mem, live):
    """A cascade quarantines a fact when the assumption under it is refuted.
    Prose still resting on it is exactly what must not reach the PDF."""
    graph, ids = live
    mem.update(ids["f1"], status="quarantined")
    body = "It grew 12 percent \\factref{%s}." % ids["f1"]
    message = gates.report_section(body, SECTION, graph_mod.Graph(mem))
    assert message is not None and ids["f1"] in message


def test_an_unsourced_numeric_claim_is_rejected(live):
    graph, _ = live
    body = "The figure reached 40 percent by the end of the period."
    message = gates.report_section(body, SECTION, graph)
    assert message is not None and "40 percent" in message


def test_a_sourced_numeric_claim_passes(live):
    graph, ids = live
    body = "The figure reached 40 percent \\cite{%s}." % ids["c1"]
    assert gates.report_section(body, SECTION, graph) is None


def test_every_problem_is_reported_at_once(live):
    """One retry, every complaint. The section has three attempts total."""
    graph, _ = live
    body = ("An invented source \\cite{C-999}. A dangling fact "
            "\\factref{F-998}. A bare 40 percent claim.")
    message = gates.report_section(body, SECTION, graph)
    assert "C-999" in message and "F-998" in message and "40 percent" in message


def test_prose_with_no_markup_and_no_numbers_passes(live):
    graph, _ = live
    assert gates.report_section(
        "Margins held steady across the period under review.",
        SECTION, graph) is None
