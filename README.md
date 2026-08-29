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
/plugin marketplace add tuannh982/deep-research
/plugin install deep-research
```

If this skill was previously installed at the old flat layout — for example
a symlink from `~/.claude/skills/deep-research` to this repo's root — remove
that symlink or copy first. Otherwise it now points at a directory with no
`SKILL.md`, and `/plugin install` leaves you with two things named
`deep-research`.

## Install — opencode

This package is not on npm, so opencode installs it from a clone:

```bash
git clone https://github.com/tuannh982/deep-research
```

Then point the `plugin` array in your `opencode.json` at that clone, by
absolute path:

```json
{
  "plugin": ["/absolute/path/to/your/clone/deep-research"]
}
```

Restart opencode. See [`docs/README.opencode.md`](docs/README.opencode.md)
for the full guide, including how the dispatch packet's Claude Code tool
names map onto opencode's, and what has and has not been verified.

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

`/plugin marketplace add` snapshots the source into
`~/.claude/plugins/cache/` — it is a copy, not a live reference. New commits
on GitHub do not reach the installed plugin on their own. Run
`/plugin marketplace update deep-research` to refresh the snapshot.

On opencode the plugin is loaded from your clone, so `git pull` in the clone
and restart opencode.

## Tests

```bash
cd skills/deep-research
uv run --with pytest --with jsonschema --with publicsuffix2 --with pyyaml python -m pytest -q
```
