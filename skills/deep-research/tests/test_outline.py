import pytest

import graph as graph_mod
import outline


@pytest.fixture
def themed(mem, mktask, mkfact, mkhypothesis):
    """A two-theme graph with one deep task, one refuted hypothesis and
    one orphan. Returns (graph, cfg, ids)."""
    root = mktask(question="why is the sky blue?", kind="decompose")["id"]
    theme_a = mktask(question="optical scattering", parent=root, depth=1)["id"]
    theme_b = mktask(question="human perception", parent=root, depth=1)["id"]
    deep = mktask(question="Rayleigh detail", parent=theme_a, depth=2)["id"]

    ids = {
        "root": root, "theme_a": theme_a, "theme_b": theme_b, "deep": deep,
        # Raised three levels down; must still land in theme_a.
        "h_deep": mkhypothesis(claim="scattering", task=deep)["id"],
        "h_b": mkhypothesis(claim="perception", task=theme_b)["id"],
        "h_refuted": mkhypothesis(claim="wrong", task=theme_a,
                                  status="refuted")["id"],
        "h_orphan": mkhypothesis(claim="orphan", task=None)["id"],
        "f_deep": mkfact(statement="blue scatters", task=deep)["id"],
        "f_b": mkfact(statement="cones peak", task=theme_b)["id"],
        "f_quarantined": mkfact(statement="dropped", task=theme_a,
                                status="quarantined")["id"],
    }
    cfg = {"question": "why is the sky blue?"}
    return graph_mod.Graph(mem), cfg, ids


def test_each_theme_becomes_one_section(themed):
    graph, cfg, ids = themed
    result = outline.compute(graph, cfg)
    assert [section["theme"] for section in result["sections"]] == [
        ids["theme_a"], ids["theme_b"]]


def test_section_ids_are_sequential_in_theme_order(themed):
    graph, cfg, _ = themed
    result = outline.compute(graph, cfg)
    assert [section["id"] for section in result["sections"]] == ["S-001", "S-002"]


def test_a_section_title_starts_as_its_theme_question(themed):
    graph, cfg, _ = themed
    result = outline.compute(graph, cfg)
    assert result["sections"][0]["title"] == "optical scattering"


def test_a_deep_hypothesis_is_assigned_to_its_theme_not_its_parent(themed):
    """The whole point of theme_of over root_branch. A hypothesis raised by
    a depth-2 task belongs to the depth-1 theme above it."""
    graph, cfg, ids = themed
    result = outline.compute(graph, cfg)
    assert result["sections"][0]["hypotheses"] == [ids["h_deep"]]


def test_a_refuted_hypothesis_is_not_in_any_section(themed):
    graph, cfg, ids = themed
    result = outline.compute(graph, cfg)
    assigned = [h for section in result["sections"] for h in section["hypotheses"]]
    assert ids["h_refuted"] not in assigned


def test_a_quarantined_fact_is_not_in_any_section(themed):
    graph, cfg, ids = themed
    result = outline.compute(graph, cfg)
    assigned = [f for section in result["sections"] for f in section["facts"]]
    assert ids["f_quarantined"] not in assigned


def test_a_hypothesis_with_no_provenance_task_is_an_orphan(themed):
    graph, cfg, ids = themed
    result = outline.compute(graph, cfg)
    assert result["orphans"]["hypotheses"] == [ids["h_orphan"]]


def test_the_seeded_root_is_never_reported_as_an_empty_theme(themed):
    """The root IS the question the report answers. Reporting it in
    Appendix C as "this line of enquiry produced no findings" told every
    reader the report had failed at its own subject.

    theme_of(root) is root — it has no depth-1 ancestor — so collecting
    themes as theme_of(every task) always swept it in, and it carries no
    hypotheses or facts directly, so it always fell through to
    empty_themes."""
    graph, cfg, ids = themed
    result = outline.compute(graph, cfg)
    assert ids["root"] not in result["empty_themes"]
    assert ids["root"] not in [section["theme"] for section in result["sections"]]


def test_a_hypothesize_task_on_the_root_is_not_an_empty_theme(mem, mktask,
                                                               mkhypothesis):
    """apply.ensure_hypothesize_tasks parents its task on root_branch —
    the run root — at the root's own depth. theme_of then resolves it to
    ITSELF, so it arrived as a theme carrying nothing and Appendix C
    printed the scheduler's own bookkeeping as an unresolved question.

    MACHINERY_KINDS did not cover it: that list is `outline` and
    `synthesize`, and this task's kind is `hypothesize`."""
    root = mktask(question="why is the sky blue?", kind="decompose")["id"]
    theme = mktask(question="optical scattering", parent=root, depth=1)["id"]
    mkhypothesis(claim="c", task=theme)
    scheduled = mktask(question="Form candidate claims from the 3 facts",
                       kind="hypothesize", parent=root, depth=0)["id"]

    result = outline.compute(graph_mod.Graph(mem), {"question": "q"})
    assert scheduled not in result["empty_themes"]
    assert result["empty_themes"] == []


def test_a_theme_that_really_produced_nothing_is_still_reported(mem, mktask,
                                                                 mkhypothesis):
    """The guard against the fix becoming "report nothing".

    A report that silently hides the directions that came up empty is a
    worse defect than the one being fixed: it overstates how completely
    the question was covered. A depth-1 child of the root, of a kind a
    decomposer can actually propose, carrying no hypothesis and no fact,
    must still reach Appendix C."""
    root = mktask(question="why is the sky blue?", kind="decompose")["id"]
    alive = mktask(question="optical scattering", parent=root, depth=1)["id"]
    dead = mktask(question="human perception", parent=root, depth=1,
                  kind="search")["id"]
    mkhypothesis(claim="c", task=alive)

    result = outline.compute(graph_mod.Graph(mem), {"question": "q"})
    assert result["empty_themes"] == [dead]
    assert [section["theme"] for section in result["sections"]] == [alive]


def test_the_outline_task_is_not_mistaken_for_the_run_root(mem, mktask,
                                                            mkhypothesis):
    """A live run holds TWO parentless tasks: the seeded `decompose` root
    and the `outline` task synthesis.seed creates at depth 0. A rule keyed
    on `parent is None` alone would pick whichever sorted first and then
    collect that one's children as the report's themes — for the outline
    task, none at all, emptying the report."""
    root = mktask(question="why is the sky blue?", kind="decompose")["id"]
    theme = mktask(question="optical scattering", parent=root, depth=1)["id"]
    mkhypothesis(claim="c", task=theme)
    # Seeded by synthesis.seed BEFORE the root in id order is impossible,
    # but its kind is what does the distinguishing, not its id.
    mktask(question="arrange the report outline", kind="outline", parent=None,
           depth=0)

    result = outline.compute(graph_mod.Graph(mem), {"question": "q"})
    assert [section["theme"] for section in result["sections"]] == [theme]
    assert result["empty_themes"] == []


def test_a_theme_kind_matches_what_a_decomposer_may_propose(mem, mktask,
                                                             mkhypothesis):
    """Both kinds in artifact.decompose.json's `children[].kind` enum count
    as themes. `decompose` is the one that would be missed by a rule that
    only looked for `search`, and it is how a broad question grows a
    sub-tree."""
    root = mktask(question="why is the sky blue?", kind="decompose")["id"]
    alive = mktask(question="optical scattering", parent=root, depth=1)["id"]
    branch = mktask(question="human perception", parent=root, depth=1,
                    kind="decompose")["id"]
    mkhypothesis(claim="c", task=alive)

    result = outline.compute(graph_mod.Graph(mem), {"question": "q"})
    assert branch in result["empty_themes"]


def test_synthesis_machinery_tasks_are_not_themes(mem, mktask, mkhypothesis):
    """`research synthesize` seeds an outline task into the same store. It
    carries no hypotheses, so theme collection would report it as an
    `empty_theme` and Appendix C would print the report's own machinery as
    an unresolved open question.

    Uses `synthesize`, which is already in task.json's enum as shipped;
    `outline` joins it in Task 5. Both are in MACHINERY_KINDS and this
    pins the behaviour for both."""
    root = mktask(question="root", kind="decompose")["id"]
    theme = mktask(question="a theme", parent=root, depth=1)["id"]
    mkhypothesis(claim="c", task=theme)
    mktask(question="write a section", kind="synthesize", parent=None,
           depth=0)

    result = outline.compute(graph_mod.Graph(mem), {"question": "q"})
    assert [section["theme"] for section in result["sections"]] == [theme]
    assert result["empty_themes"] == []


def test_a_hypothesis_anchored_directly_on_a_machinery_task_is_an_orphan(
        mem, mktask, mkhypothesis):
    """The static candidate loop in `compute` only ever sees a machinery
    task through its OWN kind, filtered before `_theme` is even called.
    `place()` resolves a hypothesis's or fact's theme independently, off
    `provenance.task`, and must apply the same MACHINERY_KINDS filter or a
    finding anchored directly on the report's own machinery (a `synthesize`
    task, self-themed because it sits at depth 0) is promoted into a real
    section instead of falling to `orphans` — the exact failure
    `test_synthesis_machinery_tasks_are_not_themes` exists to prevent,
    unguarded on the dynamic-registration path."""
    root = mktask(question="root", kind="decompose")["id"]
    theme = mktask(question="a theme", parent=root, depth=1)["id"]
    mkhypothesis(claim="c", task=theme)
    synth = mktask(question="write a section", kind="synthesize",
                   parent=None, depth=0)["id"]
    orphan = mkhypothesis(claim="machinery-anchored", task=synth)["id"]

    result = outline.compute(graph_mod.Graph(mem), {"question": "q"})
    assert orphan in result["orphans"]["hypotheses"]
    assert synth not in [section["theme"] for section in result["sections"]]


def test_every_live_hypothesis_is_assigned_exactly_once(themed):
    graph, cfg, ids = themed
    result = outline.compute(graph, cfg)
    assigned = [h for section in result["sections"] for h in section["hypotheses"]]
    assert sorted(assigned) == sorted([ids["h_deep"], ids["h_b"]])
    assert len(assigned) == len(set(assigned))


def test_assigned_ids_are_sorted_numerically(themed):
    """Global constraint: every id-returning function returns sorted. Sorted
    NUMERICALLY, so H-1000 does not precede H-999."""
    graph, cfg, _ = themed
    result = outline.compute(graph, cfg)
    for section in result["sections"]:
        assert section["hypotheses"] == sorted(
            section["hypotheses"], key=lambda i: int(i.split("-")[1]))


def test_a_schema_invalid_theme_task_does_not_crash_the_outline(mem, mktask,
                                                                mkhypothesis):
    """graph.tasks keeps tasks that parse but fail their schema. Indexing
    ["question"] on one raises KeyError straight out of synthesis."""
    root = mktask(question="root", kind="decompose")["id"]
    theme = mktask(question="theme", parent=root, depth=1)["id"]
    mkhypothesis(claim="c", task=theme)
    # `question` is the markdown BODY, not frontmatter, so patching
    # "question: theme" would be a no-op and this test would pass without
    # testing anything. `type: task` IS in frontmatter, and task.json sets
    # additionalProperties: false, so the injected key invalidates it while
    # the file still parses.
    path = mem.path_for(theme)
    path.write_text(path.read_text(encoding="utf-8").replace(
        "type: task", "type: task\nbogus_field: 1"), encoding="utf-8")

    result = outline.compute(graph_mod.Graph(mem), {"question": "q"})
    assert len(result["sections"]) == 1
    assert result["sections"][0]["title"]  # a fallback title, never empty


def _artifact_from(computed, **overrides):
    """A well-formed outliner artifact echoing the computed outline."""
    sections = [
        {"id": section["id"], "title": section["title"],
         "hypotheses": list(section["hypotheses"]),
         "facts": list(section["facts"])}
        for section in computed["sections"]
    ]
    artifact = {"task_id": "T-099", "sections": sections}
    artifact.update(overrides)
    return artifact


def test_an_unchanged_outline_validates(themed):
    graph, cfg, _ = themed
    computed = outline.compute(graph, cfg)
    assert outline.validate(computed, _artifact_from(computed)) == []


def test_reordering_sections_is_allowed(themed):
    graph, cfg, _ = themed
    computed = outline.compute(graph, cfg)
    artifact = _artifact_from(computed)
    artifact["sections"].reverse()
    assert outline.validate(computed, artifact) == []


def test_retitling_a_section_is_allowed(themed):
    graph, cfg, _ = themed
    computed = outline.compute(graph, cfg)
    artifact = _artifact_from(computed)
    artifact["sections"][0]["title"] = "How light scatters"
    assert outline.validate(computed, artifact) == []


def test_moving_a_hypothesis_between_sections_is_allowed(themed):
    """Reassignment is a legitimate editorial judgement; losing one is not."""
    graph, cfg, _ = themed
    computed = outline.compute(graph, cfg)
    artifact = _artifact_from(computed)
    moved = artifact["sections"][0]["hypotheses"].pop()
    artifact["sections"][1]["hypotheses"].append(moved)
    assert outline.validate(computed, artifact) == []


def test_emptying_a_section_is_rejected(themed):
    """`compute` refuses to emit a section carrying nothing — "a theme that
    carries nothing is not a section". `validate` did not re-apply that
    rule: its checks are multisets over ALL sections combined, and
    outliner.md explicitly licenses moving a finding between sections. So
    an outliner consolidating everything into S-001 passed validation,
    apply_outline seeded a writer with hypotheses [], facts [] and
    allowed_cite_keys [], and gate 5 waves through unmarked qualitative
    prose — an evidence-free chapter written entirely by the model.

    test_moving_a_hypothesis_between_sections_is_allowed leaves a fact
    behind, which is why this case was untested."""
    graph, cfg, _ = themed
    computed = outline.compute(graph, cfg)
    artifact = _artifact_from(computed)
    for key in ("hypotheses", "facts"):
        artifact["sections"][1][key] += artifact["sections"][0][key]
        artifact["sections"][0][key] = []

    errors = outline.validate(computed, artifact)
    assert any("S-001" in error for error in errors)
    # Nothing was dropped or invented — the multiset checks are all happy.
    # Only the new rule can be reporting this.
    assert not any("dropped" in error or "not in the computed" in error
                   for error in errors)


def test_a_section_holding_only_a_fact_is_accepted(themed):
    """The rule is "neither a hypothesis nor a fact", not "both". A section
    narrating facts alone is legitimate, and rejecting it would burn a
    retry on a correct outline."""
    graph, cfg, _ = themed
    computed = outline.compute(graph, cfg)
    artifact = _artifact_from(computed)
    artifact["sections"][1]["hypotheses"] += artifact["sections"][0]["hypotheses"]
    artifact["sections"][0]["hypotheses"] = []
    assert outline.validate(computed, artifact) == []


def test_dropping_a_hypothesis_is_rejected(themed):
    graph, cfg, ids = themed
    computed = outline.compute(graph, cfg)
    artifact = _artifact_from(computed)
    artifact["sections"][0]["hypotheses"] = []
    errors = outline.validate(computed, artifact)
    assert any(ids["h_deep"] in error and "dropped" in error for error in errors)


def test_inventing_a_hypothesis_is_rejected(themed):
    graph, cfg, _ = themed
    computed = outline.compute(graph, cfg)
    artifact = _artifact_from(computed)
    artifact["sections"][0]["hypotheses"].append("H-999")
    errors = outline.validate(computed, artifact)
    assert any("H-999" in error for error in errors)


def test_assigning_one_hypothesis_to_two_sections_is_rejected(themed):
    """'Exactly once' is the requirement. Twice would double-count it in the
    report and in every per-section allowed-cite-key set."""
    graph, cfg, ids = themed
    computed = outline.compute(graph, cfg)
    artifact = _artifact_from(computed)
    artifact["sections"][1]["hypotheses"].append(ids["h_deep"])
    errors = outline.validate(computed, artifact)
    assert any(ids["h_deep"] in error and "more than one" in error
               for error in errors)


def test_dropping_a_whole_section_is_rejected(themed):
    graph, cfg, _ = themed
    computed = outline.compute(graph, cfg)
    artifact = _artifact_from(computed)
    dropped = artifact["sections"].pop()
    errors = outline.validate(computed, artifact)
    assert any(dropped["id"] in error for error in errors)


def test_inventing_a_section_is_rejected(themed):
    graph, cfg, _ = themed
    computed = outline.compute(graph, cfg)
    artifact = _artifact_from(computed)
    artifact["sections"].append(
        {"id": "S-099", "title": "Invented", "hypotheses": [], "facts": []})
    errors = outline.validate(computed, artifact)
    assert any("S-099" in error for error in errors)


def test_an_empty_title_is_rejected(themed):
    graph, cfg, _ = themed
    computed = outline.compute(graph, cfg)
    artifact = _artifact_from(computed)
    artifact["sections"][0]["title"] = "   "
    errors = outline.validate(computed, artifact)
    assert any("title" in error for error in errors)


def test_every_problem_is_reported_at_once(themed):
    """One retry must be able to fix everything. Reporting one problem per
    attempt burns all three attempts on three separate complaints."""
    graph, cfg, _ = themed
    computed = outline.compute(graph, cfg)
    artifact = _artifact_from(computed)
    artifact["sections"][0]["hypotheses"] = ["H-999"]
    artifact["sections"][1]["title"] = ""
    errors = outline.validate(computed, artifact)
    assert len(errors) >= 3  # dropped, invented, empty title


def test_apply_artifact_takes_order_and_titles_from_the_artifact(themed):
    graph, cfg, _ = themed
    computed = outline.compute(graph, cfg)
    artifact = _artifact_from(computed)
    artifact["sections"].reverse()
    artifact["sections"][0]["title"] = "Perception first"

    accepted = outline.apply_artifact(computed, artifact)
    assert [section["id"] for section in accepted["sections"]] == ["S-002", "S-001"]
    assert accepted["sections"][0]["title"] == "Perception first"


def test_apply_artifact_keeps_the_theme_from_the_computed_outline(themed):
    """`theme` is graph topology, not editorial. The outliner never sees it
    and must never be able to change it — Appendix C and the retry path
    both resolve a section back to its theme task through this field."""
    graph, cfg, ids = themed
    computed = outline.compute(graph, cfg)
    artifact = _artifact_from(computed)
    artifact["sections"][0]["theme"] = "T-999"

    accepted = outline.apply_artifact(computed, artifact)
    assert accepted["sections"][0]["theme"] == ids["theme_a"]


# --- a chapter is a line of enquiry, not a scheduler round ------------
#
# apply.ensure_hypothesize_tasks parents every round on `branch_of(...)`,
# which resolves through Graph.root_branch -- "on a real run this is a
# constant function", the seeded root. So every hypothesize task is a
# depth-0 child of the root and `theme_of` resolves it to ITSELF, and
# `place` registers a bucket for it rather than lose the finding.
#
# Measured on a run driven through research.main:
#
#   S-001 how does scattering work?                    hyps: 0  facts: 3
#   S-002 Form candidate claims from the 3 facts g...  hyps: 1  facts: 1
#   ...  nine chapters named after scheduler bookkeeping, each holding
#        the same claim, and the one real theme holding no findings.
#
# The fixtures above never caught it because they hand-build hypotheses
# under real themes. These build them the way the scheduler does.

@pytest.fixture
def real_parenting(mem, mktask, mkfact, mkhypothesis, mkcitation):
    """A graph shaped the way the loop actually builds one: findings are
    raised by a `hypothesize` task parented on the run root."""
    root = mktask(question="why is the sky blue?", kind="decompose")["id"]
    theme = mktask(question="optical scattering", parent=root, depth=1)["id"]
    extract = mktask(question="read it", kind="extract", parent=theme,
                     depth=1)["id"]
    other = mktask(question="human perception", parent=root, depth=1)["id"]
    other_extract = mktask(question="read that", kind="extract", parent=other,
                           depth=1)["id"]
    # Parented on the ROOT at the root's own depth, exactly as
    # ensure_hypothesize_tasks does it.
    round_one = mktask(question="Form candidate claims from the 3 facts "
                                "gathered under: why is the sky blue?",
                       kind="hypothesize", parent=root, depth=0)["id"]

    def evidence(task, tag):
        citation = mkcitation(url=f"https://{tag}-example.com/p",
                              domain=f"{tag}-example.com",
                              quote=f"a quoted span for {tag}")["id"]
        mkfact(statement=f"fact {tag}", citations=[citation], task=task)
        return citation

    return {"mem": mem, "root": root, "theme": theme, "other": other,
            "round_one": round_one, "evidence": evidence,
            "extract": extract, "other_extract": other_extract,
            "cfg": {"question": "why is the sky blue?"}}


def test_a_hypothesis_lands_in_the_theme_its_evidence_came_from(
    real_parenting, mkhypothesis
):
    scene = real_parenting
    citation = scene["evidence"](scene["extract"], "a")
    mkhypothesis(claim="Rayleigh explains it", supporting=[citation],
                 task=scene["round_one"])
    result = outline.compute(graph_mod.Graph(scene["mem"]), scene["cfg"])
    themes = {section["theme"] for section in result["sections"]}
    assert scene["round_one"] not in themes, (
        "a scheduler round became a chapter")
    placed = {h for section in result["sections"] for h in section["hypotheses"]}
    assert placed, "the finding was lost rather than relocated"
    assert [s["theme"] for s in result["sections"]
            if s["hypotheses"]] == [scene["theme"]]


def test_a_claim_built_from_two_themes_lands_in_the_dominant_one(
    real_parenting, mkhypothesis
):
    """A claim spanning two enquiries is real and has to land in exactly
    one chapter. The enquiry that supplied most of its support is the
    honest choice."""
    scene = real_parenting
    here = [scene["evidence"](scene["extract"], f"a{i}") for i in range(2)]
    there = [scene["evidence"](scene["other_extract"], "b")]
    mkhypothesis(claim="spans both", supporting=here + there,
                 task=scene["round_one"])
    result = outline.compute(graph_mod.Graph(scene["mem"]), scene["cfg"])
    carrying = [s["theme"] for s in result["sections"] if s["hypotheses"]]
    assert carrying == [scene["theme"]]


def test_counter_citations_do_not_move_a_claim_between_chapters(
    real_parenting, mkhypothesis
):
    """A successful refutation must not relocate the finding it
    challenged. The refute search runs under whatever task raised the
    claim; letting its citations vote would file the claim under its own
    disproof."""
    scene = real_parenting
    supporting = scene["evidence"](scene["extract"], "a")
    against = [scene["evidence"](scene["other_extract"], f"b{i}")
               for i in range(3)]
    mkhypothesis(claim="challenged", supporting=[supporting],
                 counter=against, task=scene["round_one"])
    result = outline.compute(graph_mod.Graph(scene["mem"]), scene["cfg"])
    carrying = [s["theme"] for s in result["sections"] if s["hypotheses"]]
    assert carrying == [scene["theme"]]


def test_a_claim_with_no_resolvable_evidence_is_still_placed_or_orphaned(
    real_parenting, mkhypothesis
):
    """place's original reasoning stands: losing a finding out of the
    body is worse than an ugly chapter, and it is worse than this fix
    too. With nothing to roll up through, it falls back to the old
    behaviour and is reported either way."""
    scene = real_parenting
    hypothesis = mkhypothesis(claim="no evidence at all",
                              task=scene["round_one"])["id"]
    result = outline.compute(graph_mod.Graph(scene["mem"]), scene["cfg"])
    placed = {h for section in result["sections"] for h in section["hypotheses"]}
    assert hypothesis in placed or hypothesis in result["orphans"]["hypotheses"]


def test_a_theme_that_produced_nothing_is_still_reported_empty(
    real_parenting, mkhypothesis
):
    """empty_themes feeds Appendix C. Hiding a line of enquiry that died
    overstates how completely the question was covered."""
    scene = real_parenting
    citation = scene["evidence"](scene["extract"], "a")
    mkhypothesis(claim="only here", supporting=[citation],
                 task=scene["round_one"])
    result = outline.compute(graph_mod.Graph(scene["mem"]), scene["cfg"])
    assert scene["other"] in result["empty_themes"]


def test_counter_evidence_lands_in_the_theme_of_the_claim_it_challenges(
    real_parenting, mkhypothesis, mktask, mkfact, mkcitation
):
    """The facts half of the same defect, and it survived the first fix.

    ensure_refute_tasks parents a refute search on its target's
    provenance task — a hypothesize round, i.e. a root child — so the
    extract under it, and the counter-evidence fact under that, all roll
    up to that round. Measured after fixing hypotheses: the claim was
    placed correctly and its refutation still produced

        S-002  "Form candidate claims from the 3 facts gathe..."  facts: 1

    A refutation belongs with the claim it challenges. The extract task
    carries `inputs.for_hypothesis` (apply_search propagates it for
    exactly this stance), so the rollup point is already on the node.
    """
    scene = real_parenting
    supporting = scene["evidence"](scene["extract"], "a")
    hypothesis = mkhypothesis(claim="challenged", supporting=[supporting],
                              task=scene["round_one"])["id"]
    refute = mktask(question="Find evidence that would show this claim is "
                             "false: challenged",
                    kind="search", parent=scene["round_one"], depth=0)["id"]
    scene["mem"].update(refute, inputs={"for_hypothesis": hypothesis,
                                        "stance": "against"})
    counter_extract = mktask(question="read the contrary page", kind="extract",
                             parent=refute, depth=0)["id"]
    scene["mem"].update(counter_extract,
                        inputs={"url": "https://contra-example.com/p",
                                "for_hypothesis": hypothesis,
                                "stance": "against"})
    citation = mkcitation(url="https://contra-example.com/p",
                          domain="contra-example.com",
                          quote="a contrary span here")["id"]
    mkfact(statement="the contrary finding", citations=[citation],
           task=counter_extract)

    result = outline.compute(graph_mod.Graph(scene["mem"]), scene["cfg"])
    titles = [s["title"] for s in result["sections"]]
    assert not any(t.startswith("Form candidate claims") for t in titles), titles
    carrying = [s["theme"] for s in result["sections"] if s["facts"]]
    assert carrying == [scene["theme"]], titles


def test_a_scheduler_round_can_never_title_a_chapter(
    real_parenting, mkhypothesis
):
    """The backstop. Whatever fails to roll up, a `hypothesize` task is
    machinery and must not become a line of enquiry in the report — the
    same rule MACHINERY_KINDS already applies to `outline` and
    `synthesize` tasks."""
    scene = real_parenting
    mkhypothesis(claim="no evidence at all", task=scene["round_one"])
    result = outline.compute(graph_mod.Graph(scene["mem"]), scene["cfg"])
    assert scene["round_one"] not in [s["theme"] for s in result["sections"]]
