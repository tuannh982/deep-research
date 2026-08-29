"""Extract, hypothesize, verify. Extract only seeds a re-check task now
(gate 2 itself moved to a `recheck` subagent, Task 6's job); spec section
6's verdict-to-graph transition lives in the third."""
import pytest

import apply
import confidence
import evidence
import gates
import nodes
import runconfig
from graph import Graph


def _delete_field(mem, node_id, field):
    """Corrupt a node in place: schema-valid minus one required key. Still
    *parses* (graph.tasks / index_of keep it) but is no longer schema-valid
    (graph.valid_task_ids() / index_of's own validate() drop it) -- the
    "parses but is not valid" state graph.py's docstrings describe, not the
    "does not even parse" state test_index_of_skips_a_corrupt_node covers."""
    path = mem.path_for(node_id)
    data = nodes.loads(path.read_text(encoding="utf-8"))
    del data[field]
    path.write_text(nodes.dumps(data), encoding="utf-8")


def _set_invalid_field(mem, node_id, field, value):
    """Corrupt a node in place: still parses, but `field` now holds a
    value its schema rejects. Complements _delete_field for a required
    field that cannot simply be removed (e.g. assumption.refuted_by,
    which is required but nullable -- only an out-of-pattern STRING makes
    it schema-invalid without touching whether the key is present)."""
    path = mem.path_for(node_id)
    data = nodes.loads(path.read_text(encoding="utf-8"))
    data[field] = value
    path.write_text(nodes.dumps(data), encoding="utf-8")


def _corrupt(mem, node_id):
    """Overwrite a node with bytes nodes.loads cannot parse at all --
    NodeFormatError, not merely schema-invalid. Mirrors
    test_index_of_skips_a_corrupt_node's own technique."""
    mem.path_for(node_id).write_text("garbage\n", encoding="utf-8")


URL = "https://a-example.com/latency"


@pytest.fixture
def cfg():
    return runconfig.default("what drives p99?")


@pytest.fixture
def extractor(mktask):
    return mktask(question="What drives p99?", kind="extract", depth=1)


def extract_artifact(task_id, quotes=("The service reports 42ms at p99",),
                     url=URL):
    return {
        "task_id": task_id, "url": url,
        "facts": [{"statement": f"Fact about {q[:12]}", "quote": q}
                  for q in quotes],
        "published_at": None,
        "source_type": "primary",
        "no_facts_reason": None,
    }


def run_extract(mem, cfg, tmp_path, task, artifact):
    return apply.apply_extract(
        mem, Graph(mem), cfg, task["id"], task, artifact, root=tmp_path)


# --- what was actually searched for -----------------------------------
#
# The searcher reported the sources it found and never the queries it
# issued, so no part of a run's literature search could be re-run or
# assessed for coverage. It is also what makes the saturation halt
# interpretable: six dry tasks might mean the question is exhausted or
# might mean the queries were a monoculture, and nothing could tell them
# apart.

def _search_task(mem, mktask):
    return mktask(question="how does scattering work?", kind="search",
                  depth=1)


def test_a_search_records_the_queries_it_reports(mem, cfg, mktask):
    task = _search_task(mem, mktask)
    apply.apply_search(
        mem, Graph(mem), cfg, task["id"], task,
        {"task_id": task["id"], "no_sources_reason": None,
         "queries": ["rayleigh scattering wavelength", "why sky blue"],
         "sources": [{"url": "https://a-example.com/p", "title": "t",
                      "relevance": 0.9, "why": "w"}]})
    assert mem.read(task["id"])["queries"] == [
        "rayleigh scattering wavelength", "why sky blue"]


def test_a_search_that_found_nothing_still_records_its_queries(
    mem, cfg, mktask
):
    """The case a reader most needs. An empty search is exactly where
    "the question is exhausted" and "we asked badly" have to be told
    apart, and only the queries can do it."""
    task = _search_task(mem, mktask)
    apply.apply_search(
        mem, Graph(mem), cfg, task["id"], task,
        {"task_id": task["id"], "sources": [],
         "queries": ["a query that found nothing"],
         "no_sources_reason": "nothing usable"})
    assert mem.read(task["id"])["queries"] == ["a query that found nothing"]


def test_a_search_artifact_with_no_queries_is_rejected(mem, cfg, mktask):
    """Gate 1. Self-healing: a rejection re-emits the task with the
    validator error attached, so the retry carries them — at the cost of
    one of three attempts for a search in flight when this shipped."""
    task = _search_task(mem, mktask)
    error = gates.schema_check("search", {
        "task_id": task["id"], "sources": [],
        "no_sources_reason": "nothing"}, task["id"])
    assert error is not None and "queries" in error


def test_a_task_written_before_queries_existed_still_validates(mem, mktask):
    """task.json is additionalProperties: false, so the field is OPTIONAL
    on the node even though the artifact requires it. abandoned_reason is
    the precedent: an optional top-level field written after the fact."""
    task = mktask(question="q", kind="search")
    assert "queries" not in task
    mem.validate(task)


# --- when the source was published ------------------------------------
#
# The bibliography printed "Retrieved <fetched_at>", and fetched_at is
# written by apply_recheck — so the only date on a source was the date we
# re-read it. A reader could not tell a 2011 page from a 2025 one.

def test_an_extraction_records_the_pages_publication_date(
    mem, cfg, tmp_path, extractor
):
    artifact = extract_artifact(extractor["id"])
    artifact["published_at"] = "2019-03-04"
    run_extract(mem, cfg, tmp_path, extractor, artifact)
    assert mem.list("citation")[0]["published_at"] == "2019-03-04"


def test_a_page_with_no_date_stores_null(mem, cfg, tmp_path, extractor):
    """The ordinary case, not a failure. Plenty of pages carry no date,
    or carry a misleading one — a "last updated" banner, a copyright
    footer, a comment timestamp. A model pressed to fill the field
    guesses, and an invented date in a bibliography is worse than an
    honest gap."""
    artifact = extract_artifact(extractor["id"])
    artifact["published_at"] = None
    run_extract(mem, cfg, tmp_path, extractor, artifact)
    assert mem.list("citation")[0]["published_at"] is None


def test_a_year_only_publication_date_is_accepted(
    mem, cfg, tmp_path, extractor
):
    """Many pages give only a year. Demanding a full ISO date would make
    the model invent a month and a day to satisfy the schema."""
    artifact = extract_artifact(extractor["id"])
    artifact["published_at"] = "2019"
    run_extract(mem, cfg, tmp_path, extractor, artifact)
    assert mem.list("citation")[0]["published_at"] == "2019"


def test_reuse_fills_an_absent_date_but_never_overwrites_one(
    mem, cfg, tmp_path, extractor, mktask
):
    """CITATION_KEY dedups on (url, quote_sha256), so a second extraction
    of the same page reuses the citation. If the first reading found no
    date and the second did, take it — but never replace one that is
    already there: the readings are equally authoritative and rewriting
    invites a flip-flop between two that disagree."""
    first = extract_artifact(extractor["id"])
    first["published_at"] = None
    run_extract(mem, cfg, tmp_path, extractor, first)
    citation_id = mem.ids("citation")[0]

    second_task = mktask(question="read it again", kind="extract", depth=1)
    filled = extract_artifact(second_task["id"])
    filled["published_at"] = "2019-03-04"
    run_extract(mem, cfg, tmp_path, second_task, filled)
    assert mem.read(citation_id)["published_at"] == "2019-03-04"

    third_task = mktask(question="read it once more", kind="extract", depth=1)
    disagreeing = extract_artifact(third_task["id"])
    disagreeing["published_at"] = "2011-01-01"
    run_extract(mem, cfg, tmp_path, third_task, disagreeing)
    assert mem.read(citation_id)["published_at"] == "2019-03-04"


def test_a_citation_written_before_this_field_still_validates(mem):
    """citation.json is additionalProperties: false, so the field has to
    be OPTIONAL. Required, it would invalidate every citation already on
    disk — dropping each out of live_citations, rescoring its hypotheses
    to 0 and quarantining its facts on the first submit after the
    upgrade. page_sha256's description costs that out in full."""
    citation = mem.create("citation", {
        "url": "https://a-example.com/p", "domain": "a-example.com",
        "title": "t", "quote": "a quoted span here",
        "quote_sha256": "0" * 64, "status": "pending",
        "http_status": None, "fetched_at": None,
        "provenance": {"task": None, "agent": "extractor"}})
    assert "published_at" not in citation
    mem.validate(citation)


# --- extract: the happy path -----------------------------------------

def test_a_quote_becomes_a_pending_citation_and_an_active_fact(
    mem, cfg, tmp_path, extractor
):
    """Renamed from ..._a_verified_quote_...: gate 2 no longer runs
    inside apply_extract (see the module docstring), so a fresh citation
    is born `pending`, not `verified`. The fact still lands `active` --
    the re-check task, not the fact, is what carries the unresolved
    question forward."""
    run_extract(mem, cfg, tmp_path, extractor, extract_artifact(extractor["id"]))
    citation = mem.list("citation")[0]
    fact = mem.list("fact")[0]
    assert citation["status"] == "pending"
    assert fact["citations"] == [citation["id"]]


def test_the_citation_records_the_registrable_domain(
    mem, cfg, tmp_path, extractor
):
    """Gate 3 counts distinct values of this field. A raw host stored
    here silently defeats it."""
    artifact = extract_artifact(extractor["id"], url="https://blog.a-example.com/x")
    apply.apply_extract(
        mem, Graph(mem), cfg, extractor["id"], extractor, artifact,
        root=tmp_path)
    assert mem.list("citation")[0]["domain"] == "a-example.com"


def test_the_citation_stores_the_quote_verbatim(mem, cfg, tmp_path, extractor):
    """Byte fidelity. Plan 1 went to some trouble for this and gate 2
    does not need it changed."""
    quote = "The service reports\n   42ms   at p99"
    run_extract(mem, cfg, tmp_path, extractor,
                extract_artifact(extractor["id"], quotes=(quote,)))
    assert mem.list("citation")[0]["quote"] == quote


def test_the_quote_hash_is_of_the_normalized_quote(
    mem, cfg, tmp_path, extractor
):
    quote = "The service reports\n   42ms   at p99"
    run_extract(mem, cfg, tmp_path, extractor,
                extract_artifact(extractor["id"], quotes=(quote,)))
    assert mem.list("citation")[0]["quote_sha256"] == \
        evidence.sha256_of(evidence.normalize(quote))


def test_the_same_span_quoted_two_ways_is_one_citation(
    mem, cfg, tmp_path, extractor, mktask
):
    """Spec section 2: 'a source cited by twelve facts must not be stored
    twelve times.' Only hashing the normalized form makes that true."""
    run_extract(mem, cfg, tmp_path, extractor, extract_artifact(
        extractor["id"], quotes=("The service reports 42ms at p99",)))
    second = mktask(question="another angle", kind="extract", depth=1)
    apply.apply_extract(
        mem, Graph(mem), cfg, second["id"], second,
        extract_artifact(second["id"],
                         quotes=("The  service   reports 42ms at p99",)),
        root=tmp_path)
    assert len(mem.ids("citation")) == 1
    assert len(mem.ids("fact")) == 2


def test_the_fact_is_provenanced_to_the_extract_task(
    mem, cfg, tmp_path, extractor
):
    """The cascade's quarantine pass matches on this."""
    run_extract(mem, cfg, tmp_path, extractor, extract_artifact(extractor["id"]))
    assert mem.list("fact")[0]["provenance"] == {"task": extractor["id"],
                                                 "agent": "extractor"}


# --- extract: guards --------------------------------------------------

def test_an_artifact_naming_a_different_url_than_the_task_is_rejected(
    mem, cfg, tmp_path, mktask
):
    """The extractor was given one page. Reading another means the
    re-check this applier seeds would confirm a page nobody asked
    about."""
    task = mktask(question="q", kind="extract", depth=1)
    mem.update(task["id"], inputs={"url": URL, "title": "t",
                                   "domain": "a-example.com"})
    task = mem.read(task["id"])
    with pytest.raises(apply.ApplyError, match="url"):
        run_extract(mem, cfg, tmp_path, task,
                    extract_artifact(task["id"], url="https://other-example.com/z"))


def test_a_url_with_no_registrable_domain_is_an_apply_error(
    mem, cfg, tmp_path, extractor
):
    with pytest.raises(apply.ApplyError):
        run_extract(mem, cfg, tmp_path, extractor,
                    extract_artifact(extractor["id"],
                                     url="https://localhost/x"))


def test_an_empty_fact_list_creates_nothing_and_seeds_no_recheck(
    mem, cfg, tmp_path, extractor
):
    """Renamed from ..._fetches_nothing: apply_extract never fetches at
    all now, so the old assertion (a stub `fetch` that raised if called)
    guarded a seam that no longer exists. What still matters: a page with
    nothing extracted from it seeds no work. _seed_recheck returns early
    when `citation_for` is empty, so a login wall spawns no re-check for
    zero quotes."""
    result = apply.apply_extract(
        mem, Graph(mem), cfg, extractor["id"], extractor,
        {"task_id": extractor["id"], "url": URL, "facts": [],
         "published_at": None,
         "source_type": "primary",
         "no_facts_reason": "the page is a login wall"},
        root=tmp_path)
    assert result.created == []
    assert [t for t in mem.list("task") if t["kind"] == "recheck"] == []


def test_applying_the_same_extract_twice_creates_nothing_new(
    mem, cfg, tmp_path, extractor
):
    run_extract(mem, cfg, tmp_path, extractor, extract_artifact(extractor["id"]))
    before = mem.all_ids()
    run_extract(mem, cfg, tmp_path, extractor, extract_artifact(extractor["id"]))
    assert mem.all_ids() == before


# --- extract: pending citations and the re-check ----------------------

def test_extract_creates_a_pending_citation(mem, cfg, tmp_path, extractor):
    """Gate 2 no longer runs here; it runs on a later tick, in an agent.
    Until it does, the citation is unchecked and must say so."""
    run_extract(mem, cfg, tmp_path, extractor, extract_artifact(extractor["id"]))
    citation = mem.list("citation")[0]
    assert citation["status"] == "pending"
    assert citation["http_status"] is None
    assert citation["fetched_at"] is None


def test_a_pending_citation_counts_for_nothing(
    mem, cfg, mkcitation, mkfact, mkhypothesis
):
    """The safety property. If a pending citation reached
    supporting_domains, a run would promote claims on evidence nobody has
    checked -- which is the whole failure gate 2 exists to prevent.

    Needs an ACTIVE FACT citing it: Graph.live_citations has two
    necessary conditions -- some active fact must cite the citation, AND
    the citation's own status must be `verified` -- and short-circuits on
    the first. Without a citing fact, the citation is never a candidate
    in the first place and the `status == "pending"` branch is never
    exercised, so this test would still pass even if `live_citations`
    admitted `pending` outright."""
    citation = mkcitation(status="pending")
    mkfact(statement="f", citations=[citation["id"]])
    hypothesis = mkhypothesis(supporting=[citation["id"]], verdict="supported")
    domains = Graph(mem).supporting_domains(hypothesis["id"])
    assert domains == []
    assert confidence.compute(domains, "supported") == 0.0


def test_extract_seeds_one_recheck_task_for_the_page(
    mem, cfg, tmp_path, extractor
):
    artifact = extract_artifact(
        extractor["id"],
        quotes=("The service reports 42ms at p99",
                "Cold starts account for most of the tail"))
    run_extract(mem, cfg, tmp_path, extractor, artifact)
    rechecks = [t for t in mem.list("task") if t["kind"] == "recheck"]
    assert len(rechecks) == 1
    recheck = rechecks[0]
    assert recheck["inputs"]["url"] == URL
    quotes = recheck["inputs"]["quotes"]
    citation_ids = recheck["inputs"]["citations"]
    # The actual count against the fixture, not just the two lists against
    # each other: `zip` truncates silently on a length mismatch, so
    # comparing `len(quotes) == len(citation_ids)` alone would not catch
    # both lists being truncated to the same (wrong) shorter length --
    # exactly what a `sorted(citation_for)[:1]` bug would produce, leaving
    # every quote past the first `pending` forever with no recheck to ever
    # resolve it.
    assert len(quotes) == 2
    assert len(citation_ids) == 2
    citation_for_quote = {c["quote"]: c["id"] for c in mem.list("citation")}
    assert set(citation_ids) == set(citation_for_quote.values())
    for quote, citation_id in zip(quotes, citation_ids):
        assert citation_for_quote[quote] == citation_id


def test_the_recheck_task_is_not_deeper_than_its_extract_task(
    mem, cfg, tmp_path, mktask
):
    """A +1 here would push some re-checks past max_depth, making them
    undispatchable -- and an undispatchable re-check leaves its citations
    pending forever, so nothing they support can ever be promoted."""
    assert cfg["config"]["max_depth"] == 4
    deep = mktask(question="deep extract", kind="extract", depth=3)
    run_extract(mem, cfg, tmp_path, deep, extract_artifact(deep["id"]))
    recheck = next(t for t in mem.list("task") if t["kind"] == "recheck")
    assert recheck["depth"] == 3


def test_re_applying_an_extract_artifact_seeds_no_second_recheck(
    mem, cfg, tmp_path, extractor
):
    """Multiple quotes, deliberately: the default single-quote artifact
    never exercises quote ORDER at all (one-element collections have only
    one order), so it cannot pin the reason _seed_recheck sorts --
    TASK_KEY hashes `canonical(inputs)`, and an unstable order across the
    two applications below would seed a second recheck task on a set
    whose iteration order isn't guaranteed to repeat."""
    artifact = extract_artifact(
        extractor["id"],
        quotes=("The service reports 42ms at p99",
                "Cold starts account for most of the tail",
                "A third paragraph"))
    run_extract(mem, cfg, tmp_path, extractor, artifact)
    run_extract(mem, cfg, tmp_path, extractor, artifact)
    rechecks = [t for t in mem.list("task") if t["kind"] == "recheck"]
    assert len(rechecks) == 1
    assert rechecks[0]["inputs"]["quotes"] == sorted(
        rechecks[0]["inputs"]["quotes"])


def test_a_quote_too_short_to_be_evidence_gets_no_citation(
    mem, cfg, tmp_path, extractor
):
    """Kept from the old behaviour: gate 1 refuses these, so this is
    unreachable on the real path -- but "unreachable" and "cannot crash
    submit" are different promises and only the second is worth anything.
    schemas/citation.json will not store such a quote."""
    artifact = extract_artifact(extractor["id"], quotes=("abc",))
    run_extract(mem, cfg, tmp_path, extractor, artifact)
    assert mem.ids("citation") == []


# --- scheduling the hypothesizer -------------------------------------

def test_a_branch_with_enough_facts_gets_a_hypothesize_task(
    mem, cfg, mktask, mkcitation, mkfact
):
    root = mktask(question="root", kind="decompose")
    worker = mktask(question="w", kind="extract", parent=root["id"],
                    depth=1, status="done")
    for i in range(3):
        citation = mkcitation(url=f"https://d{i}-example.com/x",
                              domain=f"d{i}-example.com", quote=f"a quoted span {i}")
        mkfact(statement=f"f{i}", citations=[citation["id"]],
               task=worker["id"])
    result = apply.ensure_hypothesize_tasks(mem, Graph(mem), cfg)
    spawned = [t for t in mem.list("task") if t["kind"] == "hypothesize"]
    assert len(spawned) == 1
    assert spawned[0]["parent"] == root["id"]
    assert result.spawned == [spawned[0]["id"]]


def test_a_branch_short_of_min_citations_gets_nothing(
    mem, cfg, mktask, mkcitation, mkfact
):
    root = mktask(question="root", kind="decompose")
    worker = mktask(question="w", kind="extract", parent=root["id"],
                    depth=1, status="done")
    citation = mkcitation(url="https://d-example.com/x", domain="d-example.com")
    mkfact(statement="f", citations=[citation["id"]], task=worker["id"])
    apply.ensure_hypothesize_tasks(mem, Graph(mem), cfg)
    assert [t for t in mem.list("task") if t["kind"] == "hypothesize"] == []


def test_a_quarantined_fact_does_not_count_toward_the_bar(
    mem, cfg, mktask, mkcitation, mkfact
):
    root = mktask(question="root", kind="decompose")
    worker = mktask(question="w", kind="extract", parent=root["id"],
                    depth=1, status="done")
    for i in range(3):
        citation = mkcitation(url=f"https://d{i}-example.com/x",
                              domain=f"d{i}-example.com", quote=f"a quoted span {i}")
        mkfact(statement=f"f{i}", citations=[citation["id"]],
               task=worker["id"], status="quarantined")
    apply.ensure_hypothesize_tasks(mem, Graph(mem), cfg)
    assert [t for t in mem.list("task") if t["kind"] == "hypothesize"] == []


def test_a_second_call_does_not_add_a_second_hypothesize_task(
    mem, cfg, mktask, mkcitation, mkfact
):
    root = mktask(question="root", kind="decompose")
    worker = mktask(question="w", kind="extract", parent=root["id"],
                    depth=1, status="done")
    for i in range(3):
        citation = mkcitation(url=f"https://d{i}-example.com/x",
                              domain=f"d{i}-example.com", quote=f"a quoted span {i}")
        mkfact(statement=f"f{i}", citations=[citation["id"]],
               task=worker["id"])
    apply.ensure_hypothesize_tasks(mem, Graph(mem), cfg)
    apply.ensure_hypothesize_tasks(mem, Graph(mem), cfg)
    assert len([t for t in mem.list("task")
                if t["kind"] == "hypothesize"]) == 1


def test_a_new_hypothesize_task_is_scheduled_once_the_old_one_is_done(
    mem, cfg, mktask, mkcitation, mkfact
):
    """More facts arrive over a multi-day run. A branch whose hypothesizer
    already ran must be able to run again -- but only once genuinely new
    evidence gives the next round a different natural key (fix round 1:
    the original version of this test added no facts before the second
    call, so its own docstring's premise never held; see
    test_repeated_calls_with_no_new_evidence_do_not_duplicate_a_done_round
    for the case this test used to collapse into by accident)."""
    root = mktask(question="root", kind="decompose")
    worker = mktask(question="w", kind="extract", parent=root["id"],
                    depth=1, status="done")
    for i in range(3):
        citation = mkcitation(url=f"https://d{i}-example.com/x",
                              domain=f"d{i}-example.com", quote=f"a quoted span {i}")
        mkfact(statement=f"f{i}", citations=[citation["id"]],
               task=worker["id"])
    apply.ensure_hypothesize_tasks(mem, Graph(mem), cfg)
    old = next(t for t in mem.list("task") if t["kind"] == "hypothesize")
    mem.update(old["id"], status="done")
    fourth = mkcitation(url="https://d3-example.com/x", domain="d3-example.com",
                        quote="a quoted span three")
    mkfact(statement="f3", citations=[fourth["id"]], task=worker["id"])
    apply.ensure_hypothesize_tasks(mem, Graph(mem), cfg)
    assert len([t for t in mem.list("task")
                if t["kind"] == "hypothesize"]) == 2


def test_repeated_calls_with_no_new_evidence_do_not_duplicate_a_done_round(
    mem, cfg, mktask, mkcitation, mkfact
):
    """Critical 2 (fix round 1): a `done` hypothesize task's natural key
    must keep blocking a duplicate when nothing about the evidence has
    changed. Measured before this test existed: 5 ticks with no new
    evidence produced 5 hypothesize tasks, 5 duplicate hypothesis nodes
    and 5 verify tasks (each an LLM call), and eventually_dispatchable()
    never emptied, so the coverage halt predicate could never fire."""
    root = mktask(question="root", kind="decompose")
    worker = mktask(question="w", kind="extract", parent=root["id"],
                    depth=1, status="done")
    for i in range(3):
        citation = mkcitation(url=f"https://d{i}-example.com/x",
                              domain=f"d{i}-example.com", quote=f"a quoted span {i}")
        mkfact(statement=f"f{i}", citations=[citation["id"]],
               task=worker["id"])
    apply.ensure_hypothesize_tasks(mem, Graph(mem), cfg)
    old = next(t for t in mem.list("task") if t["kind"] == "hypothesize")
    mem.update(old["id"], status="done")
    for _ in range(4):
        apply.ensure_hypothesize_tasks(mem, Graph(mem), cfg)
    assert len([t for t in mem.list("task")
                if t["kind"] == "hypothesize"]) == 1


def test_two_branches_each_get_their_own_hypothesize_task(
    mem, cfg, mktask, mkcitation, mkfact
):
    for branch in range(2):
        root = mktask(question=f"root{branch}", kind="decompose")
        worker = mktask(question=f"w{branch}", kind="extract",
                        parent=root["id"], depth=1, status="done")
        for i in range(3):
            citation = mkcitation(url=f"https://b{branch}d{i}-example.com/x",
                                  domain=f"b{branch}d{i}-example.com",
                                  quote=f"branch {branch} span {i}")
            mkfact(statement=f"b{branch}f{i}", citations=[citation["id"]],
                   task=worker["id"])
    apply.ensure_hypothesize_tasks(mem, Graph(mem), cfg)
    assert len([t for t in mem.list("task")
                if t["kind"] == "hypothesize"]) == 2


@pytest.mark.parametrize("field", ["depth", "kind"])
def test_a_branch_whose_root_task_is_malformed_is_dropped_not_fatal(
    mem, cfg, mktask, mkcitation, mkfact, field
):
    """Important 4, site 2 (fix round 1), extended fix round 2:
    graph.tasks keeps every task that merely parses, valid or not
    (graph.py's own convention). `root["depth"]` on a branch root missing
    that field must not crash the scheduler -- but neither must the
    `busy` loop's own unguarded `task["kind"]`, scanned over EVERY task in
    the store before this branch is ever reached. The original version of
    this test deleted only `depth` and so never exercised that earlier
    site; parametrized over both fields so each guard is pinned on its
    own."""
    root = mktask(question="root", kind="decompose")
    worker = mktask(question="w", kind="extract", parent=root["id"],
                    depth=1, status="done")
    for i in range(3):
        citation = mkcitation(url=f"https://d{i}-example.com/x",
                              domain=f"d{i}-example.com", quote=f"a quoted span {i}")
        mkfact(statement=f"f{i}", citations=[citation["id"]],
               task=worker["id"])
    _delete_field(mem, root["id"], field)
    result = apply.ensure_hypothesize_tasks(mem, Graph(mem), cfg)
    assert result.spawned == []
    # .get(), not [...]: when `field` is "kind" itself, root's own record
    # is one of the tasks this assertion scans.
    assert [t for t in mem.list("task") if t.get("kind") == "hypothesize"] == []
    assert any(what == "task" for what, _ in result.dropped)


# --- hypothesize ------------------------------------------------------

@pytest.fixture
def evidence_scene(mem, mktask, mkcitation, mkfact):
    root = mktask(question="root", kind="decompose")
    worker = mktask(question="w", kind="extract", parent=root["id"], depth=1,
                    status="done")
    hypothesizer = mktask(question="form claims", kind="hypothesize",
                          parent=root["id"], depth=1)
    citations = [mkcitation(url=f"https://d{i}-example.com/x",
                            domain=f"d{i}-example.com", quote=f"a quoted span {i}")
                 for i in range(3)]
    for index, citation in enumerate(citations):
        mkfact(statement=f"f{index}", citations=[citation["id"]],
               task=worker["id"])
    return {"root": root, "task": hypothesizer,
            "citations": [c["id"] for c in citations]}


def widen_support(mem, mkcitation, mkfact, scene, hypothesis_id, extra=3):
    """Add `extra` more live supporting citations to a hypothesis.

    Needed wherever a test wants a claim to stay PROMOTED while carrying
    live counter-evidence. Since counter-evidence entered the confidence
    arithmetic, opposition is weighed against the volume of support, so
    a claim sitting at exactly the gate-3 minimum is demoted by a single
    counter — measured, 3 supporting and 1 counter gives 0.45 against a
    0.6 threshold. `contested` now means "strong enough to absorb the
    dispute and still clear the bar", and reaching it needs six
    supporting citations, not three.
    """
    added = []
    for index in range(extra):
        citation = mkcitation(url=f"https://w{index}-example.com/x",
                              domain=f"w{index}-example.com",
                              quote=f"a widening span {index}")["id"]
        mkfact(statement=f"widen {index}", citations=[citation],
               task=scene["task"]["id"])
        added.append(citation)
    hypothesis = mem.read(hypothesis_id)
    mem.update(hypothesis_id,
               supporting=sorted(set(hypothesis["supporting"]) | set(added)))
    return added


def hypothesize_artifact(task_id, supporting, refutes=None):
    return {
        "task_id": task_id,
        "hypotheses": [{"claim": "Cold starts dominate the tail",
                        "supporting": supporting, "counter": [],
                        "refutes": refutes}],
        "no_hypotheses_reason": None,
    }


def test_a_hypothesis_is_created_proposed_with_zero_confidence(
    mem, cfg, evidence_scene
):
    """No model ever sets a confidence value. It is recomputed from
    evidence at the end of the tick."""
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"],
                             evidence_scene["citations"]))
    hypothesis = mem.list("hypothesis")[0]
    assert hypothesis["status"] == "proposed"
    assert hypothesis["confidence"] == 0.0
    assert hypothesis["verdict"] is None


def test_a_verify_task_is_spawned_for_each_hypothesis(mem, cfg, evidence_scene):
    """Gate 4 is this task. Code cannot call an LLM, so the adversarial
    gate is dispatched by the loop's own machinery on the next tick."""
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"],
                             evidence_scene["citations"]))
    verify = next(t for t in mem.list("task") if t["kind"] == "verify")
    assert verify["inputs"]["hypothesis"] == mem.ids("hypothesis")[0]
    assert verify["status"] == "pending"


def test_a_refutes_proposal_is_carried_on_the_verify_task(
    mem, cfg, evidence_scene, mkassumption
):
    """The hypothesizer only proposes the link. Code sets
    assumption.refuted_by and runs the cascade if and only if the
    verifier returns `contradicted`, so the proposal has to survive until
    then — and the verify task's inputs is the only place it can."""
    assumption = mkassumption(raised_by=evidence_scene["root"]["id"])
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"],
                             evidence_scene["citations"],
                             refutes=assumption["id"]))
    verify = next(t for t in mem.list("task") if t["kind"] == "verify")
    assert verify["inputs"]["refutes"] == assumption["id"]
    assert mem.read(assumption["id"])["status"] == "open"


def test_an_invented_citation_id_is_an_apply_error(mem, cfg, evidence_scene):
    """Gate 1 checks the shape of a citation id; only the graph knows
    whether it exists. This is gate 5's rule applied early."""
    with pytest.raises(apply.ApplyError, match="C-404"):
        apply.apply_hypothesize(
            mem, Graph(mem), cfg, evidence_scene["task"]["id"],
            evidence_scene["task"],
            hypothesize_artifact(evidence_scene["task"]["id"], ["C-404"]))


def test_a_refutes_naming_no_assumption_is_an_apply_error(
    mem, cfg, evidence_scene
):
    with pytest.raises(apply.ApplyError, match="A-404"):
        apply.apply_hypothesize(
            mem, Graph(mem), cfg, evidence_scene["task"]["id"],
            evidence_scene["task"],
            hypothesize_artifact(evidence_scene["task"]["id"],
                                 evidence_scene["citations"],
                                 refutes="A-404"))


def test_a_citation_listed_as_both_supporting_and_counter_is_an_apply_error(
    mem, cfg, evidence_scene
):
    """Important A8 (fix round 2): JSON Schema cannot express cross-field
    disjointness, so gate 1 lets this through; only code can catch it.
    Before this guard it was also a route around Critical 1's fix: the
    citation is `in supporting`, so apply_verify's failing_citations
    guard permits rejecting it, and rejecting it also drops it from
    live_citations -- killing it as counter-evidence and promoting a
    hypothesis the graph still records a dispute against."""
    overlap = evidence_scene["citations"][0]
    artifact = {
        "task_id": evidence_scene["task"]["id"],
        "hypotheses": [{
            "claim": "Cold starts dominate the tail",
            "supporting": evidence_scene["citations"],
            "counter": [overlap], "refutes": None,
        }],
        "no_hypotheses_reason": None,
    }
    with pytest.raises(apply.ApplyError, match=overlap):
        apply.apply_hypothesize(
            mem, Graph(mem), cfg, evidence_scene["task"]["id"],
            evidence_scene["task"], artifact)


def test_applying_the_same_hypothesize_twice_creates_nothing_new(
    mem, cfg, evidence_scene
):
    artifact = hypothesize_artifact(evidence_scene["task"]["id"],
                                    evidence_scene["citations"])
    apply.apply_hypothesize(mem, Graph(mem), cfg,
                            evidence_scene["task"]["id"],
                            evidence_scene["task"], artifact)
    before = mem.all_ids()
    apply.apply_hypothesize(mem, Graph(mem), cfg,
                            evidence_scene["task"]["id"],
                            evidence_scene["task"], artifact)
    assert mem.all_ids() == before


# --- verify: the verdict transition ----------------------------------

@pytest.fixture
def verify_scene(mem, cfg, evidence_scene):
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"],
                             evidence_scene["citations"]))
    verify_task = next(t for t in mem.list("task") if t["kind"] == "verify")
    return {**evidence_scene, "verify": verify_task,
            "hypothesis": mem.ids("hypothesis")[0]}


def verify_artifact(task_id, hypothesis, verdict, failing=()):
    return {"task_id": task_id, "hypothesis": hypothesis, "verdict": verdict,
            "failing_citations": list(failing),
            "reasoning": "the quotes state the claim directly"}


def run_verify(mem, cfg, scene, verdict, failing=()):
    graph = Graph(mem, promotion_threshold=cfg["config"]["promotion_threshold"])
    return apply.apply_verify(
        mem, graph, cfg, scene["verify"]["id"], scene["verify"],
        verify_artifact(scene["verify"]["id"], scene["hypothesis"], verdict,
                        failing))


def test_a_verdict_records_the_reasoning_that_produced_it(
    mem, cfg, verify_scene
):
    """schemas/artifact.verify.json describes `reasoning` as "Journaled
    verbatim. 'Why is H-012 refuted' must be answerable three days later."
    Nothing kept it. journal_mod.append's artifact_applied record carries
    the verdict's EFFECTS -- created, dropped, spawned, cascaded -- never
    its content, and the only surviving copy was the artifact file submit
    moved to inbox/applied/, which appendices.py cannot read: it renders
    from a Graph. So the one question the field exists to answer was
    unanswerable from the report."""
    graph = Graph(mem, promotion_threshold=cfg["config"]["promotion_threshold"])
    artifact = verify_artifact(verify_scene["verify"]["id"],
                               verify_scene["hypothesis"], "supported")
    artifact["reasoning"] = "C-001 and C-002 both state the figure directly."
    apply.apply_verify(mem, graph, cfg, verify_scene["verify"]["id"],
                       verify_scene["verify"], artifact)
    assert mem.read(verify_scene["hypothesis"])["verdict_reasoning"] == (
        "C-001 and C-002 both state the figure directly.")


def test_a_hypothesis_with_no_verdict_reasoning_still_validates(mem):
    """schemas/hypothesis.json is additionalProperties: false, so a new
    field is only safe if it is OPTIONAL. Required, it would retroactively
    invalidate every hypothesis already on disk -- dropping each out of
    Graph.readable, rescoring it to 0 and quarantining its facts on the
    first submit after the upgrade, irreversibly, because promotion only
    happens when a fresh verify verdict lands. citation.json's
    page_sha256 description is that same mistake costed out in full.

    Absent means "no verdict has landed yet, or this node predates the
    field" -- exactly as absent `cascaded` means false in
    schemas/assumption.json."""
    hypothesis = mem.create("hypothesis", {
        "claim": "c", "supporting": [], "counter": [], "status": "proposed",
        "confidence": 0.0, "verdict": None,
        "provenance": {"task": None, "agent": "hypothesizer"},
    })
    assert "verdict_reasoning" not in hypothesis
    mem.validate(hypothesis)


def test_a_second_verdict_replaces_the_earlier_reasoning(mem, cfg,
                                                         verify_scene):
    """A hypothesis can be re-verified over a multi-day run. The stored
    prose has to describe the verdict currently on the node: Appendix A
    prints the two side by side, and a stale explanation of a decision
    that was since reversed is worse there than no explanation at all."""
    graph = Graph(mem, promotion_threshold=cfg["config"]["promotion_threshold"])
    first = verify_artifact(verify_scene["verify"]["id"],
                            verify_scene["hypothesis"], "supported")
    first["reasoning"] = "the quotes establish it"
    apply.apply_verify(mem, graph, cfg, verify_scene["verify"]["id"],
                       verify_scene["verify"], first)
    second = verify_artifact(verify_scene["verify"]["id"],
                             verify_scene["hypothesis"], "contradicted")
    second["reasoning"] = "C-003 shows the opposite"
    apply.apply_verify(mem, Graph(mem), cfg, verify_scene["verify"]["id"],
                       verify_scene["verify"], second)
    hypothesis = mem.read(verify_scene["hypothesis"])
    assert hypothesis["verdict"] == "contradicted"
    assert hypothesis["verdict_reasoning"] == "C-003 shows the opposite"


def test_reasoning_is_stored_even_when_the_verdict_does_not_move(
    mem, cfg, verify_scene
):
    """The write is guarded on `verdict != verdict or status != target`,
    which is the right guard for those two fields and the wrong one for
    this third. A re-verification reaching the same verdict by different
    reasoning would leave the old prose on the node while the journal
    recorded a new artifact -- the report then quoting an argument the
    verifier no longer made."""
    graph = Graph(mem, promotion_threshold=cfg["config"]["promotion_threshold"])
    first = verify_artifact(verify_scene["verify"]["id"],
                            verify_scene["hypothesis"], "supported")
    first["reasoning"] = "first pass reasoning"
    apply.apply_verify(mem, graph, cfg, verify_scene["verify"]["id"],
                       verify_scene["verify"], first)
    again = verify_artifact(verify_scene["verify"]["id"],
                            verify_scene["hypothesis"], "supported")
    again["reasoning"] = "second pass, same verdict, better argument"
    apply.apply_verify(mem, Graph(mem), cfg, verify_scene["verify"]["id"],
                       verify_scene["verify"], again)
    assert mem.read(verify_scene["hypothesis"])["verdict_reasoning"] == (
        "second pass, same verdict, better argument")


def test_supported_promotes_the_hypothesis(mem, cfg, verify_scene):
    """Hand-computed: 3 live verified citations on 3 distinct domains,
    verdict supported. volume = min(1, 3/3) = 1.0, breadth = 3/(3+1) =
    0.75, weight = 1.0, so 0.75 — comfortably above the 0.67 threshold,
    because a third independent source now pays where it used to be
    worth nothing.

    apply_verify is the only thing in the system that writes
    `status: supported`. recompute_confidence reconciles downward only,
    deliberately: promoting there re-promotes a hypothesis the cascade
    has just provenance-demoted."""
    run_verify(mem, cfg, verify_scene, "supported")
    hypothesis = mem.read(verify_scene["hypothesis"])
    assert hypothesis["verdict"] == "supported"
    assert hypothesis["status"] == "supported"
    Graph(mem).recompute_confidence()
    assert mem.read(verify_scene["hypothesis"])["confidence"] == 0.75
    assert mem.read(verify_scene["hypothesis"])["status"] == "supported"


def test_verified_status_uses_the_graphs_own_promotion_threshold(
    mem, cfg, verify_scene
):
    """Minor 7 (fix round 1), tested for the first time in fix round 2:
    _verified_status must read graph.promotion_threshold, not
    cfg["config"]["promotion_threshold"] -- every existing test's Graph
    and cfg agree on the threshold, so reverting that one word left all
    662 tests green. cfg here stays at the default 0.6, which the
    fixture's 3-domain evidence clears; the Graph is built at a
    deliberately mismatched 0.9, which it does not clear, so only
    reading graph.promotion_threshold demotes the verdict to
    `proposed`."""
    graph = Graph(mem, promotion_threshold=0.9)
    apply.apply_verify(
        mem, graph, cfg, verify_scene["verify"]["id"], verify_scene["verify"],
        verify_artifact(verify_scene["verify"]["id"], verify_scene["hypothesis"],
                        "supported"))
    hypothesis = mem.read(verify_scene["hypothesis"])
    assert hypothesis["verdict"] == "supported"
    assert hypothesis["status"] == "proposed"


def test_a_supported_verdict_below_the_threshold_does_not_promote(
    mem, cfg, verify_scene
):
    """The verifier's opinion is necessary for promotion, not sufficient.
    Hand-computed: rejecting two of three citations leaves one live
    citation on one domain — base = 1/3, spread = min(1, 1/2) = 0.5,
    weight = 1.0, so 0.17, well under 0.6."""
    run_verify(mem, cfg, verify_scene, "supported",
               failing=verify_scene["citations"][:2])
    hypothesis = mem.read(verify_scene["hypothesis"])
    assert hypothesis["verdict"] == "supported"
    assert hypothesis["status"] == "proposed"
    Graph(mem).recompute_confidence()
    assert mem.read(verify_scene["hypothesis"])["confidence"] == 0.17


def test_live_counter_evidence_makes_it_contested_not_supported(
    mem, cfg, verify_scene, mkcitation, mkfact
):
    """Spec section 4's digest counts "supported 14, contested 5", so the
    status has to be reachable. Counter-evidence that is itself live is
    the thing that distinguishes them."""
    counter = mkcitation(url="https://x-example.com/c", domain="x-example.com",
                         quote="the tail is dominated by GC, not cold starts")
    mkfact(statement="counter", citations=[counter["id"]],
           task=verify_scene["verify"]["id"])
    mem.update(verify_scene["hypothesis"], counter=[counter["id"]])
    # Six supporting citations, not the fixture's three: opposition is
    # weighed against the volume of support now, so a claim at exactly
    # the gate-3 minimum is demoted by one counter rather than contested
    # by it. `contested` means "absorbed the dispute and still cleared
    # the bar", and this test exists to prove that state is reachable.
    widen_support(mem, mkcitation, mkfact, verify_scene,
                  verify_scene["hypothesis"])
    run_verify(mem, cfg, verify_scene, "supported")
    assert mem.read(verify_scene["hypothesis"])["status"] == "contested"


def test_dead_counter_evidence_does_not_make_it_contested(
    mem, cfg, verify_scene, mkcitation, mkfact
):
    """A counter citation whose fact was quarantined is out of the
    report, so it should not hold a claim back either."""
    counter = mkcitation(url="https://x-example.com/c", domain="x-example.com",
                         quote="a disputed span", status="rejected")
    mkfact(statement="counter", citations=[counter["id"]],
           task=verify_scene["verify"]["id"], status="quarantined")
    mem.update(verify_scene["hypothesis"], counter=[counter["id"]])
    run_verify(mem, cfg, verify_scene, "supported")
    assert mem.read(verify_scene["hypothesis"])["status"] == "supported"


def test_a_counter_citation_named_as_failing_is_not_rejected(
    mem, cfg, verify_scene, mkcitation, mkfact
):
    """Critical 1 (fix round 1): the verifier's job is to judge the claim,
    not erase opposition. `failing_citations` is scoped to `supporting`;
    naming a live COUNTER citation there must not let the verifier
    silently reject its own opposition and promote past a real dispute.
    Measured before this fix: the counter citation was rejected and the
    hypothesis promoted to `supported` where `contested` was correct."""
    counter = mkcitation(url="https://x-example.com/c", domain="x-example.com",
                         quote="the tail is dominated by GC, not cold starts")
    mkfact(statement="counter", citations=[counter["id"]],
           task=verify_scene["verify"]["id"])
    mem.update(verify_scene["hypothesis"], counter=[counter["id"]])
    widen_support(mem, mkcitation, mkfact, verify_scene,
                  verify_scene["hypothesis"])
    result = run_verify(mem, cfg, verify_scene, "supported",
                        failing=[counter["id"]])
    assert mem.read(counter["id"])["status"] == "verified"
    assert counter["id"] not in result.rejected_citations
    assert mem.read(verify_scene["hypothesis"])["status"] == "contested"


def test_a_citation_already_on_both_sides_is_not_rejected_at_the_point_of_use(
    mem, cfg, mktask, mkcitation, mkfact, mkhypothesis
):
    """Important, at the point of use (fix round 3): apply_hypothesize's
    entry guard (fix round 2) stops a NEW hypothesis from listing one
    citation on both sides, but does nothing for a hypothesis already on
    disk with `supporting` and `counter` overlapping -- written before
    that guard existed, or by any other means. apply_verify's own
    failing_citations guard must refuse to reject such a citation too:
    it is `in supporting` (so the naive guard treats it as rejectable),
    and rejecting it drops it from live_citations, erasing it as
    counter-evidence and promoting a hypothesis the graph still records
    a dispute against. Measured before this fix: status=supported,
    counter=['C-001'], C-001 rejected."""
    root = mktask(question="root", kind="decompose")
    worker = mktask(question="w", kind="extract", parent=root["id"], depth=1)
    # Six, not three: the overlapping citation counts as live opposition
    # in the score now, and a claim at exactly the gate-3 minimum would
    # be demoted rather than contested. This test is about the
    # failing_citations guard, so the claim has to stay promoted for the
    # distinction it checks to be visible at all.
    citations = [mkcitation(url=f"https://d{i}-example.com/x",
                            domain=f"d{i}-example.com", quote=f"a quoted span {i}")
                 for i in range(6)]
    for index, citation in enumerate(citations):
        mkfact(statement=f"f{index}", citations=[citation["id"]],
               task=worker["id"])
    overlap = citations[0]["id"]
    hypothesis = mkhypothesis(
        claim="Cold starts dominate the tail",
        supporting=[c["id"] for c in citations],
        counter=[overlap], task=worker["id"])
    verify_task = mktask(question="verify", kind="verify", parent=root["id"],
                         depth=1)
    mem.update(verify_task["id"],
              inputs={"hypothesis": hypothesis["id"], "refutes": None})
    verify_task = mem.read(verify_task["id"])
    apply.apply_verify(
        mem, Graph(mem), cfg, verify_task["id"], verify_task,
        verify_artifact(verify_task["id"], hypothesis["id"], "supported",
                        failing=[overlap]))
    assert mem.read(overlap)["status"] == "verified"
    assert mem.read(hypothesis["id"])["status"] == "contested"


def test_a_provenance_demoted_hypothesis_is_not_re_promoted_by_a_recompute(
    mem, cfg, mktask, mkcitation, mkfact, mkhypothesis, mkassumption
):
    """The regression this design avoids, pinned here rather than left to
    plan 1's test to catch by accident: the cascade demotes a promoted
    hypothesis whose authoring reasoning is now unsound; a rescore must
    not undo that.

    Fix round 1: the original version of this test reused `verify_scene`,
    whose evidence is all provenanced under the cascaded branch's own
    subtree, so the cascade's own fact-quarantine pass alone drove
    confidence to 0.0 regardless of whether recompute_confidence also
    promotes -- vacuous. This version provenances the single supporting
    fact to a task OUTSIDE the cascaded branch's subtree, the way
    test_graph_cascade.py's dedicated test does, so the citations stay
    live through the cascade and only the provenance-demotion protection
    (not evidence decay) explains the assertions below."""
    root = mktask(question="root", kind="decompose")
    child = mktask(question="form claims", kind="hypothesize",
                   parent=root["id"], depth=1)
    survivor = mktask(question="unrelated")
    citations = [mkcitation(url=f"https://d{i}-example.com/x",
                            domain=f"d{i}-example.com", quote=f"a quoted span {i}")
                 for i in range(3)]
    mkfact(statement="clean", citations=[c["id"] for c in citations],
           task=survivor["id"])
    hypothesis = mkhypothesis(claim="Cold starts dominate the tail",
                              supporting=[c["id"] for c in citations],
                              task=child["id"])
    verify_task = mktask(question="verify", kind="verify", parent=child["id"],
                         depth=1)
    mem.update(verify_task["id"],
              inputs={"hypothesis": hypothesis["id"], "refutes": None})
    verify_task = mem.read(verify_task["id"])
    apply.apply_verify(
        mem, Graph(mem), cfg, verify_task["id"], verify_task,
        verify_artifact(verify_task["id"], hypothesis["id"], "supported"))
    assert mem.read(hypothesis["id"])["status"] == "supported"

    assumption = mkassumption(raised_by=root["id"], status="refuted")
    Graph(mem).cascade(assumption["id"])
    assert mem.read(hypothesis["id"])["status"] == "proposed"
    Graph(mem).recompute_confidence()
    assert mem.read(hypothesis["id"])["status"] == "proposed"


def test_unsupported_demotes_to_an_open_assumption(mem, cfg, verify_scene):
    """Spec section 6: 'demoted to an open assumption; task spawned for
    better evidence'. The hypothesis is not deleted — its verdict weight
    drops to 0.5, which puts it back to `proposed` on recompute."""
    result = run_verify(mem, cfg, verify_scene, "unsupported")
    assumption = next(a for a in mem.list("assumption")
                      if a["statement"] == "Cold starts dominate the tail")
    assert assumption["status"] == "open"
    assert assumption["blocks"] == [verify_scene["hypothesis"]]
    assert assumption["id"] in result.created


def test_unsupported_spawns_a_search_task_for_better_evidence(
    mem, cfg, verify_scene
):
    run_verify(mem, cfg, verify_scene, "unsupported")
    spawned = [t for t in mem.list("task")
               if t["kind"] == "search" and t["status"] == "pending"]
    assert len(spawned) == 1
    assert "Cold starts dominate the tail" in spawned[0]["question"]


def test_unsupported_leaves_the_hypothesis_below_the_threshold(
    mem, cfg, verify_scene
):
    """Hand-computed: weight(unsupported) = 0.5, so 0.6 * 1.0 * 0.5 = 0.3,
    under the 0.6 threshold, and recompute sets `proposed`."""
    run_verify(mem, cfg, verify_scene, "unsupported")
    Graph(mem).recompute_confidence()
    hypothesis = mem.read(verify_scene["hypothesis"])
    # 3 citations / 3 domains scores 0.75, and the `unsupported` weight
    # is 0.3 rather than 0.5: an adversarial rejection costs more than
    # never having been checked, which it did not before. 0.75 * 0.3.
    assert hypothesis["confidence"] == 0.22
    assert hypothesis["status"] == "proposed"


def test_contradicted_refutes_the_hypothesis(mem, cfg, verify_scene):
    run_verify(mem, cfg, verify_scene, "contradicted")
    hypothesis = mem.read(verify_scene["hypothesis"])
    assert hypothesis["status"] == "refuted"
    assert hypothesis["verdict"] == "contradicted"


def test_a_refuted_hypothesis_is_not_un_refuted_by_a_recompute(
    mem, cfg, verify_scene
):
    run_verify(mem, cfg, verify_scene, "contradicted")
    Graph(mem).recompute_confidence()
    assert mem.read(verify_scene["hypothesis"])["status"] == "refuted"


def test_failing_citations_are_rejected(mem, cfg, verify_scene):
    doomed = verify_scene["citations"][0]
    run_verify(mem, cfg, verify_scene, "supported", failing=[doomed])
    assert mem.read(doomed)["status"] == "rejected"


def test_rejecting_a_citation_lowers_the_score(mem, cfg, verify_scene):
    """Hand-computed: two live citations on two domains, verdict
    supported. volume = min(1, 2/3) = 0.667, breadth = 2/(2+1) = 0.667,
    so 0.44 — below the 0.67 threshold, so the hypothesis lands on
    `proposed`. This is what makes failing_citations more than a
    comment.

    Both terms bite here, and that is the point of having two: rejecting
    a citation drops the claim under gate 3's citation bar AND under its
    domain count, and the score has to notice both."""
    run_verify(mem, cfg, verify_scene, "supported",
               failing=[verify_scene["citations"][0]])
    Graph(mem).recompute_confidence()
    hypothesis = mem.read(verify_scene["hypothesis"])
    assert hypothesis["confidence"] == 0.44
    assert hypothesis["status"] == "proposed"


def test_a_failing_citation_the_verifier_was_never_given_is_an_apply_error(
    mem, cfg, verify_scene, mkcitation
):
    """The verifier's input packet is the claim and its quotes, nothing
    else. Any other id is invented."""
    stranger = mkcitation(url="https://z-example.com/x", domain="z-example.com",
                          quote="an unrelated span")
    with pytest.raises(apply.ApplyError, match=stranger["id"]):
        run_verify(mem, cfg, verify_scene, "supported",
                   failing=[stranger["id"]])


def test_an_artifact_naming_a_different_hypothesis_is_an_apply_error(
    mem, cfg, verify_scene, mkhypothesis
):
    other = mkhypothesis(claim="something else", supporting=["C-001"])
    with pytest.raises(apply.ApplyError, match=other["id"]):
        apply.apply_verify(
            mem, Graph(mem), cfg, verify_scene["verify"]["id"],
            verify_scene["verify"],
            verify_artifact(verify_scene["verify"]["id"], other["id"],
                            "supported"))


def test_a_verify_task_with_no_hypothesis_in_inputs_is_an_apply_error(
    mem, cfg, mktask, mkhypothesis
):
    hypothesis = mkhypothesis(supporting=["C-001"])
    task = mktask(question="verify", kind="verify")
    with pytest.raises(apply.ApplyError, match="inputs"):
        apply.apply_verify(
            mem, Graph(mem), cfg, task["id"], task,
            verify_artifact(task["id"], hypothesis["id"], "supported"))


def test_applying_the_same_verify_twice_converges(mem, cfg, verify_scene):
    run_verify(mem, cfg, verify_scene, "unsupported")
    before = mem.all_ids()
    run_verify(mem, cfg, verify_scene, "unsupported")
    assert mem.all_ids() == before


# --- verify: contradicted runs the cascade ---------------------------

def test_contradicted_refutes_the_named_assumption_and_cascades(
    mem, cfg, evidence_scene, mkassumption, mktask
):
    """The full chain spec section 2 describes: A-003 refuted by H-009,
    everything downstream staled and quarantined. Code decides what is
    invalidated; the agents only proposed the link."""
    assumption = mkassumption(raised_by=evidence_scene["root"]["id"])
    downstream = mktask(question="rested on it", kind="search",
                        parent=evidence_scene["root"]["id"], depth=1,
                        status="done")
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"],
                             evidence_scene["citations"],
                             refutes=assumption["id"]))
    verify_task = next(t for t in mem.list("task") if t["kind"] == "verify")
    hypothesis_id = mem.ids("hypothesis")[0]
    result = apply.apply_verify(
        mem, Graph(mem), cfg, verify_task["id"], verify_task,
        verify_artifact(verify_task["id"], hypothesis_id, "contradicted"))

    refuted = mem.read(assumption["id"])
    assert refuted["status"] == "refuted"
    assert refuted["refuted_by"] == hypothesis_id
    assert result.cascaded == [assumption["id"]]
    # Refuted by apply_verify, cascaded by submit — after every artifact
    # in the tick has landed. Nothing is staled until then.
    assert mem.read(downstream["id"])["status"] == "done"
    apply.run_cascades(Graph(mem), result.cascaded)
    assert mem.read(downstream["id"])["status"] == "stale"


def test_run_cascades_skips_an_assumption_that_is_no_longer_refuted(
    mem, cfg, mkassumption, mktask
):
    """submit's recovery path re-applies artifacts, and Graph.cascade
    raises on an assumption that is not refuted. A second run must be a
    no-op, not a crash."""
    root = mktask(question="root", kind="decompose")
    open_one = mkassumption(raised_by=root["id"], status="open")
    assert apply.run_cascades(Graph(mem), [open_one["id"], "A-404"]) == []


def test_run_cascades_skips_an_unparseable_assumption(
    mem, cfg, mktask, mkassumption
):
    """Important 4, guard family (fix round 2): run_cascades's own read
    caught only KeyError. An assumption id that exists but is
    unparseable raises nodes.NodeFormatError instead, and must be
    skipped the same way index_of and Graph._readable already do."""
    root = mktask(question="root", kind="decompose")
    assumption = mkassumption(raised_by=root["id"], status="refuted")
    _corrupt(mem, assumption["id"])
    assert apply.run_cascades(Graph(mem), [assumption["id"]]) == []


def test_run_cascades_skips_a_refuted_but_schema_invalid_assumption(
    mem, cfg, mktask, mkassumption
):
    """New Important (fix round 2): the marker write at the end of a
    cascade always re-validates the WHOLE merged record. An assumption
    that parses and would cascade fine but is schema-invalid for an
    unrelated reason (here, `refuted_by` failing its own pattern) must
    not have the cascade start at all -- otherwise the stale/quarantine
    writes commit, the marker write raises ValidationError, and every
    retry re-runs the cascade (re-committing the same writes) and
    re-raises on the same line: wedged, not convergent. Skipping before
    cascading means nothing commits and nothing needs undoing."""
    root = mktask(question="root", kind="decompose")
    downstream = mktask(question="rested on it", kind="search",
                        parent=root["id"], depth=1, status="done")
    assumption = mkassumption(raised_by=root["id"], status="refuted")
    _set_invalid_field(mem, assumption["id"], "refuted_by", "H-1")
    # Must not raise, and must not partially apply the cascade.
    assert apply.run_cascades(Graph(mem), [assumption["id"]]) == []
    assert mem.read(downstream["id"])["status"] == "done"


def test_run_cascades_is_ordered_and_deduplicated(mem, cfg, mkassumption,
                                                   mktask):
    root = mktask(question="root", kind="decompose")
    first = mkassumption(statement="a", raised_by=root["id"],
                         status="refuted")
    second = mkassumption(statement="b", raised_by=root["id"],
                          status="refuted")
    ran = apply.run_cascades(
        Graph(mem), [second["id"], first["id"], first["id"]])
    assert [assumption_id for assumption_id, _ in ran] == [
        first["id"], second["id"]]


def test_a_contradicted_verdict_with_no_refutes_runs_no_cascade(
    mem, cfg, verify_scene
):
    """Not every refuted claim invalidates an assumption. Cascading on a
    bare `contradicted` would stale the whole branch every time a
    hypothesis failed."""
    result = run_verify(mem, cfg, verify_scene, "contradicted")
    assert result.cascaded == []


def test_the_cascade_is_not_re_run_on_an_already_refuted_assumption(
    mem, cfg, evidence_scene, mkassumption
):
    """Graph.cascade raises on an assumption that is not `refuted`, and
    submit's recovery path re-applies artifacts. Re-running must be a
    no-op, not a raise."""
    assumption = mkassumption(raised_by=evidence_scene["root"]["id"])
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"],
                             evidence_scene["citations"],
                             refutes=assumption["id"]))
    verify_task = next(t for t in mem.list("task") if t["kind"] == "verify")
    artifact = verify_artifact(verify_task["id"], mem.ids("hypothesis")[0],
                               "contradicted")
    apply.apply_verify(mem, Graph(mem), cfg, verify_task["id"], verify_task,
                       artifact)
    apply.apply_verify(mem, Graph(mem), cfg, verify_task["id"], verify_task,
                       artifact)  # must not raise
    assert mem.read(assumption["id"])["status"] == "refuted"


def test_run_cascades_invalidates_a_pre_warmed_cache(
    mem, cfg, mktask, mkassumption
):
    """Mutation-guards run_cascades's own graph.invalidate_cache() call. A
    caller may hand it a Graph whose `.tasks` cache was already warmed
    (e.g. an earlier frontier() call in the same tick) before the task
    this cascade needs to stale was marked `done`. Without the call,
    cascade() reads that pre-`done` snapshot and the completed task
    escapes staling."""
    root = mktask(question="root", kind="decompose")
    downstream = mktask(question="rested on it", kind="search",
                        parent=root["id"], depth=1, status="pending")
    assumption = mkassumption(raised_by=root["id"], status="refuted")
    graph = Graph(mem)
    graph.tasks  # warm the cache while downstream is still `pending`
    mem.update(downstream["id"], status="done")
    apply.run_cascades(graph, [assumption["id"]])
    assert mem.read(downstream["id"])["status"] == "stale"


def test_a_crash_before_the_cascade_recovers_and_stales_on_re_apply(
    mem, cfg, evidence_scene, mkassumption, mktask
):
    """Critical 3 (fix round 1): 'refuted' alone does not say whether the
    cascade for it has run. Simulates the crash window between
    apply_verify's write and submit's run_cascades call: re-applying the
    SAME verify artifact (submit's recovery path) after the assumption is
    already `refuted` must still report it for cascading, because the
    cascade itself never ran. Measured before this fix: the recovery
    re-apply took the "already refuted, cascade already ran" branch and
    reported `cascaded == []`, so run_cascades was never even asked, and
    downstream stayed `done` on a premise known false."""
    assumption = mkassumption(raised_by=evidence_scene["root"]["id"])
    downstream = mktask(question="rested on it", kind="search",
                        parent=evidence_scene["root"]["id"], depth=1,
                        status="done")
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"],
                             evidence_scene["citations"],
                             refutes=assumption["id"]))
    verify_task = next(t for t in mem.list("task") if t["kind"] == "verify")
    hypothesis_id = mem.ids("hypothesis")[0]
    artifact = verify_artifact(verify_task["id"], hypothesis_id, "contradicted")
    first = apply.apply_verify(mem, Graph(mem), cfg, verify_task["id"],
                               verify_task, artifact)
    assert first.cascaded == [assumption["id"]]
    # The crash lands here: run_cascades is never called for `first`.
    assert mem.read(assumption["id"]).get("cascaded") is not True
    assert mem.read(downstream["id"])["status"] == "done"

    # Recovery re-applies the same verify artifact.
    recovered = apply.apply_verify(mem, Graph(mem), cfg, verify_task["id"],
                                   verify_task, artifact)
    assert recovered.cascaded == [assumption["id"]]
    apply.run_cascades(Graph(mem), recovered.cascaded)
    assert mem.read(downstream["id"])["status"] == "stale"
    assert mem.read(assumption["id"])["cascaded"] is True


def test_a_crash_after_the_cascade_does_not_re_stale_on_re_apply(
    mem, cfg, evidence_scene, mkassumption, mktask
):
    """Critical 3 (fix round 1): the opposite direction. Once run_cascades
    has actually run, a recovery re-apply of the same verify artifact must
    not report the assumption again -- otherwise a later, legitimately
    `done` task would be re-staled on top of real, current work. The
    existing "do not re-stale completed work" behaviour must survive."""
    assumption = mkassumption(raised_by=evidence_scene["root"]["id"])
    downstream = mktask(question="rested on it", kind="search",
                        parent=evidence_scene["root"]["id"], depth=1,
                        status="done")
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"],
                             evidence_scene["citations"],
                             refutes=assumption["id"]))
    verify_task = next(t for t in mem.list("task") if t["kind"] == "verify")
    hypothesis_id = mem.ids("hypothesis")[0]
    artifact = verify_artifact(verify_task["id"], hypothesis_id, "contradicted")
    result = apply.apply_verify(mem, Graph(mem), cfg, verify_task["id"],
                                verify_task, artifact)
    apply.run_cascades(Graph(mem), result.cascaded)
    assert mem.read(downstream["id"])["status"] == "stale"
    assert mem.read(assumption["id"])["cascaded"] is True

    # A later tick legitimately redoes and completes the staled work.
    mem.update(downstream["id"], status="done")

    recovered = apply.apply_verify(mem, Graph(mem), cfg, verify_task["id"],
                                   verify_task, artifact)
    assert recovered.cascaded == []
    apply.run_cascades(Graph(mem), [assumption["id"]])
    assert mem.read(downstream["id"])["status"] == "done"


# --- a claim accumulates evidence instead of forking ------------------
#
# HYPOTHESIS_KEY is (provenance.task, claim) and apply_hypothesize builds
# its candidate with the CURRENT task's id, while ensure_hypothesize_tasks
# deliberately varies each round's question by fact count so two rounds
# are different task keys. So the key could never match an earlier node
# and the reuse branch was unreachable across rounds. Measured before the
# fix:
#
#   round 1 created: ['H-001']
#   round 2 created: ['H-002'] reused: []
#   H-001 | THE SAME CLAIM | supporting: ['C-001']
#   H-002 | THE SAME CLAIM | supporting: ['C-001','C-002','C-003']
#
# Linear in rounds, and plan 8 added a third multiplier: three rounds gave
# 3 nodes, 3 verify tasks and 3 refute searches for one claim.

CLAIM = "Rayleigh scattering explains the blue sky"


def _round(mem, cfg, branch_task, supporting, counter=(), claim=CLAIM,
           label=None):
    """One hypothesize round on `branch_task`, as the scheduler seeds it.

    The question carries a distinguishing label because
    ensure_hypothesize_tasks puts the fact count in it, which is what
    makes each round a new task — the precondition for the whole defect.
    """
    hypothesize = mem.create("task", {
        "question": f"Form candidate claims from the {label} facts gathered "
                    f"under: theme",
        "kind": "hypothesize", "parent": branch_task, "depth": 1,
        "status": "running", "depends_on": [], "attempts": 0, "inputs": {},
        "provenance": {"task": None, "agent": "scheduler"}})["id"]
    return apply.apply_hypothesize(
        mem, Graph(mem), cfg, hypothesize, mem.read(hypothesize),
        {"task_id": hypothesize, "no_hypotheses_reason": None,
         "hypotheses": [{"claim": claim, "supporting": list(supporting),
                         "counter": list(counter), "refutes": None}]})


@pytest.fixture
def branch_scene(mem, mktask, mkcitation, mkfact):
    """A branch with six citations, ready to be hypothesized over twice."""
    root = mktask(question="root", kind="decompose", status="done")["id"]
    branch = mktask(question="theme", kind="search", parent=root, depth=1,
                    status="done")["id"]
    citations = []
    for index in range(6):
        citation = mkcitation(url=f"https://d{index}-example.com/x",
                              domain=f"d{index}-example.com",
                              quote=f"a quoted span number {index}")["id"]
        mkfact(statement=f"f{index}", citations=[citation], task=branch)
        citations.append(citation)
    return {"root": root, "branch": branch, "citations": citations}


def test_a_re_proposed_claim_merges_into_the_existing_node(
    mem, cfg, branch_scene
):
    """The root cause. Two rounds on one branch, one claim, one node."""
    first = _round(mem, cfg, branch_scene["branch"],
                   branch_scene["citations"][:1], label=1)
    second = _round(mem, cfg, branch_scene["branch"],
                    branch_scene["citations"][:3], label=3)
    assert len(mem.ids("hypothesis")) == 1
    assert second.created == [] or all(not h.startswith("H-")
                                       for h in second.created)
    assert mem.ids("hypothesis")[0] in second.reused


def test_merging_unions_the_supporting_citations(mem, cfg, branch_scene):
    """Not "latest wins": a round can legitimately cite a subset, and
    dropping the rest would undo evidence already gathered."""
    _round(mem, cfg, branch_scene["branch"],
           branch_scene["citations"][:1], label=1)
    _round(mem, cfg, branch_scene["branch"],
           branch_scene["citations"][1:3], label=3)
    hypothesis = mem.read(mem.ids("hypothesis")[0])
    assert hypothesis["supporting"] == sorted(branch_scene["citations"][:3])


def test_merging_unions_the_counter_citations(mem, cfg, branch_scene):
    """A challenge found on day two must survive day three's round.
    apply._attach_counter_evidence writes these directly, so a merge that
    dropped them would erase a refutation the run paid a full search ->
    extract -> recheck cycle for."""
    _round(mem, cfg, branch_scene["branch"], branch_scene["citations"][:1],
           counter=[branch_scene["citations"][5]], label=1)
    _round(mem, cfg, branch_scene["branch"], branch_scene["citations"][:3],
           label=3)
    hypothesis = mem.read(mem.ids("hypothesis")[0])
    assert hypothesis["counter"] == [branch_scene["citations"][5]]


def test_the_same_claim_anywhere_in_the_run_is_one_node(
    mem, cfg, branch_scene, mktask, mkcitation, mkfact
):
    """INVERTED, and the reason matters more than the assertion.

    Plan 9 keyed the merge on (theme, claim) to stop two themes' identical
    sentences colliding, and argued for it. That was wrong about this
    architecture, and the integration run proved it: on a real run the
    key is (the round's own task, claim) — because
    ensure_hypothesize_tasks parents every round on root_branch, the
    constant function, so theme_of resolves each round to itself — and
    it never matches across rounds. Nine nodes for one claim.

    There is no per-theme hypothesizing to protect. scheduler.agent_input
    hands the hypothesizer every active fact in the run (_branch_of ->
    root_branch again), and ensure_hypothesize_tasks schedules one round
    at a time run-wide. One lineage, one claim namespace: the same
    sentence IS the same finding, wherever its evidence came from. And
    since outline now files a claim by its EVIDENCE rather than by the
    round that proposed it, one node spanning two themes lands in the
    dominant one instead of being impossible to place."""
    other = mktask(question="another theme", kind="search",
                   parent=branch_scene["root"], depth=1, status="done")["id"]
    citation = mkcitation(url="https://other-example.com/x",
                          domain="other-example.com",
                          quote="a span from the other theme")["id"]
    mkfact(statement="other", citations=[citation], task=other)
    _round(mem, cfg, branch_scene["branch"], branch_scene["citations"][:1],
           label=1)
    _round(mem, cfg, other, [citation], label=1)
    assert len(mem.ids("hypothesis")) == 1


def test_a_merge_that_would_put_one_citation_on_both_sides_is_refused(
    mem, cfg, branch_scene
):
    """Reachable for the first time only after a union: the existing
    guard runs on the artifact item alone, and each round here is
    internally coherent. A citation on both sides makes _verified_status
    see live counter-evidence that is also the claim's own support."""
    shared = branch_scene["citations"][0]
    _round(mem, cfg, branch_scene["branch"], [shared], label=1)
    with pytest.raises(apply.ApplyError, match="both supporting and counter"):
        _round(mem, cfg, branch_scene["branch"],
               branch_scene["citations"][1:3], counter=[shared], label=3)


def test_merging_does_not_promote(mem, cfg, branch_scene):
    """recompute_confidence is demote-only by design — _verified_status's
    docstring records that making it promote re-promotes a hypothesis the
    cascade has just provenance-demoted. A merge must not smuggle
    promotion in through the back door; the fresh verify task each round
    spawns is what promotes."""
    _round(mem, cfg, branch_scene["branch"], branch_scene["citations"][:1],
           label=1)
    _round(mem, cfg, branch_scene["branch"], branch_scene["citations"][:3],
           label=3)
    Graph(mem, promotion_threshold=cfg["config"]["promotion_threshold"]
          ).recompute_confidence()
    assert mem.read(mem.ids("hypothesis")[0])["status"] == "proposed"


def test_the_merged_node_keeps_its_original_provenance(mem, cfg,
                                                       branch_scene):
    """The node records who FIRST proposed the claim. Rewriting it to the
    latest round would move the claim between sections mid-run, because
    outline derives a section from the provenance branch."""
    first = _round(mem, cfg, branch_scene["branch"],
                   branch_scene["citations"][:1], label=1)
    before = mem.read(mem.ids("hypothesis")[0])["provenance"]["task"]
    _round(mem, cfg, branch_scene["branch"], branch_scene["citations"][:3],
           label=3)
    assert mem.read(mem.ids("hypothesis")[0])["provenance"]["task"] == before


def test_one_claim_over_three_rounds_is_one_node(mem, cfg, branch_scene):
    """The regression guard for the measured table. Before the fix: 3
    hypothesis nodes, 3 verify tasks and — since plan 8 — 3 refute
    searches, all for one claim, each adversarially verified and
    challenged separately and all three reaching the report.

    A fresh verify task per round is KEPT, and is not the same defect.
    Merging raises a claim's evidence, and `recompute_confidence` is
    demote-only, so nothing re-promotes a claim except a fresh verdict —
    without one, a claim merged up to three good citations would sit at
    `proposed` for ever. What matters is that all three now adjudicate
    the SAME node rather than three separate ones."""
    for count, upto in ((1, 1), (3, 3), (5, 5)):
        _round(mem, cfg, branch_scene["branch"],
               branch_scene["citations"][:upto], label=count)
    assert len(mem.ids("hypothesis")) == 1
    targets = {t["inputs"]["hypothesis"] for t in mem.list("task")
               if t["kind"] == "verify"}
    assert targets == {mem.ids("hypothesis")[0]}


# --- looking for disconfirmation --------------------------------------
#
# ensure_evidence_tasks is the only other hypothesis-driven search in the
# system and it fires only when gate 3 FAILS, asking for more support.
# Nothing had ever gone looking for the opposite, so `supported` meant
# "three quotes nobody sought the contrary of". Gate 4 weighs counter
# evidence now, but only counter evidence that turned up by accident.

def _promoted(mem, cfg, evidence_scene, status="supported"):
    """A hypothesis in a promoted state, the way apply_verify leaves one."""
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"],
                             evidence_scene["citations"]))
    hypothesis_id = mem.ids("hypothesis")[0]
    mem.update(hypothesis_id, status=status, verdict="supported",
               confidence=0.6)
    return hypothesis_id


def _refute_tasks(mem):
    return [t for t in mem.list("task")
            if (t.get("inputs") or {}).get("stance") == "against"]


def test_a_promoted_claim_gets_a_refute_search(mem, cfg, evidence_scene):
    """The whole point of this change."""
    hypothesis_id = _promoted(mem, cfg, evidence_scene)
    result = apply.ensure_refute_tasks(mem, Graph(mem), cfg)
    tasks = _refute_tasks(mem)
    assert len(tasks) == 1
    assert tasks[0]["kind"] == "search"
    assert tasks[0]["inputs"]["for_hypothesis"] == hypothesis_id
    assert tasks[0]["id"] in result.spawned


def test_a_contested_claim_gets_a_refute_search(mem, cfg, evidence_scene):
    """`contested` is promoted too — it reaches the report body, so the
    report stands behind it and it has to have been challenged."""
    _promoted(mem, cfg, evidence_scene, status="contested")
    apply.ensure_refute_tasks(mem, Graph(mem), cfg)
    assert len(_refute_tasks(mem)) == 1


def test_an_unpromoted_claim_gets_no_refute_search(mem, cfg, evidence_scene):
    """Nothing in the report rests on a `proposed` claim, and a challenge
    costs a full search -> extract -> recheck cycle. Scoping to promoted
    claims is what keeps the added spend bounded."""
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"],
                             evidence_scene["citations"]))
    apply.ensure_refute_tasks(mem, Graph(mem), cfg)
    assert _refute_tasks(mem) == []


def test_a_refuted_claim_gets_no_refute_search(mem, cfg, evidence_scene):
    """Already disproven. There is nothing left to disconfirm, and
    searching anyway is the unbounded-breadth failure gate 3's own
    refuted check exists to avoid."""
    _promoted(mem, cfg, evidence_scene)
    mem.update(mem.ids("hypothesis")[0], status="refuted")
    apply.ensure_refute_tasks(mem, Graph(mem), cfg)
    assert _refute_tasks(mem) == []


def test_the_refute_question_is_written_by_code_not_by_a_model(
    mem, cfg, evidence_scene
):
    """Spec section 4: the model is never the scheduler. Which claim gets
    attacked, and the fact that it is being attacked rather than
    supported, are both scheduling decisions."""
    _promoted(mem, cfg, evidence_scene)
    apply.ensure_refute_tasks(mem, Graph(mem), cfg)
    question = _refute_tasks(mem)[0]["question"]
    assert "false" in question.lower()
    assert "Rayleigh" in question or evidence_scene is not None


def test_a_second_refute_search_is_not_spawned_while_one_is_open(
    mem, cfg, evidence_scene
):
    """Called once per submit for the whole run. Without the open-task
    check this adds one task per promoted claim per tick, forever."""
    _promoted(mem, cfg, evidence_scene)
    apply.ensure_refute_tasks(mem, Graph(mem), cfg)
    apply.ensure_refute_tasks(mem, Graph(mem), cfg)
    assert len(_refute_tasks(mem)) == 1


def test_a_refute_search_and_an_evidence_search_can_be_open_together(
    mem, cfg, evidence_scene, mkcitation, mkfact
):
    """Both carry inputs.for_hypothesis, and ensure_evidence_tasks skips
    a hypothesis that has ANY open task with that tag. Undiscriminated,
    whichever opened first would suppress the other for the rest of the
    run — and since a refute search only exists for a PROMOTED claim,
    which by definition cleared gate 3, the starvation would run the
    other way once a cascade or a rejected citation reopened the gap.

    Contrived on purpose: a claim that is promoted AND short on evidence
    is reachable in a real run, because promotion and gate 3 are
    evaluated at different moments and a re-check landing later can
    reject a citation the promotion counted."""
    hypothesis_id = _promoted(mem, cfg, evidence_scene)
    # Knock its evidence out from under it without demoting it, which is
    # exactly what a late gate-2 rejection does before recompute runs.
    for citation_id in evidence_scene["citations"][1:]:
        mem.update(citation_id, status="rejected")
    apply.ensure_refute_tasks(mem, Graph(mem), cfg)
    apply.ensure_evidence_tasks(mem, Graph(mem), cfg)
    tagged = [t for t in mem.list("task")
              if (t.get("inputs") or {}).get("for_hypothesis") == hypothesis_id]
    stances = sorted((t["inputs"].get("stance") or "for") for t in tagged)
    assert stances == ["against", "for"], tagged


# --- refute evidence attaches to the claim it was gathered against ----
#
# There is no other mechanism that attaches a citation to an EXISTING
# hypothesis. The hypothesizer is the only writer of supporting/counter
# and it only ever writes them at creation; on reuse it appends to
# result.reused and leaves the node alone. Worse, HYPOTHESIS_KEY is
# (provenance.task, claim) and ensure_hypothesize_tasks deliberately puts
# the fact count in its question so two rounds are different task keys --
# so a re-proposed identical claim writes a DUPLICATE node rather than
# strengthening the original. That defect is pre-existing and out of
# scope here; this direct path is what routes around it.

def _refute_extract_scene(mem, cfg, evidence_scene, mkcitation):
    """A promoted claim, a refute search open on it, and the extract task
    that search spawned."""
    hypothesis_id = _promoted(mem, cfg, evidence_scene)
    apply.ensure_refute_tasks(mem, Graph(mem), cfg)
    search = _refute_tasks(mem)[0]
    result = apply.apply_search(
        mem, Graph(mem), cfg, search["id"], search,
        {"task_id": search["id"], "no_sources_reason": None,
         "queries": ["evidence against the p99 claim"],
         "sources": [{"url": "https://against-example.com/p",
                      "title": "a contrary measurement", "relevance": 0.9,
                      "why": "reports the opposite result"}]})
    extract_id = result.spawned[0]
    return hypothesis_id, extract_id


def test_a_refute_search_propagates_its_stance_to_its_extract_tasks(
    mem, cfg, evidence_scene, mkcitation
):
    """apply_extract has to know the citations it is about to create are
    counter-evidence, and the extract task is where it can be told.
    Carried explicitly rather than walked up to the parent: a
    self-contained task record is this codebase's convention, and
    TASK_KEY hashes inputs so an "against" extract of a URL is a distinct
    task from a "for" extract of the same URL."""
    hypothesis_id, extract_id = _refute_extract_scene(
        mem, cfg, evidence_scene, mkcitation)
    inputs = mem.read(extract_id)["inputs"]
    assert inputs["stance"] == "against"
    assert inputs["for_hypothesis"] == hypothesis_id


def test_a_refute_extract_attaches_its_citations_to_counter(
    mem, cfg, evidence_scene, mkcitation
):
    """The payoff. Without this the citations land in the branch and the
    only thing that could ever notice them is a hypothesizer, which would
    write a new claim rather than attach to this one."""
    hypothesis_id, extract_id = _refute_extract_scene(
        mem, cfg, evidence_scene, mkcitation)
    apply.apply_extract(
        mem, Graph(mem), cfg, extract_id, mem.read(extract_id),
        {"task_id": extract_id, "url": "https://against-example.com/p",
         "published_at": None,
         "source_type": "primary",
         "no_facts_reason": None,
         "facts": [{"statement": "the effect does not hold",
                    "quote": "we measured no such effect at all"}]})
    hypothesis = mem.read(hypothesis_id)
    assert len(hypothesis["counter"]) == 1
    citation = mem.read(hypothesis["counter"][0])
    assert citation["url"] == "https://against-example.com/p"


def test_an_ordinary_extract_attaches_nothing_to_any_hypothesis(
    mem, cfg, evidence_scene
):
    """Guards the guard. The attach path must be reachable only from a
    refute search, or every extraction in the run would start writing
    counter-evidence onto whatever claim happened to be tagged."""
    hypothesis_id = _promoted(mem, cfg, evidence_scene)
    before = mem.read(hypothesis_id)["counter"]
    extractor = mem.create("task", {
        "question": "read it", "kind": "extract", "parent": None, "depth": 1,
        "status": "running", "depends_on": [], "attempts": 0,
        "inputs": {"url": "https://plain-example.com/p", "title": "t"},
        "provenance": {"task": None, "agent": "searcher"}})["id"]
    apply.apply_extract(
        mem, Graph(mem), cfg, extractor, mem.read(extractor),
        {"task_id": extractor, "url": "https://plain-example.com/p",
         "published_at": None,
         "source_type": "primary",
         "no_facts_reason": None,
         "facts": [{"statement": "something", "quote": "a quoted span here"}]})
    assert mem.read(hypothesis_id)["counter"] == before


def test_a_citation_already_supporting_is_not_also_added_to_counter(
    mem, cfg, evidence_scene, mkcitation
):
    """apply_hypothesize rejects an artifact putting one id on both sides
    as incoherent, and apply_verify re-checks it at the point of use
    because a hypothesis already on disk can carry the overlap. This new
    writer has to honour the same rule: one quote on two sides is not a
    dispute, it is a contradiction in the record, and it would make
    _verified_status see live counter-evidence that is also its own
    support."""
    hypothesis_id, extract_id = _refute_extract_scene(
        mem, cfg, evidence_scene, mkcitation)
    artifact = {"task_id": extract_id, "url": "https://against-example.com/p",
                "published_at": None,
                "source_type": "primary",
                "no_facts_reason": None,
                "facts": [{"statement": "the effect does not hold",
                           "quote": "we measured no such effect at all"}]}
    apply.apply_extract(mem, Graph(mem), cfg, extract_id,
                        mem.read(extract_id), artifact)
    citation_id = mem.read(hypothesis_id)["counter"][0]
    # Force the overlap the guard exists for.
    mem.update(hypothesis_id, supporting=sorted(
        set(mem.read(hypothesis_id)["supporting"]) | {citation_id}),
        counter=[])
    result = apply.apply_extract(mem, Graph(mem), cfg, extract_id,
                                 mem.read(extract_id), artifact)
    assert mem.read(hypothesis_id)["counter"] == []
    assert any("already supports" in str(reason)
               for _, reason in result.dropped), result.dropped


def test_a_successful_refutation_takes_a_thin_claim_out_of_the_findings(
    mem, cfg, evidence_scene, mkcitation, mkfact
):
    """End to end, and the reason the whole plan is worth doing.
    Graph.recompute_confidence already re-evaluates supported ->
    contested whenever a counter citation is live; nothing here builds
    that. What this change does is finally make such a citation exist."""
    hypothesis_id, extract_id = _refute_extract_scene(
        mem, cfg, evidence_scene, mkcitation)
    apply.apply_extract(
        mem, Graph(mem), cfg, extract_id, mem.read(extract_id),
        {"task_id": extract_id, "url": "https://against-example.com/p",
         "published_at": None,
         "source_type": "primary",
         "no_facts_reason": None,
         "facts": [{"statement": "the effect does not hold",
                    "quote": "we measured no such effect at all"}]})
    # The counter citation has to be LIVE: verified by gate 2, and cited
    # by an active fact. Both, or live_citations excludes it.
    counter_id = mem.read(hypothesis_id)["counter"][0]
    mem.update(counter_id, status="verified")
    assert mem.read(hypothesis_id)["status"] == "supported"
    Graph(mem, promotion_threshold=cfg["config"]["promotion_threshold"]
          ).recompute_confidence()
    # `proposed`, not `contested`, and that is a strengthening of what
    # this test originally asserted. The claim carried exactly the
    # gate-3 minimum, and opposition is weighed against the volume of
    # support now — so one live counter takes a minimally-evidenced
    # claim out of the findings entirely rather than leaving it in them
    # with a badge. `contested` is for a claim strong enough to absorb
    # the dispute and still clear the bar; that path is covered by
    # test_live_counter_evidence_makes_it_contested_not_supported.
    assert mem.read(hypothesis_id)["status"] == "proposed"
    assert mem.read(hypothesis_id)["confidence"] < 0.6


# --- counter-evidence re-opens the adversarial question ---------------
#
# Gate 4 became adjudicative in plan 8 — the verify packet labels every
# quote `supporting` or `counter`. But the verifier only ever saw a claim
# once, when the hypothesizer first proposed it. _attach_counter_evidence
# wrote the counter list and spawned nothing, so a refute search could
# gather fifteen contradicting sources and the one agent that could act
# on them was never dispatched. There was an incidental route — the
# refute facts trigger a hypothesize round, which re-proposes the claim,
# which merges and seeds a verify — but it depended on the hypothesizer
# choosing to restate that particular claim.

def _verify_tasks_for(mem, hypothesis_id):
    return [t for t in mem.list("task")
            if t["kind"] == "verify"
            and (t.get("inputs") or {}).get("hypothesis") == hypothesis_id]


def test_attaching_counter_evidence_seeds_a_fresh_verification(
    mem, cfg, evidence_scene, mkcitation
):
    hypothesis_id, extract_id = _refute_extract_scene(
        mem, cfg, evidence_scene, mkcitation)
    for task in _verify_tasks_for(mem, hypothesis_id):
        mem.update(task["id"], status="done")
    before = len(_verify_tasks_for(mem, hypothesis_id))
    apply.apply_extract(
        mem, Graph(mem), cfg, extract_id, mem.read(extract_id),
        {"task_id": extract_id, "url": "https://against-example.com/p",
         "published_at": None,
         "source_type": "primary", "no_facts_reason": None,
         "facts": [{"statement": "the effect does not hold",
                    "quote": "we measured no such effect at all"}]})
    assert len(_verify_tasks_for(mem, hypothesis_id)) == before + 1


def test_a_second_attachment_does_not_pile_up_verify_tasks(
    mem, cfg, evidence_scene, mkcitation
):
    """Each is a real subagent call, and a page yielding five quotes
    would otherwise seed five."""
    hypothesis_id, extract_id = _refute_extract_scene(
        mem, cfg, evidence_scene, mkcitation)
    for task in _verify_tasks_for(mem, hypothesis_id):
        mem.update(task["id"], status="done")
    artifact = {"task_id": extract_id, "url": "https://against-example.com/p",
                "published_at": None,
                "source_type": "primary", "no_facts_reason": None,
                "facts": [{"statement": "one", "quote": "a countering span one"},
                          {"statement": "two", "quote": "a countering span two"}]}
    apply.apply_extract(mem, Graph(mem), cfg, extract_id,
                        mem.read(extract_id), artifact)
    after_first = len(_verify_tasks_for(mem, hypothesis_id))
    apply.apply_extract(mem, Graph(mem), cfg, extract_id,
                        mem.read(extract_id), artifact)
    assert len(_verify_tasks_for(mem, hypothesis_id)) == after_first


def test_no_verification_is_seeded_when_nothing_was_attached(
    mem, cfg, evidence_scene, mkcitation
):
    """Every citation already on the counter list. Nothing about the
    balance of evidence changed, so there is nothing to re-ask."""
    hypothesis_id, extract_id = _refute_extract_scene(
        mem, cfg, evidence_scene, mkcitation)
    artifact = {"task_id": extract_id, "url": "https://against-example.com/p",
                "published_at": None,
                "source_type": "primary", "no_facts_reason": None,
                "facts": [{"statement": "the effect does not hold",
                           "quote": "we measured no such effect at all"}]}
    apply.apply_extract(mem, Graph(mem), cfg, extract_id,
                        mem.read(extract_id), artifact)
    for task in _verify_tasks_for(mem, hypothesis_id):
        mem.update(task["id"], status="done")
    before = len(_verify_tasks_for(mem, hypothesis_id))
    apply.apply_extract(mem, Graph(mem), cfg, extract_id,
                        mem.read(extract_id), artifact)
    assert len(_verify_tasks_for(mem, hypothesis_id)) == before


def test_the_reopened_verification_carries_no_refutes_proposal(
    mem, cfg, evidence_scene, mkcitation
):
    """`refutes` belongs to the hypothesizer that proposed it. Inventing
    one here would fire an assumption cascade nobody asked for."""
    hypothesis_id, extract_id = _refute_extract_scene(
        mem, cfg, evidence_scene, mkcitation)
    for task in _verify_tasks_for(mem, hypothesis_id):
        mem.update(task["id"], status="done")
    apply.apply_extract(
        mem, Graph(mem), cfg, extract_id, mem.read(extract_id),
        {"task_id": extract_id, "url": "https://against-example.com/p",
         "published_at": None,
         "source_type": "primary", "no_facts_reason": None,
         "facts": [{"statement": "the effect does not hold",
                    "quote": "we measured no such effect at all"}]})
    fresh = sorted(_verify_tasks_for(mem, hypothesis_id),
                   key=lambda t: t["id"])[-1]
    assert fresh["inputs"]["refutes"] is None


# --- gate 3's failure action -----------------------------------------

def test_a_hypothesis_short_on_evidence_gets_a_search_task(
    mem, cfg, evidence_scene, mkcitation, mkfact
):
    """Spec section 6, gate 3: 'fail -> spawn tasks seeking other
    domains'. Also what makes the coverage halt predicate reachable."""
    thin = mkcitation(url="https://one-example.com/x", domain="one-example.com",
                      quote="a thin quoted span")
    mkfact(statement="thin", citations=[thin["id"]],
           task=evidence_scene["task"]["id"])
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"], [thin["id"]]))
    result = apply.ensure_evidence_tasks(mem, Graph(mem), cfg)
    spawned = [t for t in mem.list("task")
               if (t.get("inputs") or {}).get("for_hypothesis")]
    assert len(spawned) == 1
    assert spawned[0]["kind"] == "search"
    assert spawned[0]["id"] in result.spawned


def _pending_citation_hypothesis(mem, cfg, evidence_scene, mkcitation, mkfact,
                                 mktask, recheck_status):
    """A hypothesis whose sole evidence is one `pending` citation, with the
    `recheck` task that apply_extract seeds beside such a citation, put in
    `recheck_status`. Returns (hypothesis_id, recheck_task)."""
    thin = mkcitation(url="https://one-example.com/x", domain="one-example.com",
                      quote="a thin quoted span", status="pending")
    mkfact(statement="thin", citations=[thin["id"]],
           task=evidence_scene["task"]["id"])
    recheck = mktask(question="re-read https://one-example.com/x",
                     kind="recheck", parent=evidence_scene["task"]["id"],
                     depth=1, status=recheck_status)
    # mktask has no `inputs=` keyword; _seed_recheck writes url/quotes/
    # citations, and `citations` is what pairs the task to the citation.
    recheck = mem.update(recheck["id"],
                         inputs={"url": "https://one-example.com/x",
                                 "quotes": ["a thin quoted span"],
                                 "citations": [thin["id"]]})
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"], [thin["id"]]))
    return mem.ids("hypothesis")[0], recheck


def test_a_hypothesis_whose_only_citation_is_pending_gets_no_search_task(
    mem, cfg, evidence_scene, mkcitation, mkfact, mktask
):
    """Task 6: gate 3 counts only `verified` citations, so a hypothesis
    whose sole evidence is still sitting in an unapplied re-check looks
    starved when it is merely unchecked. Spawning a search for it here
    would be a redundant dispatch on every extraction tick, for a gap
    that is about to close on its own.

    Round-4 fix: this scene used to have NO recheck task in it at all, so
    it did not actually test what its own docstring describes -- it
    pinned "pending citation" rather than "a check is still coming", and
    the veto it pinned therefore also suppressed the search for a
    citation nobody would ever check again. The re-check is now present
    and open, which is the only state in which "about to close on its
    own" is true. The assertion is unchanged.
    """
    _pending_citation_hypothesis(mem, cfg, evidence_scene, mkcitation, mkfact,
                                 mktask, recheck_status="pending")
    apply.ensure_evidence_tasks(mem, Graph(mem), cfg)
    assert [t for t in mem.list("task")
            if (t.get("inputs") or {}).get("for_hypothesis")] == []


@pytest.mark.parametrize("recheck_status", ["ready", "stale", "running"])
def test_the_veto_holds_for_every_state_a_recheck_can_still_run_from(
    mem, cfg, evidence_scene, mkcitation, mkfact, mktask, recheck_status
):
    """`running` is deliberately included alongside Graph.OPEN_TASK_STATUSES:
    a re-check dispatched this very tick is the case the veto exists for,
    and it is not in the open set."""
    _pending_citation_hypothesis(mem, cfg, evidence_scene, mkcitation, mkfact,
                                 mktask, recheck_status=recheck_status)
    apply.ensure_evidence_tasks(mem, Graph(mem), cfg)
    assert [t for t in mem.list("task")
            if (t.get("inputs") or {}).get("for_hypothesis")] == []


def test_a_hypothesis_whose_recheck_was_abandoned_does_get_a_search_task(
    mem, cfg, evidence_scene, mkcitation, mkfact, mktask
):
    """The livelock. A `recheck` abandoned after three attempts -- an
    ordinary outcome for a URL that keeps timing out -- leaves its
    citations `pending` for ever. `abandoned` is not in
    Graph.OPEN_TASK_STATUSES, so nothing is dispatchable and no halt ever
    fires; the veto then suppressed the one thing that could still make
    work, and `research next` printed "nothing to dispatch" forever.

    The veto's intent was right -- do not chase evidence that is merely
    unchecked -- but it only holds while the check is still coming. A
    citation whose re-check was abandoned is not about to be checked; it
    is starved."""
    hypothesis_id, _ = _pending_citation_hypothesis(
        mem, cfg, evidence_scene, mkcitation, mkfact, mktask,
        recheck_status="abandoned")
    result = apply.ensure_evidence_tasks(mem, Graph(mem), cfg)
    spawned = [t for t in mem.list("task")
               if (t.get("inputs") or {}).get("for_hypothesis")]
    assert len(spawned) == 1
    assert spawned[0]["kind"] == "search"
    assert spawned[0]["inputs"]["for_hypothesis"] == hypothesis_id
    assert spawned[0]["id"] in result.spawned


def test_a_hypothesis_whose_recheck_already_landed_does_get_a_search_task(
    mem, cfg, evidence_scene, mkcitation, mkfact, mktask
):
    """The other terminal state. A `done` re-check that left the citation
    `pending` cannot be waited on either -- apply_recheck only leaves a
    citation pending when the artifact omitted its index, and no further
    verdict is coming for it."""
    _pending_citation_hypothesis(mem, cfg, evidence_scene, mkcitation, mkfact,
                                 mktask, recheck_status="done")
    apply.ensure_evidence_tasks(mem, Graph(mem), cfg)
    assert len([t for t in mem.list("task")
                if (t.get("inputs") or {}).get("for_hypothesis")]) == 1


def test_a_hypothesis_with_a_verified_but_still_thin_citation_gets_a_search_task(
    mem, cfg, evidence_scene, mkcitation, mkfact
):
    """The guard above must not swallow a genuine gap: once the citation
    is actually verified and gate 3 still is not cleared, the search must
    still be spawned -- otherwise a hypothesis stuck below the bar with
    all its evidence already checked would never get more sought."""
    thin = mkcitation(url="https://one-example.com/x", domain="one-example.com",
                      quote="a thin quoted span", status="verified")
    mkfact(statement="thin", citations=[thin["id"]],
           task=evidence_scene["task"]["id"])
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"], [thin["id"]]))
    apply.ensure_evidence_tasks(mem, Graph(mem), cfg)
    spawned = [t for t in mem.list("task")
               if (t.get("inputs") or {}).get("for_hypothesis")]
    assert len(spawned) == 1


def test_the_search_question_names_which_bar_was_missed(
    mem, cfg, evidence_scene, mkcitation, mkfact
):
    """Fix round 1: the original assertion (`"citation" in question`)
    matched BOTH of gate 3's gap strings -- the count branch ("N verified
    citation(s), needs M") and the spread branch ("N citation(s) span only
    M registrable domain(s) ... a different site") both contain the
    substring "citation". This scenario (one thin citation, needing
    min_citations=3) trips the COUNT branch specifically, so the
    assertion is tightened to text unique to it and absent from the
    spread branch, the way Task 10's own count-vs-spread test was
    tightened."""
    thin = mkcitation(url="https://one-example.com/x", domain="one-example.com",
                      quote="a thin quoted span")
    mkfact(statement="thin", citations=[thin["id"]],
           task=evidence_scene["task"]["id"])
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"], [thin["id"]]))
    apply.ensure_evidence_tasks(mem, Graph(mem), cfg)
    spawned = next(t for t in mem.list("task")
                   if (t.get("inputs") or {}).get("for_hypothesis"))
    assert "verified citation" in spawned["question"]
    assert "domain" not in spawned["question"]


def test_a_hypothesis_that_clears_gate_three_gets_nothing(
    mem, cfg, evidence_scene
):
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"],
                             evidence_scene["citations"]))
    apply.ensure_evidence_tasks(mem, Graph(mem), cfg)
    assert [t for t in mem.list("task")
            if (t.get("inputs") or {}).get("for_hypothesis")] == []


def test_a_refuted_hypothesis_gets_no_more_evidence_sought(
    mem, cfg, evidence_scene, mkcitation, mkfact
):
    """Refutation is a settled answer. Searching for more sources for a
    claim already disproven is exactly the unbounded-breadth failure the
    spec's open risks warn about."""
    thin = mkcitation(url="https://one-example.com/x", domain="one-example.com",
                      quote="a thin quoted span")
    mkfact(statement="thin", citations=[thin["id"]],
           task=evidence_scene["task"]["id"])
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"], [thin["id"]]))
    mem.update(mem.ids("hypothesis")[0], status="refuted")
    apply.ensure_evidence_tasks(mem, Graph(mem), cfg)
    assert [t for t in mem.list("task")
            if (t.get("inputs") or {}).get("for_hypothesis")] == []


def test_a_second_call_does_not_pile_up_search_tasks(
    mem, cfg, evidence_scene, mkcitation, mkfact
):
    """Called once per submit for the whole run. Without the open-task
    check this adds one task per hypothesis per tick, forever."""
    thin = mkcitation(url="https://one-example.com/x", domain="one-example.com",
                      quote="a thin quoted span")
    mkfact(statement="thin", citations=[thin["id"]],
           task=evidence_scene["task"]["id"])
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"], [thin["id"]]))
    for _ in range(3):
        apply.ensure_evidence_tasks(mem, Graph(mem), cfg)
    assert len([t for t in mem.list("task")
                if (t.get("inputs") or {}).get("for_hypothesis")]) == 1


def test_a_hypothesis_whose_provenance_task_is_malformed_is_dropped_not_fatal(
    mem, cfg, evidence_scene, mkcitation, mkfact
):
    """Important 4, site 1 (fix round 1): `parent not in graph.tasks` alone
    is not a validity check -- graph.tasks keeps every task that merely
    parses. A provenance task missing `depth` must not crash indexing it
    when scheduling more evidence-seeking; it must be reported and
    skipped."""
    thin = mkcitation(url="https://one-example.com/x", domain="one-example.com",
                      quote="a thin quoted span")
    mkfact(statement="thin", citations=[thin["id"]],
           task=evidence_scene["task"]["id"])
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"], [thin["id"]]))
    _delete_field(mem, evidence_scene["task"]["id"], "depth")
    result = apply.ensure_evidence_tasks(mem, Graph(mem), cfg)
    assert result.spawned == []
    assert [t for t in mem.list("task")
            if (t.get("inputs") or {}).get("for_hypothesis")] == []
    assert any(what == "task" for what, _ in result.dropped)


def test_ensure_evidence_tasks_survives_an_open_task_missing_its_status(
    mem, cfg, evidence_scene, mkcitation, mkfact
):
    """Important 4, guard family (fix round 2): the narrower analogue of
    ensure_hypothesize_tasks's busy-loop fix. open_for's own scan already
    read `inputs` with `.get`, but indexed `status` straight off
    graph.tasks -- a task carrying `for_hypothesis` in its inputs but
    missing `status` entirely must not crash the scan; it is simply
    invisible to it, and a fresh (if redundant) search task is spawned
    rather than a KeyError escaping."""
    thin = mkcitation(url="https://one-example.com/x", domain="one-example.com",
                      quote="a thin quoted span")
    mkfact(statement="thin", citations=[thin["id"]],
           task=evidence_scene["task"]["id"])
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"], [thin["id"]]))
    hypothesis_id = mem.ids("hypothesis")[0]
    stray = mem.create("task", {
        "question": "stray", "status": "pending", "depends_on": [],
        "parent": None, "depth": 0, "kind": "search", "attempts": 0,
        "inputs": {"for_hypothesis": hypothesis_id},
        "provenance": {"task": None, "agent": "scheduler"},
    })
    _delete_field(mem, stray["id"], "status")
    result = apply.ensure_evidence_tasks(mem, Graph(mem), cfg)
    # Not visible as "already open" -- the malformed stray does not count
    # -- so a new, legitimate search task is spawned. The point is that
    # this does not raise.
    assert len(result.spawned) == 1


def test_a_failing_citation_dangling_on_disk_is_dropped_not_fatal(
    mem, cfg, mktask, mkhypothesis
):
    """Important 4, site 3 (fix round 1): a citation id is only ever
    shape-checked by the schema (graph.py's _domains_of docstring); a fact
    citing a citation whose write failed is a fully schema-valid store, no
    disk corruption required. The verifier can be handed the same
    dangling id, and rejecting a citation that never landed must not crash
    the verdict it arrived with."""
    task = mktask(question="verify", kind="verify")
    hypothesis = mkhypothesis(supporting=["C-404"], task=task["id"])
    mem.update(task["id"],
              inputs={"hypothesis": hypothesis["id"], "refutes": None})
    task = mem.read(task["id"])
    result = apply.apply_verify(
        mem, Graph(mem), cfg, task["id"], task,
        verify_artifact(task["id"], hypothesis["id"], "supported",
                        failing=["C-404"]))
    assert mem.read(hypothesis["id"])["verdict"] == "supported"
    assert "C-404" not in result.rejected_citations
    assert any(what == "citation" for what, _ in result.dropped)


def test_a_failing_citation_that_is_unparseable_is_dropped_not_fatal(
    mem, cfg, mktask, mkhypothesis, mkcitation
):
    """Important 4, guard family (fix round 2): the citation guard added
    in fix round 1 caught only KeyError (a missing file). An
    existing-but-unparseable citation raises nodes.NodeFormatError
    instead -- the same distinction index_of and Graph._readable already
    draw -- and must be dropped the same way, not escape."""
    task = mktask(question="verify", kind="verify")
    citation = mkcitation()
    hypothesis = mkhypothesis(supporting=[citation["id"]], task=task["id"])
    mem.update(task["id"],
              inputs={"hypothesis": hypothesis["id"], "refutes": None})
    task = mem.read(task["id"])
    _corrupt(mem, citation["id"])
    result = apply.apply_verify(
        mem, Graph(mem), cfg, task["id"], task,
        verify_artifact(task["id"], hypothesis["id"], "supported",
                        failing=[citation["id"]]))
    assert mem.read(hypothesis["id"])["verdict"] == "supported"
    assert citation["id"] not in result.rejected_citations
    assert any(what == "citation" for what, _ in result.dropped)


def test_apply_verify_reports_an_unparseable_hypothesis_as_an_apply_error(
    mem, cfg, mktask, mkhypothesis
):
    """Two words of code (fix round 4): apply_verify's read of
    hypothesis_id caught only KeyError. Every other read in this file
    uses (KeyError, nodes.NodeFormatError) -- an unparseable-but-existing
    hypothesis file let NodeFormatError escape the applier, the same
    failure class rounds 1-3 have been closing everywhere else. Widened
    to match, so it converts to the same ApplyError a missing file
    already produces."""
    task = mktask(question="verify", kind="verify")
    hypothesis = mkhypothesis(supporting=["C-001"])
    mem.update(task["id"],
              inputs={"hypothesis": hypothesis["id"], "refutes": None})
    task = mem.read(task["id"])
    _corrupt(mem, hypothesis["id"])
    with pytest.raises(apply.ApplyError, match=hypothesis["id"]):
        apply.apply_verify(
            mem, Graph(mem), cfg, task["id"], task,
            verify_artifact(task["id"], hypothesis["id"], "supported"))


def test_apply_verify_survives_an_unparseable_assumption_file(
    mem, cfg, evidence_scene, mkassumption
):
    """Two words of code (fix round 4): apply_verify's read of `refutes`
    also caught only KeyError. An unparseable-but-existing assumption
    file must be dropped the same way a missing one already is (recorded
    in result.dropped, no cascade), not escape as NodeFormatError."""
    assumption = mkassumption(raised_by=evidence_scene["root"]["id"])
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"],
                             evidence_scene["citations"],
                             refutes=assumption["id"]))
    verify_task = next(t for t in mem.list("task") if t["kind"] == "verify")
    hypothesis_id = mem.ids("hypothesis")[0]
    _corrupt(mem, assumption["id"])
    result = apply.apply_verify(
        mem, Graph(mem), cfg, verify_task["id"], verify_task,
        verify_artifact(verify_task["id"], hypothesis_id, "contradicted"))
    assert result.cascaded == []
    assert any(what == "cascade" for what, _ in result.dropped)


def test_the_unsupported_search_task_is_visible_to_ensure_evidence_tasks(
    mem, cfg, verify_scene
):
    """Minor 6 (fix round 1): without `for_hypothesis` in its inputs, the
    search task apply_verify spawns for an `unsupported` verdict is
    invisible to ensure_evidence_tasks's own open-task check, which then
    spawns a near-duplicate search for the same gap."""
    run_verify(mem, cfg, verify_scene, "unsupported")
    apply.ensure_evidence_tasks(mem, Graph(mem), cfg)
    for_this = [t for t in mem.list("task")
               if (t.get("inputs") or {}).get("for_hypothesis") ==
               verify_scene["hypothesis"]]
    assert len(for_this) == 1


# test_every_artifact_kind_has_an_applier lived here too, byte-identical
# to the copy in test_apply_decompose.py but without its docstring. One
# assertion, one home: the documented copy is the one that survives.


# --- C2: a schema-invalid assumption is read AND written unvalidated --

@pytest.mark.parametrize("break_it", [
    pytest.param(lambda mem, aid: _delete_field(mem, aid, "status"),
                 id="missing status (bare KeyError)"),
    pytest.param(lambda mem, aid: _delete_field(mem, aid, "blocks"),
                 id="missing blocks (ValidationError on the write)"),
    pytest.param(lambda mem, aid: _delete_field(mem, aid, "provenance"),
                 id="missing provenance"),
    pytest.param(lambda mem, aid: _set_invalid_field(mem, aid, "raised_by",
                                                     "not-a-task-id"),
                 id="raised_by fails its pattern"),
    pytest.param(lambda mem, aid: _set_invalid_field(mem, aid, "refuted_by",
                                                     "not-an-id"),
                 id="refuted_by fails its pattern"),
])
def test_apply_verify_declines_a_schema_invalid_assumption(
    mem, cfg, evidence_scene, mkassumption, break_it
):
    """run_cascades validates the same node before touching it and
    explains why in its docstring; apply_verify did not. Indexing
    `assumption["status"]` on a record that parsed but lost that key is a
    bare KeyError, and `memory.update(refutes, ...)` re-validates the
    whole merged record, so any other invalid field is a ValidationError.
    research.main catches neither: `tick_submitted` never lands, and
    every retry dies on the same line — permanently wedged, with
    hand-editing research/memory/ the only escape, which SKILL.md
    forbids."""
    assumption = mkassumption(raised_by=evidence_scene["root"]["id"])
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"],
                             evidence_scene["citations"],
                             refutes=assumption["id"]))
    verify_task = next(t for t in mem.list("task") if t["kind"] == "verify")
    hypothesis_id = mem.ids("hypothesis")[0]
    break_it(mem, assumption["id"])

    result = apply.apply_verify(
        mem, Graph(mem), cfg, verify_task["id"], verify_task,
        verify_artifact(verify_task["id"], hypothesis_id, "contradicted"))

    # Declined, and said so.
    assert result.cascaded == []
    assert any(what == "cascade" and assumption["id"] in why
               for what, why in result.dropped)
    # The verdict itself still landed; only the assumption was left alone.
    assert mem.read(hypothesis_id)["status"] == "refuted"


def test_a_wedged_verify_can_be_re_run_after_the_assumption_is_repaired(
    mem, cfg, evidence_scene, mkassumption
):
    """The recovery the fix buys: declining commits nothing, so once fsck
    has pointed at the file and it is repaired, the same artifact applies
    normally. Before, the tick could not get past this line at all."""
    assumption = mkassumption(raised_by=evidence_scene["root"]["id"])
    apply.apply_hypothesize(
        mem, Graph(mem), cfg, evidence_scene["task"]["id"],
        evidence_scene["task"],
        hypothesize_artifact(evidence_scene["task"]["id"],
                             evidence_scene["citations"],
                             refutes=assumption["id"]))
    verify_task = next(t for t in mem.list("task") if t["kind"] == "verify")
    hypothesis_id = mem.ids("hypothesis")[0]
    good = nodes.loads(mem.path_for(assumption["id"]).read_text(
        encoding="utf-8"))
    _delete_field(mem, assumption["id"], "status")

    apply.apply_verify(
        mem, Graph(mem), cfg, verify_task["id"], verify_task,
        verify_artifact(verify_task["id"], hypothesis_id, "contradicted"))
    mem.path_for(assumption["id"]).write_text(nodes.dumps(good),
                                              encoding="utf-8")

    result = apply.apply_verify(
        mem, Graph(mem), cfg, verify_task["id"], verify_task,
        verify_artifact(verify_task["id"], hypothesis_id, "contradicted"))
    assert result.cascaded == [assumption["id"]]
    assert mem.read(assumption["id"])["status"] == "refuted"


# --- I4: the cascade must not sterilize the evidence it quarantines ---
#
# The three reactivation tests that used to live here (a quarantined fact
# reactivated when its quote re-verifies; the freshly-verified citation
# landing on it; an unverifiable re-check declining to reactivate) all
# drove that reactivation through apply_extract's own gate-2 re-fetch.
# That machinery is gone from this applier -- gate 2 now runs in a
# `recheck` task (Task 6's apply_recheck), which is where reactivation
# belongs and will be re-pinned.

def test_an_active_fact_is_not_rewritten_by_a_re_run(mem, cfg, tmp_path,
                                                     extractor):
    """Idempotence: a plain recovery re-run of the same extract artifact
    must write nothing new. This no longer guards a reactivation branch
    (apply_extract has none), but the property still has to hold: dedup
    by natural key means a second, identical apply touches neither the
    citation nor the fact."""
    artifact = extract_artifact(extractor["id"])
    run_extract(mem, cfg, tmp_path, extractor, artifact)
    fact_id = mem.ids("fact")[0]
    before = mem.read(fact_id)
    result = run_extract(mem, cfg, tmp_path, extractor, artifact)
    assert result.reactivated_facts == []
    assert mem.read(fact_id) == before


def test_redoing_the_extract_alone_does_not_resurrect_a_quarantined_fact(
    mem, cfg, tmp_path, mktask, mkassumption
):
    """The old version of this test measured the FULL restoration: cascade
    quarantines the fact and stales the extract task, the task is redone,
    and the branch has live evidence again -- because apply_extract used
    to re-run gate 2 inline and reactivate on a fresh VERIFIED. Gate 2 now
    runs in a separate `recheck` task apply_extract only seeds, so redoing
    the extract alone lands back on the same citation and the same fact by
    natural key, and dedup-by-key alone does not revive either one. Only a
    `recheck` confirming the quote again (Task 6's apply_recheck) can lift
    the quarantine -- pinned here as what does NOT happen on this tick, so
    it is not silently reintroduced into apply_extract."""
    root = mktask(question="root", kind="decompose", status="done")
    reader = mktask(question="read it", kind="extract", parent=root["id"],
                    depth=1, status="done")
    artifact = extract_artifact(reader["id"])
    run_extract(mem, cfg, tmp_path, reader, artifact)
    fact_id = mem.ids("fact")[0]

    assumption = mkassumption(raised_by=root["id"], status="refuted")
    Graph(mem).cascade(assumption["id"])
    assert mem.read(fact_id)["status"] == "quarantined"
    assert mem.read(reader["id"])["status"] == "stale"

    run_extract(mem, cfg, tmp_path, mem.read(reader["id"]), artifact)
    assert mem.read(fact_id)["status"] == "quarantined"


# --- I2: a degenerate quote cannot crash the applier ------------------

def test_a_quote_too_short_to_store_drops_its_fact_instead_of_raising(
    mem, cfg, tmp_path, extractor
):
    """Gate 1 refuses this artifact before an applier ever sees it, so on
    the real path this is unreachable. It exists because "unreachable"
    and "cannot crash submit" are different promises: creating the
    citation would raise ValidationError out of memory.create, past
    ApplyError, past research.main, and take the whole tick down."""
    artifact = extract_artifact(extractor["id"], quotes=("a",))
    result = run_extract(mem, cfg, tmp_path, extractor, artifact)
    assert mem.ids("citation") == []
    assert mem.ids("fact") == []
    assert any(what == "citation" for what, _ in result.dropped)
    assert any(what == "fact" for what, _ in result.dropped)


def test_a_short_quote_does_not_cost_its_healthy_sibling(
    mem, cfg, tmp_path, extractor
):
    artifact = extract_artifact(
        extractor["id"], quotes=("a", "The service reports 42ms at p99"))
    run_extract(mem, cfg, tmp_path, extractor, artifact)
    assert len(mem.ids("citation")) == 1
    assert mem.list("citation")[0]["quote"] == "The service reports 42ms at p99"


def test_rounds_parented_the_way_the_scheduler_parents_them_still_merge(
    mem, cfg, branch_scene
):
    """The regression guard the plan-9 tests could not be.

    They hand-built hypothesize tasks under a depth-1 parent, so
    theme_of gave a shared theme and the merge fired. The SCHEDULER
    parents every round on the run root (ensure_hypothesize_tasks ->
    branch_of -> root_branch, a constant function), which makes each
    round its own theme and the merge inert. Measured on a driven run:
    nine nodes for one claim, and nine chapters to match.
    """
    root = branch_scene["root"]
    for label, upto in ((1, 1), (3, 3), (5, 5)):
        hypothesize = mem.create("task", {
            "question": f"Form candidate claims from the {label} facts "
                        f"gathered under: why is the sky blue?",
            "kind": "hypothesize", "parent": root, "depth": 0,
            "status": "running", "depends_on": [], "attempts": 0, "inputs": {},
            "provenance": {"task": None, "agent": "scheduler"}})["id"]
        apply.apply_hypothesize(
            mem, Graph(mem), cfg, hypothesize, mem.read(hypothesize),
            {"task_id": hypothesize, "no_hypotheses_reason": None,
             "hypotheses": [{"claim": CLAIM,
                             "supporting": branch_scene["citations"][:upto],
                             "counter": [], "refutes": None}]})
    assert len(mem.ids("hypothesis")) == 1
    assert mem.read(mem.ids("hypothesis")[0])["supporting"] == sorted(
        branch_scene["citations"][:5])
