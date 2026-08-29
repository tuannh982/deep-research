"""Sequential id allocation. Gaps are never reused."""
import re

from nodes import ID_PREFIX, NODE_TYPES

_ID = re.compile(r"\A([TFAHC])-([0-9]+)\Z")


def next_id(existing_ids, node_type):
    if node_type not in NODE_TYPES:
        raise ValueError(f"unknown node type {node_type!r}")
    prefix = ID_PREFIX[node_type]
    highest = 0
    for node_id in existing_ids:
        match = _ID.match(node_id)
        if match and match.group(1) == prefix:
            highest = max(highest, int(match.group(2)))
    return f"{prefix}-{highest + 1:03d}"


def numeric(node_id):
    """The integer part of an id, for ordering.

    Memory.ids() sorts lexicographically, which puts F-1000 before F-999.
    That is harmless for a stable set but wrong wherever "most recent"
    matters — the fact cap in the hypothesizer's input packet, for one.
    Memory.ids is deliberately left alone: changing it would ripple
    through every sorted-ids guarantee in the codebase for the sake of
    one caller.
    """
    match = _ID.match(node_id)
    if not match:
        raise ValueError(f"not a node id: {node_id!r}")
    return int(match.group(2))
