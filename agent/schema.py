"""The diagnosis the investigator returns (spec 9.3), validated with Pydantic.

The vocabulary is the one the evaluator scores against. Day 3 labelled categories by the kind
of cause (doc_update, config_change...) rather than with spec section 7's names, so this list
follows the ground truth.
"""
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from tools.refs import RefError, parse_ref
from tools.replay import PatchError, normalize_patch

COMPONENTS = {
    "knowledge_base": "the documents and which versions of them are indexed for retrieval",
    "retriever": "retrieval itself: top_k, similarity threshold, embedding model, ranking",
    "tool:<name>": "one store tool the assistant calls, e.g. tool:lookup_order (use the name from the trace)",
    "prompt": "the system prompt version the model was given",
    "model": "the language model, when it got correct inputs and still answered wrongly",
    "config": "another runtime setting that is not part of retrieval (e.g. max_response_tokens)",
    "guardrail": "the topic filter that blocks questions before they reach the model",
}

CATEGORIES = {
    "doc_update": "a document was published or edited, including which of its versions are indexed",
    "doc_delete": "a document was removed or taken out of the index",
    "config_change": "a runtime configuration value was changed",
    "prompt_edit": "the system prompt was edited",
    "model_change": "the model or model version changed, or the model erred on correct inputs",
    "tool_outage": "a tool failed, timed out or returned nothing when it should have returned data",
    "stale_data": "a tool returned data that is out of date",
}

_TOOL_COMPONENT = re.compile(r"^tool:[a-z][a-z0-9_]*$")


def valid_component(name: str) -> bool:
    return name in COMPONENTS and name != "tool:<name>" or bool(_TOOL_COMPONENT.match(name or ""))


class EvidenceItem(BaseModel):
    type: Literal["trace", "chunk", "log", "metric", "change", "replay"]
    ref: str
    claim: str = Field(min_length=3)
    # Optional, checkable against the recording by the evidence verifier (eval/verifier.py):
    attribute: str | None = None    # a span attribute, e.g. "tool.result_count"
    value: Any = None               # its value; for a replay, the matches_correction it showed
    quote: str | None = None        # words that appear in the chunk or log text
    supports: str | None = None     # the component this item points at (the gate decides for itself)

    @field_validator("ref")
    @classmethod
    def ref_is_well_formed(cls, ref: str) -> str:
        try:
            parse_ref(ref)
        except RefError as e:
            raise ValueError(str(e)) from e
        return ref


class RuledOut(BaseModel):
    change: str
    reason: str
    ref: str | None = None          # the change, or a replay, that backs the reason

    @field_validator("ref")
    @classmethod
    def ref_is_well_formed(cls, ref: str | None) -> str | None:
        if ref is not None:
            try:
                parse_ref(ref)
            except RefError as e:
                raise ValueError(str(e)) from e
        return ref


class Diagnosis(BaseModel):
    root_component: str | None = None
    category: str | None = None
    cause_ref: str | None = None          # the change that caused it, when a change did
    confidence: float = Field(ge=0, le=1)
    summary: str
    evidence: list[EvidenceItem] = []
    ruled_out: list[RuledOut] = []
    suggested_fix: str = ""
    fix_patch: dict | None = None         # the suggested fix as a replay patch (tools/replay.py)
    fix_patch_error: str = ""             # why a malformed fix_patch was dropped
    abstained: bool = False
    # Filled in by the abstain node: what it would have said, and why that was not enough.
    top_candidate: dict | None = None     # {root_component, category}
    top_candidates: list[dict] = []
    missing_evidence: str = ""

    @model_validator(mode="before")
    @classmethod
    def drop_a_malformed_patch(cls, data):
        """A fix_patch that is not a valid patch is dropped, not fatal: the diagnosis still stands,
        and it scores as having no fix."""
        if isinstance(data, dict) and data.get("fix_patch"):
            try:
                patch = normalize_patch(data["fix_patch"])
                data = {**data, "fix_patch": None if patch["kind"] == "none" else patch}
            except PatchError as e:
                data = {**data, "fix_patch": None, "fix_patch_error": f"{e}. {e.hint}".strip()}
        return data

    @model_validator(mode="after")
    def check_vocabulary(self) -> "Diagnosis":
        if self.abstained:
            return self
        if not self.root_component or not valid_component(self.root_component):
            raise ValueError(f"root_component must be one of {sorted(COMPONENTS)}, got {self.root_component!r}")
        if self.category not in CATEGORIES:
            raise ValueError(f"category must be one of {sorted(CATEGORIES)}, got {self.category!r}")
        if not self.evidence:
            raise ValueError("a diagnosis that does not abstain needs at least one evidence item")
        if self.cause_ref:
            try:
                parse_ref(self.cause_ref)
            except RefError as e:
                raise ValueError(f"cause_ref: {e}") from e
        return self


def abstain(summary: str, top_candidates: list[dict] | None = None, missing: str = "") -> dict:
    """An abstention. It names no component, so it scores as no answer; `top_candidate` keeps what
    the investigator would have said, so the evaluator can tell whether abstaining was right."""
    tops = [{"root_component": c.get("root_component"), "category": c.get("category")}
            for c in (top_candidates or [])][:2]
    return Diagnosis(abstained=True, confidence=0.0, summary=summary, top_candidate=tops[0] if tops else None,
                     top_candidates=tops, missing_evidence=missing).model_dump()
