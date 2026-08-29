"""One atomic, byte-faithful text write. Every writer in the skill uses it.

Extracted from memory.Memory._atomic_write, which had it right and had it
alone. Three details are load-bearing, and each was a real defect before
it was pinned:

encoding="utf-8" — without it the text layer encodes with whatever the
locale happens to be, which is UnicodeEncodeError under LC_ALL=C.

newline="" — without it Python rewrites line endings on the way through.
A citation.quote is verbatim web text hashed into quote_sha256, so
translating \\r\\n to \\n silently breaks the quote against its own hash.

The temp file lives in the destination directory, never in /tmp, so
os.replace() is a same-filesystem rename: a reader sees either the whole
old file or the whole new one, never a partial write, and a crash
mid-write leaves the original intact.
"""
import os
from pathlib import Path


def write_text(path, text):
    """Write `text` to `path` atomically. Returns the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return path
