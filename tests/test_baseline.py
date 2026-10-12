"""Pure tests for baseline B1 and the scorer: no database or network needed."""
import math

from eval.baselines import b1_diagnose, component_for
from eval.score import markdown, score_cases, summarize


def trace_with(*spans) -> dict:
    return {"data": [{"spans": [
        {"operationName": name, "startTime": i, "tags": tags} for i, (name, tags) in enumerate(spans)
    ]}]}


ERR = [{"key": "otel.status_code", "value": "ERROR"}, {"key": "error", "value": True}]


def test_component_mapping():
    assert component_for("retrieve") == "retriever"
    assert component_for("tool.lookup_order") == "tool:lookup_order"
    assert component_for("guardrail") == "guardrail"
    assert component_for("llm.chat") == "model"
    assert component_for("POST /chat") is None


def test_b1_blames_the_error_span():
    d = b1_diagnose(trace_with(("POST /chat", []), ("retrieve", ERR), ("llm.chat", [])))
    assert d["root_component"] == "retriever"
    d = b1_diagnose(trace_with(("tool.check_inventory", ERR)))
    assert (d["root_component"], d["category"]) == ("tool:check_inventory", "tool_outage")


def test_b1_blames_the_first_error_in_time():
    d = b1_diagnose(trace_with(("tool.lookup_order", ERR), ("llm.chat", ERR)))
    assert d["root_component"] == "tool:lookup_order"


def test_b1_ignores_an_error_on_the_root_span_alone():
    d = b1_diagnose(trace_with(("POST /chat", ERR), ("llm.chat", [])))
    assert d["root_component"] == "model"


def test_b1_defaults_to_the_model_when_nothing_errors():
    d = b1_diagnose(trace_with(("POST /chat", []), ("retrieve", []), ("tool.lookup_order", []), ("llm.chat", [])))
    assert (d["root_component"], d["category"]) == ("model", "model_change")


TRUTH = [
    {"case_id": "case-001", "fault_type": "threshold_too_strict", "has_decoy": False,
     "root_component": "retriever", "category": "config_change", "cause_ref": "change:config-change-1"},
    {"case_id": "case-002", "fault_type": "silent_tool_failure", "has_decoy": True,
     "root_component": "tool:lookup_order", "category": "tool_outage", "cause_ref": "outage:tool:lookup_order"},
    {"case_id": "case-003", "fault_type": "prompt_regression", "has_decoy": True,
     "root_component": "prompt", "category": "prompt_edit", "cause_ref": "change:prompt-edit-3"},
]


def test_scoring_counts_a_missing_diagnosis_as_wrong():
    diagnoses = {
        "case-001": {"root_component": "retriever", "category": "config_change", "cause_ref": "change:config-change-1"},
        "case-002": {"root_component": "model", "category": "model_change"},
    }
    rows = score_cases(TRUTH, diagnoses)
    assert [r["component_correct"] for r in rows] == [True, False, False]
    assert [r["category_correct"] for r in rows] == [True, False, False]
    assert [r["cause_correct"] for r in rows] == [True, False, False]

    summary = {(s["group"], s["key"]): s for s in summarize(rows)}
    assert summary[("overall", "all")]["top1"] == 1 / 3
    assert summary[("decoy", "without decoy")]["top1"] == 1.0
    assert summary[("decoy", "with decoy")]["top1"] == 0.0
    assert summary[("fault_type", "prompt_regression")]["n"] == 1


def test_all_b1_style_guesses_score_zero_on_silent_faults():
    diagnoses = {t["case_id"]: {"root_component": "model", "category": "model_change"} for t in TRUTH}
    rows = score_cases(TRUTH, diagnoses)
    assert sum(r["component_correct"] for r in rows) == 0


def test_empty_groups_do_not_crash_the_report():
    rows = score_cases(TRUTH[:1], {})
    summary = summarize(rows)
    without = next(s for s in summary if s["key"] == "with decoy")
    assert without["n"] == 0 and math.isnan(without["top1"])
    assert "n/a" in markdown("x", summary, rows)
