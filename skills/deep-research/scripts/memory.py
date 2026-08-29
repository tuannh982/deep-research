"""The only writer of research/memory/. Validating and atomic."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import jsonschema

import atomicio
import ids as ids_mod
import nodes

DIRNAME = {
    "task": "tasks", "fact": "facts", "assumption": "assumptions",
    "hypothesis": "hypotheses", "citation": "citations",
}


class ValidationError(ValueError):
    """A node does not satisfy its schema."""


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Memory:
    def __init__(self, root, schema_dir=None):
        self.root = Path(root)
        self.schema_dir = Path(schema_dir) if schema_dir else (
            Path(__file__).resolve().parent.parent / "schemas"
        )
        self._schemas = {}

    # --- locations ---------------------------------------------------
    def dir_for(self, node_type):
        return self.root / "memory" / DIRNAME[node_type]

    def path_for(self, node_id):
        return self.dir_for(nodes.type_of(node_id)) / f"{node_id}.md"

    # --- validation --------------------------------------------------
    def schema(self, node_type):
        if node_type not in self._schemas:
            path = self.schema_dir / f"{node_type}.json"
            self._schemas[node_type] = json.loads(
                path.read_text(encoding="utf-8")
            )
        return self._schemas[node_type]

    def validate(self, data):
        try:
            jsonschema.validate(data, self.schema(data["type"]))
        except jsonschema.ValidationError as error:
            where = "/".join(str(p) for p in error.absolute_path) or "<root>"
            node_id = data.get("id", "<unallocated>")
            raise ValidationError(f"{node_id}: {where}: {error.message}") from None

    # --- reads -------------------------------------------------------
    def exists(self, node_id):
        return self.path_for(node_id).is_file()

    def read(self, node_id):
        path = self.path_for(node_id)
        if not path.is_file():
            raise KeyError(node_id)
        # newline="" disables universal-newline translation, so a body's
        # \r\n and bare \r survive the read instead of collapsing to \n.
        # Path.read_text() grew a newline= parameter only in 3.13 and this
        # project targets >=3.11, hence the explicit open().
        with path.open("r", encoding="utf-8", newline="") as handle:
            return nodes.loads(handle.read())

    def ids(self, node_type):
        directory = self.dir_for(node_type)
        if not directory.is_dir():
            return []
        return sorted(path.stem for path in directory.glob("*.md"))

    def all_ids(self):
        return sorted(i for t in nodes.NODE_TYPES for i in self.ids(t))

    def list(self, node_type):
        return [self.read(i) for i in self.ids(node_type)]

    # --- writes ------------------------------------------------------
    def _atomic_write(self, path, text):
        """See atomicio.write_text for why encoding, newline and the temp
        file's location are all pinned."""
        atomicio.write_text(path, text)

    def create(self, node_type, data):
        """Allocate an id and write. Retries once if the id was taken."""
        for _ in range(2):
            node_id = ids_mod.next_id(self.ids(node_type), node_type)
            if self.exists(node_id):
                continue
            now = utcnow()
            full = {**data, "id": node_id, "type": node_type,
                    "created_at": now, "updated_at": now}
            self.validate(full)
            self._atomic_write(self.path_for(node_id), nodes.dumps(full))
            return full
        raise RuntimeError(f"could not allocate an id for {node_type}")

    def update(self, node_id, **changes):
        # The id is pinned to the argument, not to current["id"]: the
        # argument is what chose the path being written, so it is the only
        # id consistent with where the bytes land. Reading it back out of
        # the file's own content would make the sole writer preserve — and
        # freshly re-stamp updated_at on — exactly the filename/frontmatter
        # divergence fsck exists to report.
        current = self.read(node_id)
        data = {**current, **changes, "id": node_id, "type": current["type"],
                "created_at": current["created_at"], "updated_at": utcnow()}
        self.validate(data)
        self._atomic_write(self.path_for(node_id), nodes.dumps(data))
        return data
