import json

import pytest

import memory as memory_mod
import outline
import render
import research
import runconfig
import stubs
import workspace


@pytest.fixture
def ready(tmp_path, monkeypatch):
    root = tmp_path / "research"
    workspace.init(root, "why is the sky blue?",
                   which=lambda name: f"/usr/bin/{name}")
    store = memory_mod.Memory(root)
    citation = store.create("citation", {
        "url": "https://a-example.com/p", "domain": "a-example.com",
        "title": "t", "quote": "short wavelengths scatter",
        "quote_sha256": "0" * 64, "fetched_at": "2026-08-22T10:00:00Z",
        "http_status": 200, "status": "verified",
        "provenance": {"task": None, "agent": "extractor"}})["id"]
    accepted = {"question": "why is the sky blue?",
                "sections": [{"id": "S-001", "theme": "T-001",
                              "title": "Optical scattering",
                              "hypotheses": [], "facts": []}],
                "orphans": {"hypotheses": [], "facts": []},
                "empty_themes": []}
    (root / "out" / outline.PATH_NAME).write_text(json.dumps(accepted),
                                                  encoding="utf-8")
    (root / "sections" / "S-001.tex").write_text(
        "Short wavelengths scatter \\cite{%s}." % citation, encoding="utf-8")
    (root / "sections" / "S-999.tex").write_text(
        "The themes agree.", encoding="utf-8")
    monkeypatch.setattr(render, "_tectonic_run", stubs.tectonic_stub())
    return root


def _run(root, *argv):
    parser = research.build_parser()
    args = parser.parse_args(["render", "--root", str(root), *argv])
    return render.run(args)


def test_render_produces_a_pdf(ready, capsys):
    assert _run(ready) == 0
    assert (ready / "out" / "report.pdf").read_bytes().startswith(b"%PDF-")
    assert "report.pdf" in capsys.readouterr().out


def test_render_moves_the_run_to_done(ready):
    _run(ready)
    assert runconfig.load(ready)["status"]["phase"] == "done"


def test_a_failed_build_returns_one_and_leaves_the_tex(ready, monkeypatch,
                                                       capsys):
    monkeypatch.setattr(render, "_tectonic_run",
                        stubs.tectonic_stub(fail=True))
    assert _run(ready) == 1
    assert (ready / "out" / "report.tex").is_file()
    assert "build-report.md" in capsys.readouterr().err


def test_a_failed_build_does_not_move_the_run_to_done(ready, monkeypatch):
    monkeypatch.setattr(render, "_tectonic_run",
                        stubs.tectonic_stub(fail=True))
    _run(ready)
    assert runconfig.load(ready)["status"]["phase"] != "done"


def test_render_refuses_when_tectonic_is_missing(ready, capsys):
    cfg = runconfig.load(ready)
    cfg["preflight"]["tectonic"] = "missing"
    runconfig.save(ready, cfg)
    assert _run(ready) == 1
    assert "tectonic" in capsys.readouterr().err


def test_render_refuses_before_synthesis_has_run(tmp_path, capsys):
    root = tmp_path / "research"
    workspace.init(root, "q", which=lambda name: f"/usr/bin/{name}")
    assert _run(root) == 1
    assert "research synthesize" in capsys.readouterr().err


def test_render_names_a_section_that_has_not_been_written(ready, capsys):
    (ready / "sections" / "S-001.tex").unlink()
    assert _run(ready) == 1
    assert "S-001" in capsys.readouterr().err


def test_render_is_registered_as_a_command():
    assert research.COMMANDS["render"] is render


def test_status_points_at_synthesize_after_a_halt(ready, capsys):
    """A halted run whose next step is synthesis should say so. Otherwise
    the operator's only documented options are `continue` and stopping."""
    cfg = runconfig.load(ready)
    # run.json requires at_tick/at alongside reason/detail (see
    # halt.record); a literal missing them would fail runconfig.save's own
    # schema validation before status is ever exercised.
    cfg["status"]["halted"] = {"reason": "coverage", "detail": "done",
                               "at_tick": 0, "at": "2026-08-22T10:00:00Z"}
    runconfig.save(ready, cfg)
    parser = research.build_parser()
    args = parser.parse_args(["status", "--root", str(ready)])
    research.COMMANDS["status"].run(args)
    assert "research synthesize" in capsys.readouterr().out


def test_status_points_at_render_during_the_synthesize_phase(ready, capsys):
    cfg = runconfig.load(ready)
    cfg["status"]["phase"] = "synthesize"
    runconfig.save(ready, cfg)
    parser = research.build_parser()
    args = parser.parse_args(["status", "--root", str(ready)])
    research.COMMANDS["status"].run(args)
    assert "research render" in capsys.readouterr().out


def test_status_also_offers_continue_during_the_synthesize_phase(ready,
                                                                 capsys):
    """The synthesize branch offered only "run the loop until it halts,
    then `research render`", which was coherent while `research continue`
    could not reopen research -- it would have pointed at a command that
    did nothing to the freeze. Now that it can, leaving it out is the
    omission: `research render` is not the only door out of synthesis."""
    cfg = runconfig.load(ready)
    cfg["status"]["phase"] = "synthesize"
    runconfig.save(ready, cfg)
    parser = research.build_parser()
    args = parser.parse_args(["status", "--root", str(ready)])
    research.COMMANDS["status"].run(args)
    out = capsys.readouterr().out
    assert "research render" in out
    assert "research continue" in out
