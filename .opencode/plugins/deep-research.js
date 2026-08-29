/**
 * deep-research plugin for opencode.
 *
 * opencode loads skills natively once it knows where they are, so this does
 * exactly one thing: register this package's `skills/` directory in the live
 * config. That replaces the manual symlink an earlier generation of these
 * plugins needed.
 *
 * There is deliberately NO messages.transform hook. A plugin needs one only
 * when its bootstrap must reach the model before it knows skills exist;
 * this package has no such problem, and injecting a tool mapping into the
 * first message of every session would be noise in every unrelated task.
 * The mapping lives in skills/deep-research/references/opencode-tools.md,
 * which SKILL.md points at.
 */
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// .opencode/plugins/ -> .opencode/ -> package root -> skills/
// Resolved from import.meta.url rather than cwd, so it is correct wherever
// npm installs the package.
const skillsDir = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "skills",
);

export const DeepResearchPlugin = async () => ({
  config: async (config) => {
    config.skills = config.skills || {};
    config.skills.paths = config.skills.paths || [];
    if (!config.skills.paths.includes(skillsDir)) {
      config.skills.paths.push(skillsDir);
    }
  },
});
