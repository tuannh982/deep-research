"""Gate 3 is three lines of counting. The tests that earn their keep are
the two cross-checks against confidence.compute: gate 3 and the
promotion threshold are independent implementations of one rule, and
nothing else would notice them drifting apart."""
import pytest

import confidence
import gates
import runconfig
from graph import Graph


DEFAULTS = {"min_citations": 3, "required_domains": 2}


# --- the counting -----------------------------------------------------

def test_three_citations_on_two_domains_passes():
    assert gates.independence(["a.com", "a.com", "b.com"], **DEFAULTS) is None


def test_two_citations_fail_on_count():
    reason = gates.independence(["a.com", "b.com"], **DEFAULTS)
    assert reason and "2" in reason and "3" in reason


def test_three_citations_on_one_domain_fail_on_spread():
    """Spec section 9's adversarial case: all citations sharing one
    eTLD+1."""
    reason = gates.independence(["a.com"] * 3, **DEFAULTS)
    assert reason and "domain" in reason


def test_no_citations_fail():
    assert gates.independence([], **DEFAULTS)


def test_the_count_is_checked_before_the_spread():
    """A one-citation claim needs more evidence before it needs more
    sources, and the reason goes into a task's prompt. This test pins the
    order: if the spread check ran first, the assertion would fail.

    The count branch message is unique: it contains "verified citation"
    and "needs" without containing "span" or "different site" (which
    distinguish the spread branch)."""
    reason = gates.independence(["a.com"], **DEFAULTS)
    assert "verified citation" in reason and "needs" in reason
    assert "span" not in reason and "different site" not in reason


def test_more_than_enough_passes():
    assert gates.independence(["a.com", "b.com", "c.com", "d.com"],
                              **DEFAULTS) is None


def test_the_thresholds_are_parameters_not_constants():
    """They come from run.yaml. A stricter run must actually be
    stricter."""
    assert gates.independence(["a.com", "a.com", "b.com"],
                              min_citations=3, required_domains=3)


# --- eTLD+1, the reason gate 3 exists --------------------------------

def test_subdomains_of_one_site_count_once():
    """Spec section 6: 'so blog.foo.com and foo.com count once'. This is
    the exact input plan 1's review measured scoring 0.6 — promotable, on
    what is really one source — before domains.py existed."""
    import domains
    reduced = [domains.registrable(h) for h in
               ["https://blog.foo.com/a", "https://foo.com/b",
                "https://www.foo.com/c"]]
    assert reduced == ["foo.com"] * 3
    assert gates.independence(reduced, **DEFAULTS)


def test_a_multi_part_public_suffix_is_honoured():
    import domains
    reduced = [domains.registrable(h) for h in
               ["https://www.bbc.co.uk/a", "https://news.bbc.co.uk/b",
                "https://example.com/c"]]
    assert set(reduced) == {"bbc.co.uk", "example.com"}
    assert gates.independence(reduced, **DEFAULTS) is None


# --- the cross-checks against confidence.compute ---------------------

def test_gate_three_s_minimum_is_exactly_the_promotion_threshold():
    """Hand-computed. The weakest evidence gate 3 admits at the defaults
    is 3 citations over 2 distinct domains: base = 3/(3+2) = 0.6,
    spread = min(1, 2/2) = 1.0, weight(supported) = 1.0, so 0.6 — which
    is promotion_threshold exactly.

    Gate 3 and the threshold are two implementations of one rule. If
    either moves, this fails."""
    cfg = runconfig.default("q")
    domains_at_the_bar = ["a.com", "a.com", "b.com"]
    assert gates.independence(
        domains_at_the_bar,
        min_citations=cfg["config"]["min_citations"],
        required_domains=cfg["config"]["required_domains"]) is None
    # Hand-computed: volume = min(1, 3/3) = 1.0, breadth = 2/(2+1)
    # = 0.667, weight 1.0 -> 0.67, which is promotion_threshold.
    assert confidence.compute(domains_at_the_bar, "supported") == 0.67
    assert (confidence.compute(domains_at_the_bar, "supported")
            >= cfg["config"]["promotion_threshold"])


def test_everything_gate_three_rejects_also_scores_below_the_threshold():
    """The other direction, and the invariant that caught a broken
    first attempt at this formula.

    Hand-computed: 2 citations on 2 domains give volume = 2/3, breadth =
    2/3, so 0.44; 3 citations on 1 domain give volume = 1.0, breadth =
    1/2, so 0.5. Both under 0.67.

    The volume term exists FOR this. apply._verified_status promotes on
    the score alone and never consults gate 3, so a formula reading only
    distinct domains let 2 citations on 2 domains score exactly the
    threshold and promote, silently bypassing min_citations."""
    threshold = runconfig.default("q")["config"]["promotion_threshold"]
    for rejected in (["a.com", "b.com"], ["a.com"] * 3, ["a.com"]):
        assert gates.independence(rejected, **DEFAULTS)
        assert confidence.compute(rejected, "supported") < threshold
    assert confidence.compute(["a.com", "b.com"], "supported") == 0.44
    assert confidence.compute(["a.com"] * 3, "supported") == 0.5


def test_an_unverified_claim_cannot_be_promoted_at_any_volume():
    """Gate 4 is a `verify` task rather than a call inside submit, so
    what stops an unverified hypothesis being promoted is arithmetic:
    VERDICT_WEIGHT[None] is 0.5 and volume * breadth <= 1.0, so the
    supremum is 0.5 — below the 0.67 threshold, forever.

    This margin is why `breadth` is d/(d+1) and not d/(d+2): the latter
    puts gate 3's minimum at 0.5 as well, colliding with this supremum,
    and an unverified claim with enough domains promotes. That was a
    real first attempt at this change and this test is what caught it.

    This is the load-bearing test for resolved design question 1."""
    threshold = runconfig.default("q")["config"]["promotion_threshold"]
    many = [f"d{i}.example" for i in range(200)]
    assert confidence.compute(many, None) <= 0.5
    assert confidence.compute(many, None) < threshold
    assert confidence.VERDICT_WEIGHT[None] == 0.5


@pytest.mark.parametrize("verdict,ceiling", [
    ("supported", 1.0), ("unsupported", 0.5), (None, 0.5),
    ("contradicted", 0.0),
])
def test_the_verdict_weight_bounds_the_score(verdict, ceiling):
    many = [f"d{i}.example" for i in range(200)]
    assert confidence.compute(many, verdict) <= ceiling


# --- the graph-level gap query ---------------------------------------

def test_supporting_domains_reduces_to_live_verified_citations(
    mem, mkcitation, mkfact, mkhypothesis
):
    verified = mkcitation(url="https://a.com/x", domain="a.com", quote="a quoted span one")
    unverified = mkcitation(url="https://b.com/x", domain="b.com", quote="a quoted span two",
                            status="unverifiable")
    mkfact(statement="e", citations=[verified["id"], unverified["id"]])
    hypothesis = mkhypothesis(supporting=[verified["id"], unverified["id"]])
    assert Graph(mem).supporting_domains(hypothesis["id"]) == ["a.com"]


def test_supporting_domains_drops_a_quarantined_fact_s_citation(
    mem, mkcitation, mkfact, mkhypothesis
):
    citation = mkcitation(url="https://a.com/x", domain="a.com")
    mkfact(statement="e", citations=[citation["id"]], status="quarantined")
    hypothesis = mkhypothesis(supporting=[citation["id"]])
    assert Graph(mem).supporting_domains(hypothesis["id"]) == []


def test_supporting_domains_of_an_unreadable_hypothesis_is_empty(
    mem, mkhypothesis
):
    """Total, like everything else that reads the graph."""
    hypothesis = mkhypothesis(supporting=["C-404"])
    mem.path_for(hypothesis["id"]).write_text("garbage\n", encoding="utf-8")
    assert Graph(mem).supporting_domains(hypothesis["id"]) == []


def test_evidence_gap_reports_a_hypothesis_short_on_domains(
    mem, mkcitation, mkfact, mkhypothesis
):
    citations = [mkcitation(url=f"https://a.com/{i}", domain="a.com",
                            quote=f"a quoted span {i}") for i in range(3)]
    ids = [c["id"] for c in citations]
    mkfact(statement="e", citations=ids)
    hypothesis = mkhypothesis(supporting=ids)
    cfg = runconfig.default("q")
    gap = gates.evidence_gap(Graph(mem), cfg, hypothesis["id"])
    assert gap and "domain" in gap


def test_evidence_gap_is_none_when_the_bar_is_met(
    mem, mkcitation, mkfact, mkhypothesis
):
    citations = [mkcitation(url=f"https://d{i}.example/x",
                            domain=f"d{i}.example", quote=f"a quoted span {i}")
                 for i in range(3)]
    ids = [c["id"] for c in citations]
    mkfact(statement="e", citations=ids)
    hypothesis = mkhypothesis(supporting=ids)
    assert gates.evidence_gap(Graph(mem), runconfig.default("q"),
                              hypothesis["id"]) is None


def test_evidence_gap_uses_the_run_s_own_thresholds(
    mem, mkcitation, mkfact, mkhypothesis
):
    citations = [mkcitation(url=f"https://d{i}.example/x",
                            domain=f"d{i}.example", quote=f"a quoted span {i}")
                 for i in range(3)]
    ids = [c["id"] for c in citations]
    mkfact(statement="e", citations=ids)
    hypothesis = mkhypothesis(supporting=ids)
    cfg = runconfig.default("q")
    cfg["config"]["required_domains"] = 4
    assert gates.evidence_gap(Graph(mem), cfg, hypothesis["id"])
