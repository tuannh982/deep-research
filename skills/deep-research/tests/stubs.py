"""Test doubles for things the suite must never really run."""
from dataclasses import dataclass

# The smallest byte sequence that is recognisably a PDF. The smoke test
# asserts a PDF LANDS — that the pipeline wrote one where it said it
# would. Whether the LaTeX is valid is tectonic's job, and is checked by
# the one skipif-guarded test that shells out for real.
MINIMAL_PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"

TECTONIC_ERROR = """error: something went wrong
error: TeX error: Undefined control sequence
error: --- line 42 of report.tex ---
error: \\factrefx{F-001}
error: --------------------------
"""


@dataclass
class CompletedStub:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def tectonic_stub(*, fail=False, stderr=TECTONIC_ERROR):
    """A stand-in for render._tectonic_run.

    On success it writes MINIMAL_PDF where tectonic would have, so the
    caller's "did a PDF land" check exercises the real path.
    """
    def run(tex_path, out_dir):
        if fail:
            return CompletedStub(1, stderr=stderr)
        pdf = out_dir / (tex_path.stem + ".pdf")
        pdf.write_bytes(MINIMAL_PDF)
        return CompletedStub(0)
    return run
