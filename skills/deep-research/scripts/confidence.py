"""Hypothesis confidence, derived only from evidence.

No model ever sets a confidence value. It is a function of how many live
citations support a claim, how independent their sources are, how much
live evidence argues against it, and what the adversarial verifier
concluded.
"""

VERDICT_WEIGHT = {
    "supported": 1.0,
    # Strictly below `None`. Both used to be 0.5, so a verifier that read
    # the quotes and found they did not establish the claim left it
    # scored exactly as if nobody had looked -- and verifier.md calls
    # this "the right answer far more often than it feels like". It does
    # not change promotion (apply._verified_status returns `proposed` for
    # any verdict but `supported`); it changes the
    # min_hypothesis_confidence predicate and Limitations' `thin` count,
    # which were both reporting an adversarial rejection as though it
    # were an absence of work.
    "unsupported": 0.3,
    "contradicted": 0.0,
    None: 0.5,
}


def compute(domains, verdict, required_domains=2, counter=0,
            min_citations=3):
    """
    domains: the eTLD+1 of every *live* supporting citation, one entry per
    citation. Duplicates are expected when one source supplies several
    quotes, and they no longer raise the score -- only the DISTINCT set
    does.
    required_domains: accepted and unused. Kept in the signature because
    every caller passes it and gate 3 still enforces it; see the note in
    the body for why it left the arithmetic.
    min_citations: gate 3's citation bar. Present so the score cannot
    call evidence promotable that gate 3 rejects -- see the volume term.
    counter: how many *live* citations argue against the claim. Live by the
    same test as `domains` -- Graph.live_citations -- because a citation
    gate 2 rejected, or one no active fact cites any more, is not evidence
    against anything.
    """
    count = len(domains)
    if count == 0:
        return 0.0
    # Two terms, and both are needed. This was `n/(n+2) * min(1,
    # distinct/required_domains)`, which had two defects: the spread
    # clamped at required_domains, so a third independent domain was
    # worth nothing (3 citations across 3 domains scored the same 0.60
    # as 3 across 2); and volume outranked breadth -- 5 citations across
    # 2 domains scored 0.71 and beat 3 across 3, which is exactly what
    # gate 3 exists to prevent. Uncapping the spread does NOT fix the
    # second: `n/(n+2)` grows on citation count faster than any bounded
    # spread grows on domains, and the inversion survives at 0.48
    # against 0.45. Measured before choosing this.
    #
    # `volume` saturates AT gate 3's citation bar and never above it. Ten
    # quotes from two sites are still two sources, so past the bar it
    # contributes nothing -- but below the bar it must still pull the
    # score down, because `apply._verified_status` promotes on the score
    # alone and never consults gate 3. Dropping this term entirely (an
    # earlier attempt) let 2 citations on 2 domains score exactly the
    # threshold and promote, silently bypassing min_citations.
    #
    # `breadth` is unbounded-ish rather than clamped, so the third and
    # fourth independent source keep paying.
    #
    # The pair is calibrated: gate 3's minimum scores exactly
    # promotion_threshold's default, and the supremum of an UNVERIFIED
    # claim -- weight 0.5, both terms at 1.0 -- is 0.5, strictly below
    # it. That second margin is gate 4's whole guarantee, and it is why
    # `breadth` is d/(d+1) rather than d/(d+2): the latter puts gate 3's
    # minimum at 0.5 too, colliding with the unverified supremum and
    # letting a claim nobody verified promote.
    volume = min(1.0, count / min_citations) if min_citations else 1.0
    breadth = len(set(domains)) / (len(set(domains)) + 1)
    base = volume * breadth
    # The share of live evidence that supports the claim. Before this,
    # `counter` was never read here at all: 3 supporting citations against
    # 15 live counter citations scored 0.6, stayed promoted, and reached
    # the report body as a finding. Plan 8 taught the run to go out and
    # look for disconfirmation; this is what lets the arithmetic hear it.
    #
    # Multiplicative and EXACTLY 1.0 at zero opposition, which is not
    # cosmetic: the defaults are tuned so gate 3's minimum (3 citations,
    # 2 domains, `supported`) scores exactly promotion_threshold, and
    # runconfig.warnings reports "nothing will ever be promoted" if that
    # stops being true. A subtractive penalty would also need clamping
    # and could drive a well-evidenced claim negative; this is bounded in
    # (0, 1] by construction.
    #
    # `count`, not `len(set(domains))`, and this is now an ASYMMETRY
    # rather than a match: `base` reads distinct sources, opposition
    # reads citation volume. Deliberate and narrow — fifteen
    # contradicting quotes are more opposition than one, whoever
    # published them — but it means support and opposition are measured
    # on different scales. Left as it is because plan 11's demotion
    # behaviour is pinned to this shape; noted so nobody reads the match
    # that used to be here into it.
    opposition = count / (count + max(0, counter))
    weight = VERDICT_WEIGHT.get(verdict, 0.5)
    return round(base * opposition * weight, 2)
