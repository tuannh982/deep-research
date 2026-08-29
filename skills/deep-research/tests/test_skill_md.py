"""SKILL.md is loaded into context on every turn of a multi-day run, and
it is the only thing that survives a compaction on its own. Both tests
that matter here are about that."""
import re
from pathlib import Path

import yaml

import research

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
REFERENCE = ROOT / "references" / "loop-protocol.md"


def parts():
    text = SKILL.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", text, re.DOTALL)
    assert match, "SKILL.md has no frontmatter"
    return yaml.safe_load(match.group(1)), match.group(2)


def test_the_frontmatter_has_a_name_and_a_description():
    front, _ = parts()
    assert front["name"] == "deep-research"
    assert front["description"]


def test_the_description_starts_with_use_when():
    """The convention every other skill follows; it is what the model
    matches against when deciding whether this applies."""
    front, _ = parts()
    assert front["description"].startswith("Use when")


def test_the_description_is_one_line():
    front, _ = parts()
    assert "\n" not in front["description"]


def test_the_body_stays_about_one_page():
    """Spec section 1 says '~1 page'. This file is in context on every
    turn of a run that may last days. The cap was 70 through plan 2, then
    95 once this file documented a second phase (the report). It now also
    documents a re-check phase — gate 2 is a subagent the harness must be
    told to grant tools for — so the cap moves to 100, still about one
    screen."""
    _, body = parts()
    assert len(body.splitlines()) <= 100, len(body.splitlines())


def test_the_loop_is_three_steps():
    """Spec section 4: 'SKILL.md's loop is three lines, so it survives
    compaction too.' Three, not four, not a flowchart."""
    _, body = parts()
    section = body.split("## The loop", 1)[1].split("\n## ", 1)[0]
    steps = re.findall(r"^\s*\d+\.", section, re.MULTILINE)
    assert len(steps) == 3, steps


def test_every_command_named_in_skill_md_exists():
    """The test that keeps the documentation from drifting away from the
    dispatch table."""
    _, body = parts()
    named = set(re.findall(r"research (\w+)", body))
    unknown = named - set(research.COMMANDS)
    assert not unknown, unknown


def test_every_command_named_in_the_reference_exists():
    named = set(re.findall(r"research (\w+)",
                           REFERENCE.read_text(encoding="utf-8")))
    assert not named - set(research.COMMANDS)


def test_the_loop_commands_are_all_mentioned():
    """A loop that does not name `next`, `submit` and `signal` is not the
    loop."""
    _, body = parts()
    for command in ("next", "submit", "signal"):
        assert f"research {command}" in body, command


def test_the_recovery_command_is_mentioned():
    """Spec section 4: 'research resume reprints the protocol if even
    that is lost.' Useless if the file that gets lost is the only place
    it is named — so it is in the reference too."""
    _, body = parts()
    assert "research resume" in body


def test_skill_md_says_subagents_are_dispatched_in_parallel():
    """Spec section 4's step packet says so, but the packet is only read
    if the loop got that far."""
    _, body = parts()
    assert "parallel" in body.lower()


def test_skill_md_does_not_describe_the_scheduler():
    """The scheduler is code. A prose description of it in the one file
    the model reads every turn is an invitation to second-guess the
    dispatch set."""
    _, body = parts()
    for forbidden in ("frontier", "max_parallel", "depth cap"):
        assert forbidden not in body.lower(), forbidden


def test_the_reference_exists_and_is_linked():
    assert REFERENCE.is_file()
    _, body = parts()
    assert "references/loop-protocol.md" in body


def test_the_reference_does_not_describe_gate_2_as_an_httpx_refetch():
    """Gate 2 moved into a subagent. This sentence was the clearest
    statement of the old design, so it is the one most likely to be
    believed after it stopped being true."""
    text = REFERENCE.read_text(encoding="utf-8")
    assert "httpx" not in text
    assert "rechecker" in text or "re-check" in text


def test_the_reference_covers_what_skill_md_leaves_out():
    text = REFERENCE.read_text(encoding="utf-8")
    for topic in ("gate", "halt", "checkpoint", "compaction", "inbox"):
        assert topic in text.lower(), topic


def test_skill_md_documents_the_report_phase():
    """Was test_no_skill_file_claims_synthesis_works_yet, which pinned the
    plan-2 state: SKILL.md had to admit it could not produce a PDF. Plan 3
    built it, so the assertion flips — the failure this now guards against
    is a skill that CAN produce a report and never tells the model so."""
    _, body = parts()
    assert "research synthesize" in body
    assert "research render" in body
    assert "plan 3" not in body.lower()


def test_the_report_phase_does_not_become_a_second_loop():
    """The compaction-survival argument rests on there being ONE loop. If
    synthesis introduced its own numbered procedure, the model has two to
    remember and will conflate them under pressure."""
    _, body = parts()
    section = body.split("## The report", 1)[1].split("\n## ", 1)[0]
    assert not re.findall(r"^\s*\d+\.", section, re.MULTILINE)


def test_loop_protocol_documents_gate_5():
    text = REFERENCE.read_text(encoding="utf-8")
    assert "gate 5" in text.lower()
    assert "build-report.md" in text


def test_skill_md_points_at_the_opencode_mapping():
    """The dispatch packet names Claude Code's tools because it is generated
    from run.yaml by scheduler.render, which is deliberately not
    harness-aware. A reader on another harness needs to be told where the
    translation lives, or the packet is just wrong for them."""
    _, body = parts()
    assert "opencode-tools.md" in body
    assert (ROOT / "references" / "opencode-tools.md").is_file()


def test_skill_md_says_research_stops_at_synthesis():
    """The loop keeps running during synthesis but only dispatches
    synthesis work. An operator who does not know that will read a
    hypothesis frozen mid-corroboration as a bug."""
    _, body = parts()
    section = body.split("## The report", 1)[1].split("\n## ", 1)[0]
    assert "stops" in section.lower() or "no longer" in section.lower()


FORK = ROOT.parent / "research-brainstorming" / "SKILL.md"


CROSS_PLUGIN_BRAINSTORMING = re.compile(r"[a-z][a-z0-9-]*:brainstorming")


def test_the_loop_calls_our_own_scoping_skill():
    """Step 1 used to call another plugin's brainstorming skill. This repo
    ships its own now, so nothing under skills/ should still send an
    operator to a cross-plugin dependency. This has to be tree-wide, not
    scoped to SKILL.md: a stale reference survived in scripts/workspace.py's
    `research init` banner after SKILL.md's body was rewired, because the
    test that was supposed to catch this only ever read SKILL.md."""
    _, body = parts()
    assert "research-brainstorming" in body

    this_file = Path(__file__).resolve()
    skills_root = ROOT.parent
    for path in sorted(skills_root.rglob("*")):
        if not path.is_file():
            continue
        if path == this_file:
            # This file carries the pattern as a literal, to test for its
            # absence elsewhere in the tree. That is the test doing its
            # job, not a stale reference in skill content.
            continue
        rel_parts = path.relative_to(skills_root).parts
        if "__pycache__" in rel_parts or any(p.startswith(".") for p in rel_parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        stale = CROSS_PLUGIN_BRAINSTORMING.findall(text)
        assert not stale, f"cross-plugin brainstorming reference in {path}: {stale}"


def test_the_scoping_skill_refuses_general_brainstorming_in_its_description():
    """A skill's description is what a harness matches on. The body can say
    what it likes; if the description reads like general brainstorming, that
    is what it will be picked for."""
    front = yaml.safe_load(FORK.read_text(encoding="utf-8").split("---\n")[1])
    description = front["description"]
    assert "ONLY" in description
    assert "NOT for general brainstorming" in description


def test_the_scoping_skill_names_its_one_output():
    """This skill's whole contract with the loop is that it produces the
    `scope` block and nothing else. A draft that stops naming the three
    keys the loop actually reads, or that drifts into describing a second
    deliverable outside the Output section, silently breaks the handoff
    that SKILL.md's 'Before the loop' depends on. Checking the tokens
    appear anywhere in the file is not enough — the Output example's YAML
    already contains all four as key names, so scope the check to the
    `## Output` section itself."""
    body = FORK.read_text(encoding="utf-8")
    output_section = body.split("## Output", 1)[1].split("\n## ", 1)[0]
    for token in ("in_scope", "out_of_scope", "success_criteria", "run.yaml"):
        assert token in output_section, token


def test_the_scoping_skill_does_not_start_the_run_itself():
    """Its job ends at the scope block. If it ran `research init` the
    approval gate in SKILL.md's 'Before the loop' would be bypassed."""
    body = FORK.read_text(encoding="utf-8")
    assert "never runs `research init`" in body


def test_the_scoping_skill_does_not_forbid_what_has_already_happened():
    """SKILL.md's "Before the loop" runs `research init` FIRST and then
    this skill. The gate said "Do not run `research init`, do not write
    `run.yaml`, and do not dispatch a tick" — and by the other document's
    own ordering the first two have already happened. An agent reading
    both together cannot tell which one is wrong, and hits it on every
    run.

    Init-first is the correct order: this skill's single output is the
    scope block OF research/run.yaml, so the file must exist, and `init`
    carries the toolchain preflight that exists to land a missing
    tectonic on day zero rather than day three. So the gate is what
    changes. It has to forbid what is genuinely still ahead."""
    body = FORK.read_text(encoding="utf-8")
    gate = body.split("## The approval gate", 1)[1].split("\n## ", 1)[0]
    assert "run `research init`" not in gate
    assert "scope block" in gate


def test_both_skills_agree_that_init_comes_first():
    """The one ordering fact that has to be identical in two files, and
    was not."""
    _, body = parts()
    assert "research init" in body
    assert "after `research init`" in FORK.read_text(encoding="utf-8")
