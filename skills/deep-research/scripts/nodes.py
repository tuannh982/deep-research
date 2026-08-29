"""Node serialization: a markdown file with YAML frontmatter <-> a dict."""
import re

import yaml

NODE_TYPES = ("task", "fact", "assumption", "hypothesis", "citation")

# Each node type has exactly one long text field. It lives in the markdown
# body rather than in frontmatter so the files stay readable and diffable.
BODY_FIELD = {
    "task": "question",
    "fact": "statement",
    "assumption": "statement",
    "hypothesis": "claim",
    "citation": "quote",
}

ID_PREFIX = {
    "task": "T", "fact": "F", "assumption": "A",
    "hypothesis": "H", "citation": "C",
}
PREFIX_TYPE = {v: k for k, v in ID_PREFIX.items()}

# Non-greedy, so it stops at the closing delimiter and a body may contain '---'.
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)


class NodeFormatError(ValueError):
    """A node file is not parseable as a node."""


def type_of(node_id):
    prefix = node_id.split("-", 1)[0]
    if prefix not in PREFIX_TYPE:
        raise NodeFormatError(f"unknown id prefix in {node_id!r}")
    return PREFIX_TYPE[prefix]


def dumps(data):
    node_type = data.get("type")
    if node_type not in NODE_TYPES:
        raise NodeFormatError(f"unknown node type {node_type!r}")
    body_field = BODY_FIELD[node_type]
    front = {k: v for k, v in data.items() if k != body_field}
    body = data.get(body_field, "")
    rendered = yaml.safe_dump(front, sort_keys=True, allow_unicode=True)
    return f"---\n{rendered}---\n{body}\n"


def loads(text):
    match = _FRONTMATTER.match(text)
    if not match:
        raise NodeFormatError("file is missing YAML frontmatter delimiters")
    try:
        front = yaml.safe_load(match.group(1))
    except yaml.YAMLError as error:
        raise NodeFormatError("malformed YAML frontmatter") from error

    if front is None:
        front = {}
    elif not isinstance(front, dict):
        raise NodeFormatError("frontmatter must be a mapping")

    node_type = front.get("type")
    if node_type not in NODE_TYPES:
        raise NodeFormatError(f"unknown node type {node_type!r}")

    body = match.group(2)
    if body.endswith('\n'):
        body = body[:-1]

    return {**front, BODY_FIELD[node_type]: body}
