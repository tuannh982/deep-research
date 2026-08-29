import confidence


def test_no_evidence_is_zero():
    assert confidence.compute([], "supported") == 0.0


def test_a_contradicted_verdict_is_zero_however_much_evidence():
    assert confidence.compute(["a.com", "b.com", "c.com"], "contradicted") == 0.0


def test_gate_three_minimum_lands_exactly_on_the_promotion_threshold():
    # 3 citations across 2 distinct domains, verdict supported.
    # Hand-computed: base = distinct/(distinct+2) = 2/4 = 0.5. It was
    # 0.6 under n/(n+2) * spread, and promotion_threshold's default
    # moved with it — the invariant is that these two are equal, not
    # that either has a particular value.
    assert confidence.compute(["a.com", "a.com", "b.com"], "supported") == 0.67


def test_more_citations_raise_confidence():
    fewer = confidence.compute(["a.com", "b.com"], "supported")
    more = confidence.compute(["a.com", "b.com", "c.com", "d.com"], "supported")
    assert more > fewer


def test_a_single_domain_is_penalised_against_two():
    one = confidence.compute(["a.com", "a.com", "a.com"], "supported")
    two = confidence.compute(["a.com", "a.com", "b.com"], "supported")
    assert one < two


def test_raising_the_domain_requirement_no_longer_moves_the_score():
    """INVERTED. This pinned `spread = min(1, distinct/required_domains)`,
    which is the term that made a third independent domain worthless.
    required_domains now lives only in gate 3's independence(), which is
    the bar it was always meant to be — one knob was doing two jobs."""
    two = confidence.compute(["a.com", "b.com", "a.com"], "supported",
                             required_domains=2)
    three = confidence.compute(["a.com", "b.com", "a.com"], "supported",
                               required_domains=3)
    assert two == three


def test_extra_domains_beyond_the_requirement_do_stack():
    """INVERTED, and this test was a description of the defect: "both
    are at or past the requirement, so spread clamps to 1.0 and the
    extra domain buys nothing". A third independent source is more
    corroboration than a second, and gate 3's whole purpose is source
    independence. Hand-computed: 2/4 = 0.5, 3/5 = 0.6."""
    at_requirement = confidence.compute(["a.com", "a.com", "b.com"], "supported",
                                        required_domains=2)
    beyond = confidence.compute(["a.com", "b.com", "c.com"], "supported",
                                required_domains=2)
    assert at_requirement == 0.67
    assert beyond == 0.75


def test_an_unsupported_verdict_costs_more_than_never_being_checked():
    """Renamed from test_an_unjudged_hypothesis_is_treated_as_unsupported,
    which pinned the defect: both weighed 0.5, so a verifier that read the
    quotes and concluded they do not establish the claim left it scored
    exactly as if nobody had looked. verifier.md calls `unsupported` "the
    right answer far more often than it feels like" — and it cost
    nothing.

    Promotion is unaffected either way (_verified_status returns
    `proposed` for any verdict but `supported`); what this moves is the
    min_hypothesis_confidence predicate and the Limitations `thin`
    count, both of which get more honest."""
    unsupported = confidence.compute(["a.com", "b.com"], "unsupported")
    unjudged = confidence.compute(["a.com", "b.com"], None)
    assert unsupported < unjudged


def test_an_unjudged_hypothesis_is_still_well_below_a_supported_one():
    supported = confidence.compute(["a.com", "b.com"], "supported")
    assert confidence.compute(["a.com", "b.com"], None) < supported


# --- opposition ------------------------------------------------------
#
# compute never saw `counter`, so no amount of accumulated
# counter-evidence could move a claim. Measured before this: 3 supporting
# citations against 15 LIVE counter citations scored 0.6, stayed
# `contested`, and still reached the report body as a finding. The only
# thing that could refute anything was one verifier returning
# `contradicted`.

def test_no_counter_evidence_scores_exactly_as_before():
    """THE CALIBRATION GUARD, and the reason the term is multiplicative
    and equals 1.0 at zero opposition.

    The defaults are tuned so gate 3's minimum — 3 citations across 2
    domains with a `supported` verdict — scores exactly
    promotion_threshold. runconfig.warnings checks it and reports
    "nothing will ever be promoted" if the best score gate 3 admits
    falls below the bar. A term that were not exactly 1.0 here would
    break every default configuration."""
    assert confidence.compute(["a.com", "a.com", "b.com"], "supported") == 0.67
    assert confidence.compute(["a.com", "a.com", "b.com"], "supported",
                              counter=0) == 0.67


def test_counter_evidence_lowers_the_score():
    without = confidence.compute(["a.com", "b.com", "c.com"], "supported")
    with_one = confidence.compute(["a.com", "b.com", "c.com"], "supported",
                                  counter=1)
    assert with_one < without


def test_overwhelming_counter_evidence_demotes_below_the_threshold():
    """The measured case: 3 supporting against 15 live counters scored
    0.6 — exactly the promotion threshold — and was reported as a
    finding."""
    score = confidence.compute(["a.com", "a.com", "b.com"], "supported",
                               counter=15)
    assert score < 0.67


def test_one_counter_against_minimum_evidence_demotes():
    """Intended, and pinned so nobody softens it without reading why. A
    claim sitting at exactly the gate-3 minimum with one live citation
    arguing against it is not a finding; it is an open question. It also
    gives `contested` a sharper meaning — evidence strong enough to
    absorb the opposition and still clear the bar."""
    assert confidence.compute(["a.com", "a.com", "b.com"], "supported",
                              counter=1) < 0.67


def test_a_well_evidenced_claim_survives_one_counter():
    """`contested` has to stay reachable or the status is dead and every
    dispute reads as a demotion."""
    assert confidence.compute([f"d{i}.com" for i in range(9)], "supported",
                              counter=1) >= 0.67


def test_more_opposition_is_always_worse():
    scores = [confidence.compute([f"d{i}.com" for i in range(9)], "supported",
                                 counter=n) for n in range(5)]
    assert scores == sorted(scores, reverse=True)


def test_counter_evidence_cannot_drive_the_score_negative():
    """Multiplicative rather than subtractive: bounded in (0, 1] by
    construction, so no clamp is needed and no count can push a claim
    below zero."""
    assert confidence.compute(["a.com", "b.com"], "supported",
                              counter=10_000) >= 0.0


def test_confidence_never_exceeds_one():
    domains = [f"d{i}.com" for i in range(50)]
    assert confidence.compute(domains, "supported") <= 1.0


def test_result_is_rounded_to_two_places():
    value = confidence.compute(["a.com", "b.com", "c.com"], "supported")
    assert value == round(value, 2)


# --- independence, not volume -----------------------------------------
#
# `base` was n/(n+2) over raw citations and `spread` was
# min(1, distinct/required_domains). Two defects fell out of that:
# a third independent domain was worth nothing (spread already clamped),
# and volume from two sites outranked genuine breadth. Measured:
#
#   3 citations / 2 domains  0.60      3 citations / 3 domains  0.60
#   5 citations / 2 domains  0.71  <-- beats 3 across 3
#
# Uncapping the spread term does NOT fix it — base grows on citation
# count faster than any bounded spread grows on domains, and the
# inversion survives at 0.48 vs 0.45. The fix is to stop scoring volume:
# ten quotes from two sites are still two sources. min_citations remains
# gate 3's separate bar, so three citations are still required.

def test_gate_three_minimum_still_lands_on_the_promotion_threshold():
    """THE CALIBRATION GUARD, written first. runconfig.warnings reports
    "nothing will ever be promoted" if the best score gate 3 admits
    falls below the threshold, so the default threshold moves with the
    formula or every default configuration stops promoting."""
    import runconfig
    best = confidence.compute(["a.com", "a.com", "b.com"], "supported")
    assert best == runconfig.default("q")["config"]["promotion_threshold"]


def test_a_third_independent_domain_raises_the_score():
    """Worth nothing before: spread clamped at required_domains, so
    3 citations across 3 domains scored the same 0.60 as 3 across 2."""
    two = confidence.compute(["a.com", "a.com", "b.com"], "supported")
    three = confidence.compute(["a.com", "b.com", "c.com"], "supported")
    assert three > two


def test_volume_within_the_same_sources_does_not_raise_the_score():
    """Ten quotes from two sites are still two sources."""
    few = confidence.compute(["a.com", "a.com", "b.com"], "supported")
    many = confidence.compute(["a.com"] * 8 + ["b.com"] * 4, "supported")
    assert many == few


def test_breadth_beats_volume():
    """THE INVERSION. 5 citations across 2 domains scored 0.71 and beat
    3 across 3 at 0.60 — volume from the same two sites outranking
    genuine independence, which is precisely what gate 3 exists to
    protect."""
    volume = confidence.compute(["a.com"] * 3 + ["b.com"] * 2, "supported")
    breadth = confidence.compute(["a.com", "b.com", "c.com"], "supported")
    assert breadth > volume


def test_required_domains_no_longer_enters_the_score():
    """It lives in gate 3's independence() now, which is where it always
    belonged. Leaving it in the score meant one knob doing two jobs."""
    assert (confidence.compute(["a.com", "b.com"], "supported",
                               required_domains=2)
            == confidence.compute(["a.com", "b.com"], "supported",
                                  required_domains=9))


def test_one_counter_at_the_gate_three_minimum_still_demotes():
    """Plan 11's behaviour has to survive the re-tuning: minimum
    evidence plus one live counter is not a finding."""
    import runconfig
    threshold = runconfig.default("q")["config"]["promotion_threshold"]
    assert confidence.compute(["a.com", "a.com", "b.com"], "supported",
                              counter=1) < threshold
