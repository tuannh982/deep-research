import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_BLOCK = re.compile(r"^# /// script\n(.*?)^# ///$", re.MULTILINE | re.DOTALL)


def _pep723_deps(path):
    m = _BLOCK.search(path.read_text())
    assert m, f"{path} is missing its PEP 723 script block"
    block = "\n".join(
        line[2:] if line.startswith("# ") else line[1:]
        for line in m.group(1).splitlines()
    )
    return tomllib.loads(block)["dependencies"]


def test_script_deps_match_pyproject():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    declared = project["project"]["dependencies"]
    inline = _pep723_deps(ROOT / "scripts" / "research.py")
    assert sorted(inline) == sorted(declared)


def test_entrypoint_reports_version():
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "research.py"), "--version"],
        capture_output=True, text=True,
    )
    assert out.returncode == 0
    # Compare against pyproject.toml, not a literal: a literal would stay
    # green after a version bump while the CLI still reported the old one.
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert out.stdout.strip() == project["project"]["version"]
