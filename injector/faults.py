"""The fault catalogue. Each fault breaks one thing in the running assistant.

apply()          break it
revert()         undo it; safe to call any number of times
change_record()  the entry the fault leaves in `changes`, or None for an outage
ground_truth()   the label that goes to the eval schema, never to the investigator
"""
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


FAULTS = {cls.type: cls for cls in (
    StaleDocument, MissingDocument, ThresholdTooStrict, ContextOverflow,
    SilentToolFailure, StaleToolData, PromptRegression,
)}


def build(conn: psycopg.Connection, spec: dict, now: datetime) -> Fault:
    """Make a fault from a case's `fault` block; its change is dated age_min minutes ago."""
    cls = FAULTS[spec["type"]]
    return cls(conn, spec["params"], now - timedelta(minutes=spec["age_min"]),
               spec.get("change_id"), spec.get("author", "ops-bot"), spec.get("description", ""))


def healthy_inventory_qty(product_id: str) -> int:
    return dict(generate()["inventory"])[product_id]
