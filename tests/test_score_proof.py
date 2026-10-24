"""The Day 6 scoring rules: hallucinated diagnoses are wrong, abstentions are not answers, and a fix
only succeeds when its replay matches AND its patch targets the true root component (F5)."""
from eval.score import proof_summary, score_cases

TRUTH = [{"case_id": f"c{i}", "fault_type": "f", "has_decoy": False, "valid": True,
          "root_component": "knowledge_base", "category": "doc_update", "cause_ref": "change:1"} for i in range(5)]


def diag(**kw) -> dict:
    return {"root_component": "knowledge_base", "category": "doc_update", "cause_ref": "change:1",
            "abstained": False, "hallucinated": False, "fix_matches": True, "fix_target": "knowledge_base",
            "fix_proves": True, "replay_judges": ["code"], **kw}


def test_rules_and_metrics():
    diagnoses = {
        "c0": diag(),                                                     # right, fixed
        "c1": diag(hallucinated=True),                                    # right component, bad citation
        "c2": diag(fix_target="config"),                                  # right, but the "fix" patched config
        "c3": {"abstained": True, "root_component": None, "top_candidate": "config", "replay_judges": ["llm"]},
        "c4": {"abstained": True, "root_component": None, "top_candidate": "knowledge_base"},
    }
    rows = {r["case_id"]: r for r in score_cases(TRUTH, diagnoses)}
    assert rows["c0"]["component_correct"] and rows["c0"]["fix_success"]
    assert not rows["c1"]["component_correct"] and rows["c1"]["component_correct_unverified"]
    assert not rows["c1"]["cause_correct"] and not rows["c1"]["category_correct"]
    assert rows["c2"]["component_correct"] and not rows["c2"]["fix_success"]  # an accidental fix (F5)
    assert not rows["c3"]["answered"] and not rows["c3"]["component_correct"]

    p = proof_summary(list(rows.values()))
    assert p["answered"] == 3 and p["abstained"] == 2
    assert p["precision"] == 2 / 3                    # c0, c2 right; c1 hallucinated
    assert p["hallucinated_rate"] == 1 / 3
    assert p["fix_success"] == 2 / 3                  # c0 and c1: c1's citation is bad, its fix still works
    assert p["abstain_rate"] == 2 / 5
    assert p["abstention_value"] == 1 / 2             # c3 would have been wrong, c4 right
    assert p["judge_fallback"] == 1 / 4               # 4 replays, 1 judged by the LLM


def test_rates_over_nothing_are_nan():
    p = proof_summary(score_cases(TRUTH[:1], {"c0": diag()}))
    assert p["abstention_value"] != p["abstention_value"]
