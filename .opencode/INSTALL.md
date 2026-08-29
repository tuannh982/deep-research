# Installing deep-research for opencode

This package is not on npm, so opencode installs it from a clone:

```bash
git clone https://github.com/tuannh982/deep-research
```

Add that clone's absolute path to the `plugin` array in your `opencode.json`
— global or project-level:

```json
{
  "plugin": ["/absolute/path/to/your/clone/deep-research"]
}
```

Restart opencode. The plugin registers both skills:

- `deep-research` — the research loop itself
- `research-brainstorming` — scopes a question before a run starts

Verify by asking: **"What deep-research skills do you have?"**

This is written from opencode's documented plugin behaviour, not exercised
end-to-end — nobody has watched opencode load a skill from this package.

Full guide, including what is and is not verified:
[`docs/README.opencode.md`](../docs/README.opencode.md).
