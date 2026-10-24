"""The diagnosis the investigator returns (spec 9.3), validated with Pydantic.

The vocabulary is the one the evaluator scores against. Day 3 labelled categories by the kind
of cause (doc_update, config_change...) rather than with spec section 7's names, so this list
follows the ground truth.
"""
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from tools.refs import RefError, parse_ref

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


class Diagnosis(BaseModel):
    root_component: str | None = None
    category: str | None = None
    cause_ref: str | None = None          # the change that caused it, when a change did
    confidence: float = Field(ge=0, le=1)
    summary: str
    evidence: list[EvidenceItem] = []
    ruled_out: list[RuledOut] = []
    suggested_fix: str = ""
    abstained: bool = False

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


def abstain(summary: str) -> dict:
    """The fallback when the model cannot produce a valid diagnosis. It names no component, so it
    scores as a miss; the candidates stay in the state for anyone reading the transcript."""
    return Diagnosis(abstained=True, confidence=0.0, summary=summary).model_dump()
