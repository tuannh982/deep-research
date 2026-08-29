---
name: research-brainstorming
description: Use ONLY when scoping a question for a deep-research run, immediately after `research init` and before the first tick. Produces the scope block of run.yaml. NOT for general brainstorming, feature design, architecture, or planning — use a general-purpose brainstorming skill for those.
---

# Research Brainstorming

Turn a research question into a scope the run can hold itself to.

You are invoked **after `research init`** has run. The workspace exists and
`research/run.yaml` is already on disk, carrying an empty `scope` block that
is waiting for you. Nothing has been dispatched yet.

This skill has exactly one output: the `scope` block of `research/run.yaml`
— `in_scope`, `out_of_scope`, `success_criteria`. It is not a general
brainstorming skill and cannot stand in for one. If you are designing a
feature, planning a change, or exploring an idea that is not a research
question, stop and use a general-purpose brainstorming skill instead.

**Announce at start:** "Using research-brainstorming to scope this question."

## Why this is not optional

The research loop has **no budget condition**. It halts when the user says
so, when coverage is exhausted, or when it saturates — never when it has
spent enough. The scope block is what the decomposer argues each child task
against, and what the report's Introduction and Limitations are generated
from. An empty scope is an unbounded run.

## The process

Ask **one question per message**. Prefer multiple choice. Stop asking when
you could write all three lists without guessing.

1. **Read the question back.** State in one sentence what you think is being
   asked, and ask whether that is right. A question that means two things
   produces a report about neither.
2. **Establish what would count as an answer.** Not "learn about X" — what
   should the reader be able to decide, or state, that they cannot now?
3. **Find the boundary.** What is adjacent, tempting, and not wanted? This
   is the list people skip and the one that stops a run wandering. Then
   apply YAGNI to `in_scope` itself: cut anything the question does not
   need, even if related and interesting. An in-scope list that grows to
   cover everything adjacent is how a run with no budget halt becomes
   unbounded.
4. **Check the evidence bar.** The defaults are 3 verified citations across
   2 registrable domains. Ask whether this question needs more, or whether
   the subject is thin enough that fewer is honest.
5. **Present the scope block** and get an explicit yes.

## The approval gate

Present the three lists in chat, as YAML, exactly as they will be written.
Then **stop**. Do not write the scope block into `run.yaml`, and do not
dispatch a tick, until your human partner says yes.

## Output

Write the agreed lists into the `scope` block of `research/run.yaml`:

```yaml
scope:
  in_scope:
    - "how Rayleigh scattering produces the daytime sky's colour"
  out_of_scope:
    - "colour perception in non-human species"
  success_criteria:
    - "a mechanism a non-specialist can follow, carrying at least three
       independently verified sources"
```

Then hand back. This skill **never runs `research init`** and never
dispatches a research tick — `SKILL.md`'s "Before the loop" owns what
happens next, including showing the user the task tree before a long run.

## Red flags

| Thought | Reality |
|---------|---------|
| "The question is clear enough, skip the scoping" | An unscoped question is what makes a run wander for days. |
| "The decomposer will work out the boundaries" | It proposes children *against the scope block*. Empty scope means unbounded breadth. |
| "Success criteria can be decided later" | There is no budget halt. They are how anyone knows when to stop. |
| "Out-of-scope is obvious" | Obvious to you, invisible to the searcher. Write it down. |
| "This is a design question, but it is brainstorming-ish" | This skill only scopes research questions. Use a general-purpose brainstorming skill. |
| "I will write run.yaml now and confirm afterwards" | The gate is the approval, not the draft. Present, then stop. |
