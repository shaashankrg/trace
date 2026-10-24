"""Graph state. `evidence` and `transcript` only grow, so nodes return the new items and the
reducer appends them."""
import operator
from typing import Annotated, Literal, TypedDict

EvidenceType = Literal["trace", "chunk", "log", "metric", "change", "replay"]


class Evidence(TypedDict):
    type: EvidenceType
    ref: str
    claim: str


class Candidate(TypedDict):
    root_component: str
    category: str
    rationale: str
    score: float


class InvestigationState(TypedDict, total=False):
    alert: dict
    triage: str                                         # the triage summary the later nodes read
    candidates: list[Candidate]
    evidence: Annotated[list[Evidence], operator.add]
    tool_calls_used: int
    replays_used: int
    transcript: Annotated[list[dict], operator.add]     # every LLM and tool call, args, sizes, tokens
    notes: str                                          # the investigator's last words before concluding
    done: bool                                          # investigate decided it has enough
    diagnosis: dict | None
