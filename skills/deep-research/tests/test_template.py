import re
from pathlib import Path

TEMPLATE = (Path(__file__).resolve().parents[1] / "templates" / "report.tex")

MARKERS = ("%%TITLE%%", "%%DATE%%", "%%INTRODUCTION%%", "%%SECTIONS%%",
           "%%SYNTHESIS%%", "%%LIMITATIONS%%", "%%BIBLIOGRAPHY%%",
           "%%APPENDICES%%")


def test_the_template_ships_with_the_skill():
    assert TEMPLATE.is_file()


def test_the_template_carries_every_marker():
    text = TEMPLATE.read_text(encoding="utf-8")
    for marker in MARKERS:
        assert marker in text, f"template is missing {marker}"


def test_the_template_defines_factref():
    """\\factref is this project's invention. If the preamble does not
    define it, every section using one fails with 'Undefined control
    sequence' — and gate 5 actively encourages their use."""
    assert "\\newcommand{\\factref}" in TEMPLATE.read_text(encoding="utf-8")


def test_the_template_opens_and_closes_its_document():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert text.count("\\begin{document}") == 1
    assert text.count("\\end{document}") == 1


def test_the_template_needs_no_package_outside_a_base_tex_install():
    """tectonic downloads what it needs, but a package that does not exist
    fails the build with a network error that reads like a bug in us."""
    allowed = {"inputenc", "fontenc", "geometry", "hyperref", "url",
               "parskip", "microtype"}
    used = set(re.findall(r"\\usepackage(?:\[[^\]]*\])?\{([^}]*)\}",
                          TEMPLATE.read_text(encoding="utf-8")))
    assert used - allowed == set()


def test_render_substitutes_every_marker_the_template_carries():
    """The reverse of test_the_template_carries_every_marker. A marker the
    template has that render never replaces ships literally into the PDF."""
    import render
    assert set(MARKERS) == set(render.MARKERS)
