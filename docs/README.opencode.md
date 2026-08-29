# deep-research for opencode

## Installation

This package is not published to npm, so opencode installs it from a clone:

```bash
git clone https://github.com/tuannh982/deep-research
```

Then add that clone's absolute path to the `plugin` array in your
`opencode.json` — global or project-level:

```json
{
  "plugin": ["/absolute/path/to/your/clone/deep-research"]
}
```

The path must be absolute, and must point at the repo root — the directory
holding `package.json`. The plugin resolves `skills/` relative to its own
file, so copying `.opencode/plugins/deep-research.js` somewhere else on its
own will not work; opencode has to load it from inside the clone.

Restart opencode. The plugin registers this repo's `skills/` directory with
opencode's `config` hook, so opencode's own skill loader can find both
skills in it — see "What is verified, and what is not" below for what has
actually been run.

## Usage

Two skills install together:

- **`deep-research`** — the research loop. Long-running, gate-checked web
  research that produces a cited PDF.
- **`research-brainstorming`** — scopes a question into `run.yaml` before a
  run starts. It is for research questions only; it is not a general
  brainstorming skill.

Verify with: **"What deep-research skills do you have?"**

To start a run, ask for deep research on a question. The skill's own
`SKILL.md` carries the three-line loop.

## Requirements

- **`uv`** on `PATH` — every script runs through it.
- **`tectonic`** on `PATH` — builds the PDF. `research init` refuses without
  it, so the failure lands on day zero rather than day three. Pass
  `--allow-missing-tectonic` to defer it to the render step.
- The harness must grant **web search** and **web fetch**. Three of the eight
  agents need them. Without them every search and every re-check fails three
  times and the run abandons its way to an empty report.

## Updating

The plugin is installed from a path. Pull the repo and restart opencode.

## How it works

`.opencode/plugins/deep-research.js` adds this package's `skills/` directory
to `config.skills.paths` through opencode's `config` hook. That is all it
does — opencode's own skill loader takes it from there.

There is no `messages.transform` hook, deliberately. Injecting this skill's
tool mapping into the first message of every session would be noise in every
unrelated task, and neither harness needs a bootstrap to discover a skill by
its description.

## Tool mapping

`research next` prints a dispatch packet naming Claude Code's concepts,
because it is generated from `run.yaml` by a function whose output shape is
pinned by tests. The translation lives in
[`skills/deep-research/references/opencode-tools.md`](../skills/deep-research/references/opencode-tools.md).

## What is verified, and what is not

Stated plainly, because it matters more here than convenience does.

**Verified:** the marketplace manifest validates (`claude plugin validate`
run on the repo root validates `marketplace.json`; run directly against
`plugin.json` it also passes); the plugin's `config` hook registers this
package's `skills/` directory, checked by running the shim under Node; the
Python suite passes from `skills/deep-research/`.

**Not verified:** that the research loop *runs* under opencode. The tool
mapping is written from opencode's documented tool names, not from a run we
performed. Registering the directory is a much smaller claim than either
opencode actually loading a skill from it or the loop behaving equivalently
— and neither of those has been observed.

**Also not verified:** the install itself. opencode documents the `plugin`
array as taking npm package names; an absolute local path is not in the
documented set. It is the only route available while this package is off
npm, but if your version of opencode rejects it, that is why.

## Troubleshooting

**Plugin not loading** — check the path in `opencode.json` is absolute and
points at the repo root, the directory holding `package.json`.

**Skills not found** — the plugin registers `<package>/skills`. Confirm that
directory exists and contains `deep-research/SKILL.md`.

**`research init` refuses to start** — it preflights `uv` and `tectonic`.
Install the one it names, or pass `--allow-missing-tectonic`.

**Every search fails** — the harness has not granted web search. `init`
cannot check for it, because it is not a program on `PATH`.

## Getting help

Open an issue at
[github.com/tuannh982/deep-research](https://github.com/tuannh982/deep-research/issues).
Include the output of `research status` and `research fsck` from the
affected run — gather both before filing.
