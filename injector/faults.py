"""The fault catalogue. Each fault breaks one thing in the running assistant.

apply()          break it
revert()         undo it; safe to call any number of times
change_record()  the entry the fault leaves in `changes`, or None for an outage
ground_truth()   the label that goes to the eval schema, never to the investigator
"""
import json
from datetime import datetime, timedelta

import psycopg

from assistant.config import DEFAULTS
from assistant.store_seed import generate
from injector import state


class Fault:
    type = ""
    root_component = ""
    category = ""

    def __init__(self, conn: psycopg.Connection, params: dict, created_at: datetime,
                 change_id: str | None = None, author: str = "ops-bot", description: str = ""):
        self.conn = conn
        self.params = params
        self.created_at = created_at
        self.change_id = change_id
        self.author = author
        self.description = description

    def apply(self) -> None:
        raise NotImplementedError

    def revert(self) -> None:
        raise NotImplementedError

    def apply_healthy(self) -> None:
        """The world the alert's correct answer assumes, without the fault: run_case asks the
        question here first. Most faults break a world that is already right, so nothing to do;
        undone by revert()."""

    def change_record(self) -> dict | None:
        return None

    def cause_ref(self) -> str:
        return self.change_id or ""

    def ground_truth(self) -> dict:
        return {"root_component": self.root_component, "category": self.category,
                "cause_ref": self.cause_ref(), "fault_type": self.type}

    def _record(self, kind: str, target: str, diff: str) -> dict:
        return state.change_record(self.change_id, kind, target, self.description, diff,
                                   self.author, self.created_at)


class StaleDocument(Fault):
    """The old version of a doc was never removed from the index, so v1 and v2 both retrieve."""
    type = "stale_document"
    root_component = "knowledge_base"
    category = "doc_update"

    def apply(self) -> None:
        self.conn.execute("UPDATE chunks SET indexed = TRUE WHERE doc_id = %s", (self.params["doc_id"],))

    def revert(self) -> None:
        state.restore_index_flags(self.conn, self.params["doc_id"])

    def cause_ref(self) -> str:
        # The v2 publish record already exists in the baseline history.
        return state.baseline_change_id(self.params["doc_id"], self.params["new_version"])


class MissingDocument(Fault):
    type = "missing_document"
    root_component = "knowledge_base"
    category = "doc_delete"

    def apply(self) -> None:
        self.conn.execute("UPDATE chunks SET indexed = FALSE WHERE doc_id = %s", (self.params["doc_id"],))

    def revert(self) -> None:
        state.restore_index_flags(self.conn, self.params["doc_id"])

    def change_record(self) -> dict:
        doc_id = self.params["doc_id"]
        n = self.conn.execute("SELECT count(*) FROM chunks WHERE doc_id = %s", (doc_id,)).fetchone()[0]
        return self._record("doc_delete", f"doc:{doc_id}",
                            f"- index: {doc_id} ({n} chunks)\n+ index: (removed)\n")


class _ConfigFault(Fault):
    key = ""
    category = "config_change"
    root_component = "retriever"

    @property
    def new_value(self) -> str:
        return str(self.params["value"])

    def apply(self) -> None:
        state.set_config(self.conn, self.key, self.new_value, self.created_at)

    def revert(self) -> None:
        state.set_config(self.conn, self.key, DEFAULTS[self.key])

    def change_record(self) -> dict:
        return self._record("config_change", f"config:{self.key}",
                            f"- {self.key}: {DEFAULTS[self.key]}\n+ {self.key}: {self.new_value}\n")


class ThresholdTooStrict(_ConfigFault):
    type = "threshold_too_strict"
    key = "similarity_threshold"


class ContextOverflow(_ConfigFault):
    type = "context_overflow"
    key = "top_k"


class SilentToolFailure(Fault):
    """The tool times out, the wrapper swallows it and returns []. No change record: an outage.

    The timeout key is global, so every tool is affected; cases only ask lookup_order
    questions, whose join is slow enough to blow a 1 ms budget."""
    type = "silent_tool_failure"
    root_component = "tool:lookup_order"
    category = "tool_outage"

    def apply(self) -> None:
        # No updated_at bump: nothing in the config store shows when this happened.
        state.set_config(self.conn, "tool_timeout_ms", "1")

    def revert(self) -> None:
        state.set_config(self.conn, "tool_timeout_ms", DEFAULTS["tool_timeout_ms"])

    def cause_ref(self) -> str:
        return "outage:tool:lookup_order"


class StaleToolData(Fault):
    """Stock changed in the source of truth but inventory_cache stopped refreshing."""
    type = "stale_tool_data"
    root_component = "tool:check_inventory"
    category = "stale_data"

    def apply(self) -> None:
        pid = self.params["product_id"]
        self.conn.execute(
            "UPDATE inventory_source SET qty = %s, updated_at = %s WHERE product_id = %s",
            (self.params["new_qty"], self.created_at, pid))
        # The cache keeps its old qty and its refreshed_at falls behind: that is the only trail.
        self.conn.execute(
            "UPDATE inventory_cache SET refreshed_at = %s WHERE product_id = %s",
            (self.created_at - timedelta(days=3), pid))

    def apply_healthy(self) -> None:
        # The restock happened and the cache refreshed, as it should have.
        pid, qty = self.params["product_id"], self.params["new_qty"]
        self.conn.execute("UPDATE inventory_source SET qty = %s, updated_at = now() WHERE product_id = %s", (qty, pid))
        self.conn.execute("UPDATE inventory_cache SET qty = %s, refreshed_at = now() WHERE product_id = %s", (qty, pid))

    def revert(self) -> None:
        state.reset_inventory(self.conn, self.params["product_id"])

    def cause_ref(self) -> str:
        return "data:inventory_cache"


def prompt_without_grounding_line(template: str) -> str:
    """Drop the 'only answer from the documents' instruction."""
    lines = template.split("\n")
    kept = [x for x in lines if not x.startswith("Only answer from the provided documents")]
    assert len(kept) < len(lines), "grounding line not found in the active prompt"
    return "\n".join(kept)


class PromptRegression(Fault):
    type = "prompt_regression"
    root_component = "prompt"
    category = "prompt_edit"
    version = None
    previous = "v1"

    def apply(self) -> None:
        self.previous = state.get_config(self.conn, "prompt_version") or "v1"
        self.old_template = self.conn.execute(
            "SELECT template FROM prompt_versions WHERE version = %s", (self.previous,)).fetchone()[0]
        self.new_template = prompt_without_grounding_line(self.old_template)
        self.version = state.add_prompt_version(
            self.conn, self.new_template, self.author, self.description, self.created_at)
        state.set_config(self.conn, "prompt_version", self.version, self.created_at)

    def revert(self) -> None:
        if self.version:
            state.drop_prompt_version(self.conn, self.version, self.previous)

    def change_record(self) -> dict:
        diff = state.unified(self.old_template + "\n", self.new_template + "\n", "prompt")
        return self._record("prompt_edit", "prompt", diff)



class ChunkingRegression(Fault):
    """Re-chunked at a size that splits the key sentence across chunks, so retrieval returns
    halves of the answer. Both chunkings stay in the table; only the index flag moves, which
    is what lets a replay switch between them in memory."""
    type = "chunking_regression"
    root_component = "retriever"
    category = "retrieval_failure"

    @property
    def size(self) -> int:
        return int(self.params["chunk_size"])

    def apply(self) -> None:
        doc_id = self.params["doc_id"]
        state.rechunk_doc(self.conn, doc_id, self.size)
        # The default chunking leaves the index; the off-size chunking of the newest version takes it.
        self.conn.execute("UPDATE chunks SET indexed = FALSE WHERE doc_id = %s AND chunk_id NOT LIKE %s",
                          (doc_id, f"%/c{self.size}"))
        newest = self.conn.execute(
            """SELECT version FROM documents WHERE doc_id = %s
               ORDER BY published_at DESC, version DESC LIMIT 1""", (doc_id,)).fetchone()[0]
        self.conn.execute(
            "UPDATE chunks SET indexed = TRUE WHERE doc_id = %s AND version = %s AND chunk_id LIKE %s",
            (doc_id, newest, f"%/c{self.size}"))
        state.set_config(self.conn, "chunk_size", str(self.size), self.created_at)

    def revert(self) -> None:
        state.set_config(self.conn, "chunk_size", DEFAULTS["chunk_size"])
        state.drop_rechunk(self.conn, self.params["doc_id"])
        state.restore_index_flags(self.conn, self.params["doc_id"])

    def change_record(self) -> dict:
        return self._record("config_change", "config:chunk_size",
                            f"- chunk_size: {DEFAULTS['chunk_size']}\n+ chunk_size: {self.size}\n")


class EmbeddingMismatch(Fault):
    """The query embedding model was switched without re-indexing, so queries are embedded by
    one model and the chunks by another. Same dimension (384), so nothing raises: the only
    symptom is that retrieval ranks the wrong chunks."""
    type = "embedding_mismatch"
    root_component = "retriever"
    category = "retrieval_failure"

    @property
    def new_value(self) -> str:
        return self.params["embedding_model"]

    def apply(self) -> None:
        state.set_config(self.conn, "embedding_model", self.new_value, self.created_at)

    def revert(self) -> None:
        state.set_config(self.conn, "embedding_model", DEFAULTS["embedding_model"])

    def change_record(self) -> dict:
        return self._record("config_change", "config:embedding_model",
                            f"- embedding_model: {DEFAULTS['embedding_model']}\n"
                            f"+ embedding_model: {self.new_value}\n")


class ModelDowngrade(Fault):
    """Switched to a smaller, cheaper model. Only breaks questions that need several steps."""
    type = "model_downgrade"
    root_component = "model"
    category = "model_change"

    @property
    def new_value(self) -> str:
        return self.params["model"]

    def apply(self) -> None:
        state.set_config(self.conn, "model", self.new_value, self.created_at)

    def revert(self) -> None:
        state.set_config(self.conn, "model", DEFAULTS["model"])

    def change_record(self) -> dict:
        return self._record("model_change", "config:model",
                            f"- model: {DEFAULTS['model']}\n+ model: {self.new_value}\n")


class GuardrailMisfire(Fault):
    """An everyday topic was added to the blocked list, so ordinary questions are refused."""
    type = "guardrail_misfire"
    root_component = "guardrail"
    category = "guardrail_error"

    @property
    def topic(self) -> str:
        return self.params["topic"]

    def _blocked(self) -> list[str]:
        return json.loads(DEFAULTS["guardrail_blocked_topics"])

    def apply(self) -> None:
        state.set_config(self.conn, "guardrail_blocked_topics",
                         json.dumps(self._blocked() + [self.topic]), self.created_at)

    def revert(self) -> None:
        state.set_config(self.conn, "guardrail_blocked_topics", DEFAULTS["guardrail_blocked_topics"])

    def change_record(self) -> dict:
        before, after = self._blocked(), self._blocked() + [self.topic]
        return self._record("config_change", "config:guardrail_blocked_topics",
                            f"- guardrail_blocked_topics: {json.dumps(before)}\n"
                            f"+ guardrail_blocked_topics: {json.dumps(after)}\n")


FAULTS = {cls.type: cls for cls in (
    StaleDocument, MissingDocument, ThresholdTooStrict, ContextOverflow,
    SilentToolFailure, StaleToolData, PromptRegression,
    ChunkingRegression, EmbeddingMismatch, ModelDowngrade, GuardrailMisfire,
)}


def build(conn: psycopg.Connection, spec: dict, now: datetime) -> Fault:
    """Make a fault from a case's `fault` block; its change is dated age_min minutes ago."""
    cls = FAULTS[spec["type"]]
    return cls(conn, spec["params"], now - timedelta(minutes=spec["age_min"]),
               spec.get("change_id"), spec.get("author", "ops-bot"), spec.get("description", ""))


def healthy_inventory_qty(product_id: str) -> int:
    return dict(generate()["inventory"])[product_id]
