"""Decoys: real but unrelated changes that sit in the history next to the real fault.

They really happen (new prompt version, new indexed doc, changed config) so the history
tools show them in the same format as any other change."""
from datetime import datetime, timedelta

import psycopg

from assistant.config import DEFAULTS
from assistant.db import chunk_id
from assistant.ingest import chunk, parse
from assistant.retrieve import embed
from injector import state
from injector.faults import Fault

# Harmless rewordings of the v1 prompt, described as typo fixes.
TYPO_EDITS = [
    ("an outdoor gear retailer.", "an outdoor-gear retailer."),
    ("step by step, showing the dates,", "step by step, showing the dates;"),
    ("for example [refund-policy@v2#0].", "for example: [refund-policy@v2#0]."),
]


class Decoy(Fault):
    def ground_truth(self) -> dict:
        raise TypeError("a decoy is not a cause")


class TypoPromptEdit(Decoy):
    type = "typo_prompt_edit"
    version = None
    previous = "v1"

    def apply(self) -> None:
        self.previous = state.get_config(self.conn, "prompt_version") or "v1"
        self.old_template = self.conn.execute(
            "SELECT template FROM prompt_versions WHERE version = %s", (self.previous,)).fetchone()[0]
        for before, after in TYPO_EDITS:
            if before in self.old_template:
                self.new_template = self.old_template.replace(before, after, 1)
                break
        else:
            raise AssertionError("no typo edit applies to the active prompt")
        self.version = state.add_prompt_version(
            self.conn, self.new_template, self.author, self.description, self.created_at)
        state.set_config(self.conn, "prompt_version", self.version, self.created_at)

    def revert(self) -> None:
        if self.version:
            state.drop_prompt_version(self.conn, self.version, self.previous)

    def change_record(self) -> dict:
        diff = state.unified(self.old_template + "\n", self.new_template + "\n", "prompt")
        return self._record("prompt_edit", "prompt", diff)


class UnrelatedDocAdded(Decoy):
    type = "unrelated_doc_added"

    def _path(self):
        return state.DECOY_DOCS_DIR / f"{self.params['doc_id']}@v1.md"

    def apply(self) -> None:
        meta, body = parse(self._path())
        size = int(state.get_config(self.conn, "chunk_size") or DEFAULTS["chunk_size"])
        pieces = chunk(body, size)
        vectors = embed(pieces, state.get_config(self.conn, "embedding_model") or DEFAULTS["embedding_model"])
        self.revert()  # re-applying must not collide with a previous run
        self.conn.execute(
            "INSERT INTO documents (doc_id, version, title, body, published_at) VALUES (%s,%s,%s,%s,%s)",
            (meta["doc_id"], meta["version"], meta.get("title"), body, self.created_at))
        for i, (piece, vec) in enumerate(zip(pieces, vectors)):
            self.conn.execute(
                """INSERT INTO chunks (chunk_id, doc_id, version, chunk_index, text, embedding, indexed)
                   VALUES (%s,%s,%s,%s,%s,%s,TRUE)""",
                (chunk_id(meta["doc_id"], meta["version"], i), meta["doc_id"], meta["version"], i, piece, vec))

    def revert(self) -> None:
        self.conn.execute("DELETE FROM chunks WHERE doc_id = %s", (self.params["doc_id"],))
        self.conn.execute("DELETE FROM documents WHERE doc_id = %s", (self.params["doc_id"],))

    def change_record(self) -> dict:
        _, body = parse(self._path())
        diff = state.NL.join(f"+{line}" for line in body.splitlines()) + state.NL
        return state.change_record(
            self.change_id, "doc_update", f"doc:{self.params['doc_id']}", self.description,
            diff, self.author, self.created_at)


class UnrelatedConfigChange(Decoy):
    type = "unrelated_config_change"
    key = "max_response_tokens"

    def apply(self) -> None:
        state.set_config(self.conn, self.key, str(self.params["value"]), self.created_at)

    def revert(self) -> None:
        state.set_config(self.conn, self.key, DEFAULTS[self.key])

    def change_record(self) -> dict:
        return self._record("config_change", f"config:{self.key}",
                            f"- {self.key}: {DEFAULTS[self.key]}\n+ {self.key}: {self.params['value']}\n")


DECOYS = {cls.type: cls for cls in (TypoPromptEdit, UnrelatedDocAdded, UnrelatedConfigChange)}


def build(conn: psycopg.Connection, spec: dict, now: datetime) -> Decoy:
    cls = DECOYS[spec["type"]]
    return cls(conn, spec["params"], now - timedelta(minutes=spec["age_min"]),
               spec.get("change_id"), spec.get("author", "ops-bot"), spec.get("description", ""))
