# deep-research

Long-running, verifiable web research for coding agents. Accumulates
citations that are re-checked against their sources by an independent
subagent, and produces a synthesised PDF whose bibliography and appendices
are generated from the research graph rather than written by a model.

Control flow is computed by code from state on disk. The model never chooses
what to work on next.

## What is in here

| Skill | Purpose |
|---|---|
| `deep-research` | The research loop, its five gates, and the report. |
| `research-brainstorming` | Scopes a question into `run.yaml` before a run starts. Research questions only. |

## Requirements

- **`uv`** — every script runs through it; there is no binary on `PATH`.
- **`tectonic`** — builds the PDF. `research init` refuses without it so the
  failure lands on day zero; `--allow-missing-tectonic` defers it.
- The harness must grant **web search** and **web fetch**.

## Install — Claude Code

```
/plugin marketplace add path/to/plugin/deep-research
/plugin install deep-research
```

If this skill was previously installed at the old flat layout — for example
a symlink from `~/.claude/skills/deep-research` to this repo's root — remove
that symlink or copy first. Otherwise it now points at a directory with no
`SKILL.md`, and `/plugin install` leaves you with two things named
`deep-research`.

## Install — opencode

Add to the `plugin` array in your `opencode.json`:

```json
{
  "plugin": ["path/to/plugin/deep-research"]
}
```

Restart opencode. See [`docs/README.opencode.md`](docs/README.opencode.md)
for the full guide, including how the dispatch packet's Claude Code tool
names map onto opencode's.

## Layout

```
.claude-plugin/plugin.json    Claude Code manifest
.claude-plugin/marketplace.json  makes this directory addable with /plugin marketplace add
.opencode/plugins/*.js        opencode shim: registers skills/
.opencode/INSTALL.md          opencode install steps
package.json                  npm manifest for the opencode install
skills/deep-research/         the loop: scripts, agents, schemas, tests
skills/research-brainstorming/  the scoping skill
docs/                         specs, plans, execution logs
```

## Updating

`/plugin marketplace add <dir>` snapshots this directory into
`~/.claude/plugins/cache/` — it is a copy, not a live reference. Pulling new
commits does not reach the installed plugin. After pulling, run
`/plugin marketplace update deep-research` to refresh the snapshot.

## Tests

```bash
cd skills/deep-research
uv run --with pytest --with jsonschema --with publicsuffix2 --with pyyaml python -m pytest -q
```

## Status

The Claude Code path is exercised. The opencode path is written from
opencode's documented tool names, and the `config` hook's directory
registration has been checked under Node, but **the research loop has not
been run under opencode**. See the "What is verified, and what is not"
section of [`docs/README.opencode.md`](docs/README.opencode.md).
