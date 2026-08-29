"""The manifests that make this repo installable, and the wiring between
them.

Nothing here is exercised by running the research loop, so nothing else in
the suite would notice if a manifest drifted, a version fell out of step, or
a skill directory lost its SKILL.md. These are cheap and they are the only
guard.
"""
import json
import tomllib
from pathlib import Path

import pytest
import yaml

# tests/ -> skills/deep-research/ -> skills/ -> repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]


def test_the_repo_root_is_where_we_think_it_is():
    """The only path in this suite that reaches above the skill directory.

    If the layout moves again, parents[3] lands somewhere else and every
    other assertion in this file passes against the wrong tree — green, and
    checking nothing. Fail here instead.
    """
    assert (REPO_ROOT / ".claude-plugin").is_dir(), REPO_ROOT
    assert (REPO_ROOT / "skills" / "deep-research").is_dir(), REPO_ROOT


def _plugin_manifest():
    return json.loads(
        (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))


def _package_manifest():
    return json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))


def _pyproject():
    return tomllib.loads(
        (REPO_ROOT / "skills" / "deep-research" / "pyproject.toml")
        .read_text(encoding="utf-8"))


def test_the_claude_plugin_manifest_parses_and_is_complete():
    manifest = _plugin_manifest()
    assert manifest["name"] == "deep-research"
    assert manifest["description"].strip()
    assert manifest["version"].strip()


def test_the_claude_manifest_states_no_url_it_cannot_honour():
    """There is no git remote. A homepage or repository field pointing at a
    URL that 404s is worse than an absent field — it looks authoritative."""
    manifest = _plugin_manifest()
    assert "homepage" not in manifest
    assert "repository" not in manifest


def test_the_marketplace_manifest_lists_this_plugin():
    """`plugin.json` describes the plugin; `marketplace.json` is what makes
    the directory addable with `/plugin marketplace add <path>`. Having only
    the first is a manifest set that looks complete and cannot be installed."""
    marketplace = json.loads(
        (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    entries = {entry["name"]: entry for entry in marketplace["plugins"]}
    assert "deep-research" in entries, sorted(entries)
    assert entries["deep-research"]["source"] == "./"
    assert entries["deep-research"]["version"] == _plugin_manifest()["version"]


def test_the_package_manifest_parses_and_is_complete():
    manifest = _package_manifest()
    assert manifest["name"] == "deep-research"
    assert manifest["type"] == "module"
    assert manifest["main"]


def test_the_package_manifest_adds_no_npm_dependency():
    """The shim adds no npm dependency — a stated global constraint of this
    work that, until now, rested on nobody adding one rather than on
    anything checking for it."""
    assert "dependencies" not in _package_manifest()


def test_the_package_main_points_at_a_file_that_exists():
    """opencode loads `main`. A path that drifted from the file it names
    fails at install time, in someone else's terminal, with no test here
    having noticed."""
    main = _package_manifest()["main"]
    assert (REPO_ROOT / main).is_file(), main


def test_the_three_manifests_agree_on_the_version():
    """Three files carry a version and nothing else makes them agree."""
    assert (_plugin_manifest()["version"]
            == _package_manifest()["version"]
            == _pyproject()["project"]["version"])


def _skill_dirs():
    """Every real skill directory under skills/.

    Dot-directories and __pycache__ are excluded deliberately: a stray
    .venv or .pytest_cache under skills/ would otherwise be parametrized
    as a skill and fail for having no SKILL.md, which is a false alarm
    about build detritus rather than a finding about the plugin.
    """
    return sorted(
        path for path in (REPO_ROOT / "skills").iterdir()
        if path.is_dir()
        and not path.name.startswith(".")
        and path.name != "__pycache__"
    )


def test_there_is_more_than_one_skill():
    """Guards the discovery below: if `skills/` were empty or held one
    entry, the parametrized tests would pass by iterating nothing."""
    assert len(_skill_dirs()) >= 2


@pytest.mark.parametrize("skill_dir", _skill_dirs(), ids=lambda p: p.name)
def test_every_skill_directory_has_a_skill_file(skill_dir):
    assert (skill_dir / "SKILL.md").is_file()


@pytest.mark.parametrize("skill_dir", _skill_dirs(), ids=lambda p: p.name)
def test_every_skill_declares_a_name_and_a_description(skill_dir):
    """A skill with no description is a skill no harness will ever pick."""
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    front = yaml.safe_load(text.split("---\n")[1])
    assert front["name"] == skill_dir.name
    assert front["description"].strip()


def test_the_opencode_guide_exists():
    """.opencode/INSTALL.md points readers at it; a dead pointer is how an
    install guide quietly rots."""
    assert (REPO_ROOT / "docs" / "README.opencode.md").is_file()
