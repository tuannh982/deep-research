"""One writer, three properties: UTF-8 regardless of locale, no newline
translation, and no partially-written file ever visible.

All three are load-bearing for the same reason: a citation.quote is
verbatim web text hashed into quote_sha256, and the rechecker's page match
depends on that text surviving intact to disk."""
import pytest

import atomicio


def test_write_text_creates_missing_parents(tmp_path):
    target = tmp_path / "a" / "b" / "c.txt"
    atomicio.write_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_write_text_returns_the_path(tmp_path):
    target = tmp_path / "x.txt"
    assert atomicio.write_text(target, "hi") == target


def test_crlf_survives_the_round_trip(tmp_path):
    target = tmp_path / "x.txt"
    atomicio.write_text(target, "a\r\nb\rc\n")
    with target.open("r", encoding="utf-8", newline="") as handle:
        assert handle.read() == "a\r\nb\rc\n"


def test_non_ascii_survives_an_ascii_locale(tmp_path, monkeypatch):
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("LANG", "C")
    target = tmp_path / "x.txt"
    atomicio.write_text(target, "café — 日本語")
    assert target.read_text(encoding="utf-8") == "café — 日本語"


def test_an_overwrite_leaves_no_temp_file_behind(tmp_path):
    target = tmp_path / "x.txt"
    atomicio.write_text(target, "one")
    atomicio.write_text(target, "two")
    assert target.read_text(encoding="utf-8") == "two"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.txt"]


def test_the_temp_file_is_created_beside_the_target(tmp_path, monkeypatch):
    """os.replace is only atomic within one filesystem. A temp file in
    /tmp would make the swap a cross-device copy."""
    seen = {}
    real_replace = atomicio.os.replace

    def spy(src, dst):
        seen["src_parent"] = str(src).rsplit("/", 1)[0]
        seen["dst_parent"] = str(dst).rsplit("/", 1)[0]
        return real_replace(src, dst)

    monkeypatch.setattr(atomicio.os, "replace", spy)
    atomicio.write_text(tmp_path / "sub" / "x.txt", "hi")
    assert seen["src_parent"] == seen["dst_parent"]


def test_a_failed_write_leaves_the_original_intact(tmp_path, monkeypatch):
    target = tmp_path / "x.txt"
    atomicio.write_text(target, "original")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(atomicio.os, "replace", boom)
    with pytest.raises(OSError):
        atomicio.write_text(target, "replacement")
    assert target.read_text(encoding="utf-8") == "original"


def test_memory_writes_through_atomicio(mem, mktask, monkeypatch):
    """The extraction is only worth anything if the store actually uses
    it. Without this, memory.py could keep a divergent private copy."""
    calls = []
    real = atomicio.write_text
    monkeypatch.setattr(atomicio, "write_text",
                        lambda p, t: calls.append(p) or real(p, t))
    mktask()
    assert any(p.name == "T-001.md" for p in calls)
