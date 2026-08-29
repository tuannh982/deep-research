---
name: deep-research
description: Use when the user wants open-ended research over the public web that runs for hours or days, accumulates verified citations, and produces a synthesised report.
---

# Deep Research

Long-running, verifiable web research. Control flow is computed by code from state on disk — you never choose what to work on next.

Throughout, **`research`** means:

    uv run <this skill dir>/scripts/research.py <command> --root ./research

`<command>` first, `--root ./research` right after it, before that
command's own arguments (for `signal`, before its stop/stop-when/
checkpoint choice). Run from the directory that holds `./research/`; there
is no binary on PATH.

## The loop

1. If the user sent a message this turn, translate it with `research signal`
   first — `stop`, `stop-when --json '{...}'`, or `checkpoint --note '...'`.
2. Run `research next`. Dispatch every subagent it lists **in parallel, in
   one message**, exactly as printed: that agent file, that model, that
   input, writing to that path.
3. Run `research submit --tick N`. Go back to step 1.

That is the whole loop; `next` prints everything a tick needs, so nothing you do depends on remembering the previous one.

## Before the loop

1. **First**, `research init "<the question>"` — workspace, toolchain
   check, seeds `T-001`, writes `run.yaml` with an empty `scope`.
2. Then run the **deep-research:research-brainstorming** skill against the
   question. Write the agreed in-scope, out-of-scope and success criteria
   into that `scope` block. `next` refuses the first tick without it.
3. Run one turn of the loop — its first tick dispatches the decomposer
   against `T-001` — then **show the user the resulting task tree and let
   them prune it** before spending days on it.
4. Then run the loop until it halts.

## When it stops

`next` prints `HALT(reason)` and writes `research/out/status.md` — nothing is lost, and the user may come back days later.

    research status      where the run is, one screen
    research continue    clear the halt and keep going

`HALT` is not an error. Show the user `status.md` and ask what to do: keep
researching, stop there, or write the report.

## The report

When the loop halts and the findings are ready, two commands turn them into
a PDF:

    research synthesize   compute the outline, seed the section writers
    research render       assemble report.tex and build out/report.pdf

`synthesize` does not write anything. It seeds one task, and the **same
three-line loop above** dispatches the outliner and then one writer per
section. When the loop halts again, run `research render`.

From that point the loop **stops gathering evidence** and dispatches only
synthesis work. Deciding to write the report is the decision to stop
researching: anything found afterwards could not appear in it, because the
outline is fixed when `synthesize` runs. To pick up where you left off,
`research continue` and then `research synthesize` again.

The outline, the bibliography and Appendices A–D are computed from the
graph, not written by a model. A section body that cites a source it was
not given is rejected the same way any other artifact is.

If the build fails, nothing is lost — recovering it is in `references/loop-protocol.md`'s "Recovering a failed build".

## If you lose the thread

A compaction can wipe this conversation. Recovery is one command:

    research resume

It reprints this loop and says exactly where the run is. Run it and carry on; do not try to reconstruct anything from memory.

## Rules

- Never edit anything under `research/memory/` by hand. `memory.py` is the
  only writer, and hand edits are exactly the corruption `research fsck`
  exists to report.
- Never invent a task, a citation id, or a next step. If `next` did not
  print it, it is not part of this run.
- Dispatch the subagents exactly as printed. Do not add context, do not
  merge two into one, do not substitute a different model.
- A rejected artifact is normal. `next` re-emits the task with the validator
  error attached. Three failures abandon it and the loop moves on.
- This run needs the harness to grant **`WebSearch`** and **`WebFetch`**.
  The searcher finds sources with one and the extractor and rechecker read
  them with the other; `init` cannot check for them because they are not
  programs on `PATH`. Without them every search and every re-check fails,
  three attempts each, and the run abandons its way to an empty report.
- Not on Claude Code? The dispatch packet names its tools and models.
  `references/opencode-tools.md` translates them.

Detail — gates, halt conditions, checkpoints, recovering a wedged tick:
`references/loop-protocol.md`.
