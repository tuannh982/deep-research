# Loop protocol — the detail

`SKILL.md` has the three-line loop. This has everything it leaves out.
Read it when something is not behaving, not on every turn.

## What a tick is

`research next` computes the ready frontier from the task DAG and prints one
dispatch per task, capped by `max_parallel` (default 6). Given the same
graph, tick N always dispatches the same set — the scheduler is code, and
that is what makes the run reproducible and tolerant of a weaker model.

`research submit --tick N` reads `research/inbox/`, runs the gates, writes
accepted nodes, appends to `research/journal.jsonl`, runs `fsck`, and prints
the next action.

`research next --serial` caps the frontier at one task. Use it when
debugging; both paths share one scheduler.

## Compaction

The step packet is self-contained by construction: agent file, model, input
packet, output schema, destination path. Nothing in tick 7 depends on tick 6.

If the conversation is lost, run `research resume`. If a tick was already
dispatched and not yet submitted, `research next` reprints the *same* packet
rather than dispatching a second time — so running it twice is safe, and is
the recovery path.

## The inbox

Each subagent writes `research/inbox/<task-id>.json`. `submit` moves accepted
artifacts to `inbox/applied/` and rejected ones to `inbox/rejected/`, so a
file still sitting in `inbox/` is work that has not been accepted.

If a subagent never wrote its file, `submit` treats the task as timed out,
increments its attempts and requeues it. One hung fetch cannot stall a tick.

## The gates

Every artifact passes these before anything is written:

1. **Schema** — validated against `schemas/artifact.<kind>.json`. On failure
   the validator error goes into the journal and into the next attempt's
   prompt.
2. **Re-check** — a separate `rechecker` subagent re-reads every cited URL
   with `WebFetch` and reports, per quote, whether that exact span is on
   the page. It is dispatched on a later tick than the extraction, sees
   only the url and the spans, and is never told what any quote is meant
   to prove — so it has no stake in the answer and nothing to be led by.
   An absent span rejects the citation and quarantines the facts resting
   solely on it. A login wall, a bot block or a JS-only page is reported
   as `blocked`, which marks the citation `unverifiable` — flagged in
   Appendix D rather than trusted, and never treated as disproof. A page
   that is gone is rejected outright.

   Until its re-check lands a citation is `pending`, and a pending
   citation counts for nothing: gate 3 and the confidence arithmetic
   admit only `verified`. A run under-promotes for a tick rather than
   promoting on evidence nobody has checked.
3. **Independence** — promotion needs at least 3 verified citations across at
   least 2 distinct registrable domains, so `blog.foo.com` and `foo.com`
   count once. Failing this spawns a search task for a different source.
4. **Adversarial** — a fresh `verify` task, dispatched on a later tick, that
   sees one claim and its quotes and nothing else. A claim cannot be promoted
   without it.

   The quotes are both sides: everything in the hypothesis's `supporting`
   list and everything in its `counter` list, each labelled with which one
   it came from. So the verdict is a judgement on the balance of the
   evidence, not on the case for the claim alone. The verifier may strike
   out a supporting quote that does not do the work claimed of it; it
   cannot strike out a counter quote, and one named is discarded.

   A `contradicted` verdict refutes the claim and cascades. A live counter
   citation on its own does not — it marks the claim `contested`, which is
   computed by code and re-evaluated on every submit, so a dispute that
   arrives days after the verifier ran still shows up.

## What a chapter is

A top-level section is a **line of enquiry** — a theme the decomposer
proposed, a child of the run's seeded root.

A finding is filed under the enquiry that produced **its evidence**, not
under the hypothesize round that happened to propose it. Those rounds are
scheduled on the run root, so they resolve to themselves as themes, and
filing by them put chapters named after scheduler bookkeeping in the
report — one per round, each holding the same claim. A claim drawing on
two enquiries lands in whichever supplied most of its support;
counter-evidence does not vote, so a refutation cannot relocate the
finding it challenges. Refutation evidence is filed with the claim it
challenges.

A `hypothesize` task can never title a chapter.

## Evidence already gathered counts

A claim **accumulates** evidence across rounds rather than forking. The
hypothesizer runs again each time a branch gathers more facts, and when
it re-proposes a claim it already made, the supporting and counter lists
are merged into the existing node instead of a second node being written.

Claims are keyed by the claim itself, run-wide. The hypothesizer sees
every fact in the run and one round is scheduled at a time, so the same
sentence is the same finding wherever its evidence came from.

Merging never promotes. Confidence is recomputed from the fuller
evidence, but promotion still waits for a fresh adversarial verdict —
which is why a `verify` task is seeded on every round, not only the
first.

The hypothesizer's packet is **run-wide**, not theme-scoped: a fact
extracted under one theme can support a claim under another. It is
capped at 40 facts, and that budget is spent on facts nothing has used
yet — a fact whose citation already supports or opposes some claim is
the one least worth showing again.

## Looking for disconfirmation

Gate 4 weighs counter-evidence, but for a long time nothing went out to
find any — the only hypothesis-driven search fired when gate 3 failed
and asked for *more support*. So `supported` meant "three quotes nobody
sought the contrary of".

Once a claim is promoted (`supported` or `contested`), the loop schedules
a **refute search**: an ordinary `search` task carrying
`inputs.stance: "against"`, whose question is written by code — *"find
evidence that would show this claim is false"* — not chosen by a model.
Its extracted citations attach directly to that claim's `counter` list,
and a live counter citation demotes `supported` to `contested` on the
next submit, automatically.

Unpromoted claims are not challenged. Nothing in the report rests on
them, and a challenge costs a full search → extract → re-check cycle.

**Coverage will not halt while a promoted claim is unchallenged.** A
refute search that ran and found nothing counts as challenged — the
claim survived a search for its opposite, which is a result — as does
one abandoned after `max_attempts` or stranded past the depth cap.
Anything else would livelock: the task is `done`, nothing would reuse
it, and no further work would ever be scheduled.

The requirement does not apply once the phase is `synthesize`. `submit`
stops all follow-on scheduling there, so a claim that reached synthesis
unchallenged could never acquire a challenge, and requiring one would
make the report unrenderable.

Appendix A distinguishes "searched for, none found" from "not searched
for", and Limitations counts any promoted claim that was never
challenged.

## What the report tells a reader about its own evidence

Two things are recorded so the work can be judged, and **neither is acted
on** — no gate, threshold or scheduling decision reads either.

**When a source was published.** The extractor reports the page's own
publication date, or `null`. Partial dates are legal (`2019`, `2019-03`)
because many pages give no more, and a date the model inferred is a date
it invented. The bibliography and Appendix D print it, and say `Undated`
out loud rather than omitting the clause. Note that `Retrieved` is a
different thing: it is written by the re-check, so it records when *this
run* last read the page.

**What was searched for.** Every `search` artifact carries the queries the
searcher reports having issued, including when it found nothing — that is
the case where "the question is exhausted" and "we asked badly" most need
telling apart. Appendix E lists them, marks refute searches as such, and
states that they are self-reported: nothing in this process observes the
`WebSearch` call.

## Opposition counts against a claim

Live counter-evidence lowers a claim's score, in proportion to how much
of its evidence argues against it. Enough of it drops the claim below
`promotion_threshold`, and `recompute_confidence` demotes it out of the
findings — it is still reported, but as an open question rather than as
something the report stands behind.

The bar is sharp at the bottom end: a claim carrying exactly gate 3's
minimum is demoted by a **single** live counter citation. That is
deliberate. Minimum evidence plus live opposition is not a finding.

`contested` therefore means something specific now — evidence strong
enough to absorb the dispute and still clear the bar. It takes roughly
twice the minimum evidence to reach.

When counter-evidence attaches to a claim, a fresh `verify` task is
seeded so the adversarial checker sees the new balance. Before this it
saw a claim exactly once, when the hypothesizer proposed it, and a
refute search's findings never reached it.

A third independent source **does** now raise the score, and volume
within sources does not: the score is
`min(1, citations/min_citations) x distinct/(distinct+1)`. Ten quotes
from two sites are still two sources. The two terms are calibrated so
gate 3's minimum scores exactly `promotion_threshold` (0.67 by default)
and an unverified claim's ceiling — weight 0.5 — stays strictly below
it, which is what makes gate 4 arithmetically unavoidable.

**What gate 3 does not measure.** It counts distinct registrable
domains, and two of those can still be one source: a syndicated release,
or two posts citing one paper. Detecting that needs origin
identification, which is fuzzy and unreliable, and requiring a primary
source instead would stall any question whose honest literature is all
secondary. So the run records whether each source presents its own work
(`source_type`) and **discloses** it — Appendix A marks a claim with no
primary source identified — rather than gating on it. `unknown` is kept
distinct from `secondary`: it means nobody could tell.

## How a claim's strength is reported

Each hypothesis carries a `confidence` score — `base × spread × weight`
over its live verified citations, computed in `confidence.py`, never set
by a model. It is a **gating quantity**: it decides promotion against
`promotion_threshold`, and it is what a `min_hypothesis_confidence` stop
predicate reads.

It is deliberately **not in the report**. It is not a probability — it
saturates below 1.0, and the ordinary promoted claim (3 verified
citations across 2 registrable domains, verdict `supported`) scores
exactly 0.60. Printed beside a claim with no formula next to it, 0.60
reads as "60% likely", which is not what it means.

Appendix A carries what that number was standing in for, and all of it
can be checked: the status as a word, what the adversarial verifier
concluded, the verifier's own reasoning in its own words, and the
citation ids on each side — `Against: none` included, because whether
anything argues against a claim is information about that claim.

The section writers are given the same thing: `status`, `verdict` and
`disputed`, not a score.

## Halting

`next` checks three conditions before computing a frontier. There is no
budget condition; the loop runs indefinitely until the graph or the user
stops it.

- **signal** — the user asked, via `research signal stop`, or a conditional
  stop they confirmed.
- **coverage** — nothing dispatchable remains, and every claim that could
  still be evidenced has been. A claim that failed gate 3 whose
  evidence-seeking search has already run and found nothing more is a
  finding, listed under Open questions in `status.md`, not a reason to
  keep going: there is nothing left that could change it.
- **saturation** — the last 6 completed tasks, across at least 2 themes,
  produced no new facts and no new domains. "Theme" is a top-level branch
  of the DAG — a child of the seeded root task, which is what becomes a
  top-level section of the report — not the seeded root itself, which
  every task in the run descends from.

Any of them writes `research/out/status.md` and stops. `research continue`
clears it.

Every 25 ticks the loop prints a one-line digest. It is a notice, not a
gate — it exists so the user can decide whether to signal stop.

## Stop signals

```
research signal stop
research signal stop-when --json '{"all": [
    {"branch": "T-004", "tasks_resolved": true},
    {"branch": "T-004", "min_hypothesis_confidence": 0.7}]}'
research signal checkpoint --note "ask me before going wider"
```

A conditional stop must compile to a predicate over the graph. The legal
conditions are `tasks_resolved`, `min_hypothesis_confidence`, `min_facts`,
`min_domains` and `min_supported_hypotheses`, each optionally scoped to a
`branch`. Anything that cannot compile — "stop when it feels complete" — is
refused and becomes a checkpoint instead.

Setting a conditional stop registers a confirmation checkpoint. Echo the
printed predicate back to the user, get an explicit yes, then
`research continue`. The loop will not dispatch until you do.

A pending checkpoint pauses the loop at the next `next`. Ask the user, then
`research continue`.

Plain `research continue` clears a halt and any pending checkpoint, but a
confirmed conditional stop otherwise survives it — the predicate is still
armed. If that predicate is what caused the halt, the very next `next`
re-evaluates it and halts again, unblockably, unless you pass
`research continue --clear-stop-when` to withdraw the predicate itself.

## When something is wrong

```
research fsck
```

Revalidates every node against its schema and every cross-reference.
Reporting only — repair is manual by design, because a corrupted research
graph should be looked at, not silently rewritten. `submit` runs it at the
end of every tick.

If a tick seems wedged, re-run the same `research submit --tick N`. It is
idempotent: already-applied artifacts are skipped and re-application
converges rather than duplicating. Re-running it is the crash-recovery
path — a task whose artifact the journal already records as applied is
finished off by the re-run rather than re-applied, so a tick interrupted
anywhere completes on the retry.

If `next` prints a SKIPPED list and dispatches nothing, still run the
`research submit --tick N` it prints. That submit is what charges an
attempt against each skipped task; after `max_attempts` of it they are
abandoned and the run moves on rather than reprinting the same
undispatchable frontier forever.

## Toolchain

`research init` refuses to start unless `tectonic` is on PATH — pass
`--allow-missing-tectonic` to proceed anyway and be refused later, at the
render step, instead — so that failure lands on day zero, not day three.

## Gate 5 — generated prose

Every section body a synthesizer returns is checked before it reaches
`sections/`:

- every `\cite{}` key must be in that section's allowed set **and** still
  citable in the graph — a citation rejected since the section was seeded
  fails here
- every `\factref{}` id must resolve to an active fact — one quarantined by
  a cascade fails here
- every sentence stating a figure must carry a `\cite{}` or a `\factref{}`

A failure rejects the artifact and re-dispatches the section with the
reasons attached, exactly like gate 1. Three failures abandon it.

Escaping happens once, in code, after gate 5 passes. Nothing asks a model
to escape LaTeX.

## The synthesis phase

`research synthesize` seeds one `outline` task and moves the run to the
`synthesize` phase. From there the ordinary loop applies — `next` dispatches
the outliner, `submit` validates its arrangement against the computed
outline and seeds one `synthesize` task per section plus one for the
cross-cutting Synthesis. The outliner may reorder, retitle and reassign; it
cannot drop, duplicate or invent a finding.

While the phase is `synthesize`, `submit` skips its follow-on scheduling —
no new evidence-seeking searches, no new hypothesis tasks. The outline was
computed and frozen when `research synthesize` ran, and `apply_outline`
validates the outliner's answer against that frozen copy, so a finding that
arrives later cannot reach any section: it would land in Appendix A with no
chapter discussing it while Limitations counted it from the stale view.

Confidence recomputation is unaffected — a `verify` artifact applied during
synthesis still moves its hypothesis's score, so Appendix A cannot
contradict the body.

To resume research, `research continue` lifts the freeze — it puts the phase
back to `research`, which is what makes `submit` schedule follow-on work
again — and a fresh `research synthesize` then recomputes the outline over
everything found since. `continue` says so in its `cleared:` line. It will
not reopen a run whose phase is already `done`; that report is written.

## Recovering a failed build

`research render` writes `out/report.tex` before it invokes `tectonic`, so a
failed build still leaves the complete source. The diagnosis is in
`out/build-report.md`.

If the error can be attributed to one section, that section's writer is
re-opened with the error in its inputs — run `research next` and
`research submit`, then render again. `max_attempts` bounds this; it cannot
loop forever. If no section can be blamed, the fault is in the template or
an appendix, and the build report is the place to start.

## A section that cannot be written

A writer abandoned after `max_attempts` never re-enters the frontier, so it
would otherwise block the report for good. Instead that section is typeset
as a one-line placeholder saying it could not be written and naming the
attempt count; the abandoned task itself is in Appendix C. The rest of the
report renders normally. Nothing else in the document is affected.

## A citation rejected after its section was accepted

Gate 5 checks cite keys when a section is submitted, and the graph keeps
moving afterwards. Synthesis schedules no *new* research — see above — but
the tasks that were already outstanding when the outline froze are still
dispatched, because the freeze is on follow-on scheduling, not on the
frontier. So a re-check seeded before the freeze can still land and reject
a citation after a section citing it was already accepted, and a `verify`
refutation can quarantine the facts under one. The bibliography would then
drop the citation while the section still cites it, and the PDF would ship
a dangling `[?]` against an Appendix D entry reading "nothing in this
report rests on it".

`research render` refuses instead, names the sections and citations
involved, and re-opens those writers with the reason attached — run
`research next` and `research submit` to have the claims dropped or
re-evidenced, then render again.

`research render --tex-only` assembles the source without building, which is
how to inspect the LaTeX on a machine with no `tectonic`.
