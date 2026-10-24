"""Diagnosis schema and evidence extraction: pure, no model, no server."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent import evidence as ev
from agent.llm import parse_json
from agent.schema import CATEGORIES, Diagnosis, abstain, valid_component
from tools import handlers, refs
from tools.backend import SnapshotBackend

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CASES = sorted(p.name for p in FIXTURES.iterdir() if (p / "alert.json").exists())

GOOD = {
    "root_component": "knowledge_base", "category": "doc_update", "cause_ref": "change:doc-update-1036",
    "confidence": 0.8, "summary": "Both versions of the warranty doc are indexed.",
    "evidence": [{"type": "chunk", "ref": "chunk:warranty@v1#0", "claim": "retrieved the 1-year text"}],
    "ruled_out": [{"change": "change:config-change-7565", "reason": "max_response_tokens has no effect here"}],
    "suggested_fix": "Unindex v1.", "abstained": False,
}


def test_a_good_diagnosis_validates():
    assert Diagnosis.model_validate(GOOD).root_component == "knowledge_base"


@pytest.mark.parametrize("field, value", [
    ("root_component", "database"), ("root_component", "tool:"), ("root_component", "tool:<name>"),
    ("category", "stale_knowledge"), ("confidence", 1.5), ("evidence", []),
    ("cause_ref", "doc-update-1036 but not a ref"),
])
def test_out_of_vocabulary_or_unsupported_diagnoses_are_rejected(field, value):
    with pytest.raises(ValidationError):
        Diagnosis.model_validate({**GOOD, field: value})


def test_a_malformed_evidence_ref_is_rejected():
    bad = {**GOOD, "evidence": [{"type": "chunk", "ref": "warranty@v1#0", "claim": "no prefix"}]}
    with pytest.raises(ValidationError):
        Diagnosis.model_validate(bad)


def test_tool_components_take_the_tool_name():
    assert valid_component("tool:lookup_order") and valid_component("prompt")
    assert not valid_component("tool:Lookup Order") and not valid_component("")


def test_abstaining_needs_no_component_and_scores_as_a_miss():
    d = abstain("could not decide")
    assert d["abstained"] and d["root_component"] is None and d["category"] is None
    Diagnosis.model_validate(d)


def test_vocabulary_matches_the_ground_truth_labels():
    assert {"doc_update", "doc_delete", "config_change", "prompt_edit", "tool_outage", "stale_data"} <= set(CATEGORIES)


@pytest.mark.parametrize("text", ['{"a": 1}', '```json\n{"a": 1}\n```', 'Here it is: {"a": 1} done'])
def test_parse_json_tolerates_fences_and_chatter(text):
    assert parse_json(text) == {"a": 1}


@pytest.mark.parametrize("case", CASES)
def test_every_evidence_item_is_a_ref_the_tool_returned_and_it_resolves(case):
    b = SnapshotBackend(case, FIXTURES)
    alert = json.loads((FIXTURES / case / "alert.json").read_text(encoding="utf-8"))
    trace = handlers.get_trace(b, alert["conversation_id"])
    retrieve = next(s["ref"] for s in trace["spans"] if s["name"] == "retrieve")
    calls = {
        "get_trace": trace,
        "get_retrieved_chunks": handlers.get_retrieved_chunks(b, retrieve),
        "document_history": handlers.document_history(b, "refund-policy"),
        "search_logs": handlers.search_logs(b),
        "recent_changes": handlers.recent_changes(b),
        "query_metrics": handlers.query_metrics(b, "kestrel_canary_accuracy"),
        "diff_prompt_versions": handlers.diff_prompt_versions(b, "v1", "v1"),
    }
    for tool, result in calls.items():
        items = ev.from_result(tool, result)
        assert items or result.get("total") == 0, tool  # a retrieval that returned nothing has nothing to cite
        for item in items:
            assert item["ref"] in json.dumps(result), (tool, item["ref"])
            refs.resolve_ref(b, item["ref"])
            assert item["type"] in ("trace", "chunk", "log", "metric", "change") and item["claim"]


def test_errors_produce_no_evidence():
    assert ev.from_result("get_trace", {"error": "x", "hint": "y"}) == []


def test_merge_keeps_the_first_claim_for_a_ref():
    a = [{"type": "log", "ref": "log:1", "claim": "first"}]
    new = [{"type": "log", "ref": "log:1", "claim": "again"}, {"type": "log", "ref": "log:2", "claim": "new"}]
    assert ev.merge(a, new) == [new[1]]


def test_agent_package_never_imports_eval_or_injector():
    import ast
    root = Path(__file__).resolve().parents[2] / "agent"
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            assert not any(n.split(".")[0] in {"eval", "injector"} for n in names), (path.name, names)
