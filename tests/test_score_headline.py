"""The headline comparison table: top-3, decoy resistance, cost and time, and the rule that every
method is scored over the same cases.

The last one matters most. If a baseline's missing cases were dropped from its denominator, a
baseline that crashed on the hard cases would outscore one that attempted them and got them wrong.
"""
from eval.score import comparison, headline, mean, num, score_cases

TRUTH = [
    {"case_id": "c0", "fault_type": "stale_document", "has_decoy": False, "valid": True,
     "root_component": "knowledge_base", "category": "doc_update", "cause_ref": "change:1"},
    {"case_id": "c1", "fault_type": "stale_document", "has_decoy": True, "valid": True,
     "root_component": "knowledge_base", "category": "doc_update", "cause_ref": "change:1"},
    {"case_id": "c2", "fault_type": "silent_tool_failure", "has_decoy": True, "valid": True,
     "root_component": "tool:lookup_order", "category": "tool_outage", "cause_ref": "change:2"},
]


def diag(**kw) -> dict:
    return {"root_component": "knowledge_base", "category": "doc_update", "cause_ref": "change:1",
            "abstained": False, "hallucinated": False, "replay_judges": [], **kw}


def test_top3_uses_the_ranked_list():
    """The right answer ranked second is a top-3 hit but not a top-1 hit."""
    rows = {r["case_id"]: r for r in score_cases(TRUTH, {
        "c0": diag(root_component="retriever",
                   ranked_components=["retriever", "knowledge_base", "model"]),
    })}
    assert not rows["c0"]["component_correct"]
    assert rows["c0"]["top3_correct"]


def test_top3_ignores_a_fourth_place_hit():
    rows = {r["case_id"]: r for r in score_cases(TRUTH, {
        "c0": diag(root_component="retriever",
                   ranked_components=["retriever", "model", "config", "knowledge_base"]),
    })}
    assert not rows["c0"]["top3_correct"], "fourth place is not top-3"


def test_a_method_that_ranks_nothing_falls_back_to_its_one_answer():
    """B1 and B2 produce a single answer. Their top-3 must equal their top-1, not be blank."""
    rows = {r["case_id"]: r for r in score_cases(TRUTH, {
        "c0": diag(),                                   # correct, no ranked list
        "c1": diag(root_component="model"),             # wrong, no ranked list
    })}
    assert rows["c0"]["top3_correct"] and rows["c1"]["top3_correct"] is False


def test_hallucinated_evidence_loses_top3_too():
    """A diagnosis citing evidence that is not there is wrong at every k, or top-3 would launder
    exactly the thing the verifier exists to catch."""
    rows = {r["case_id"]: r for r in score_cases(TRUTH, {
        "c0": diag(hallucinated=True, ranked_components=["knowledge_base"]),
    })}
    assert not rows["c0"]["top3_correct"]


def test_decoy_resistance_is_the_share_of_accuracy_that_survives_a_decoy():
    rows = score_cases(TRUTH, {
        "c0": diag(),                                        # no decoy, correct
        "c1": diag(root_component="prompt"),                 # decoy, wrong (blamed the decoy)
        "c2": diag(root_component="tool:lookup_order", category="tool_outage"),  # decoy, correct
    })
    h = headline(rows)
    assert h["decoy_without"] == 1.0
    assert h["decoy_with"] == 0.5
    assert h["decoy_resistance"] == 0.5


def test_cost_and_time_skip_methods_that_have_none():
    """B1 calls no model and has no wall time. Averaging it as zero would make it look fast and
    free next to the investigator, which is a comparison, not a fact about B1."""
    rows = score_cases(TRUTH, {
        "c0": diag(cost_usd=0.05, wall_s=60.0),
        "c1": diag(cost_usd=0.03, wall_s=40.0),
        "c2": diag(),                                   # B1-style: no cost, no timing
    })
    assert mean(rows, "cost_usd") == 0.04
    assert mean(rows, "wall_s") == 50.0
    assert num(mean([{"cost_usd": None}], "cost_usd"), "$", 4) == "n/a"


def test_every_method_is_scored_over_the_same_cases():
    """A method missing a case scores it unanswered, keeping the denominator honest."""
    full = score_cases(TRUTH, {c: diag() for c in ("c0", "c1", "c2")})
    partial = score_cases(TRUTH, {"c0": diag()})          # crashed on c1 and c2
    assert len(full) == len(partial) == 3
    assert headline(partial)["n"] == 3
    assert headline(partial)["top1"] < headline(full)["top1"]


def test_the_comparison_table_lists_each_method_in_order():
    rows = score_cases(TRUTH, {c: diag() for c in ("c0", "c1", "c2")})
    table = comparison({"investigator": rows, "b1": rows, "b2": rows})
    lines = [ln for ln in table.splitlines() if ln.startswith("| ")]
    assert "method" in lines[0]
    assert lines[1].startswith("| investigator |"), "the investigator goes first"
    assert [ln.split("|")[1].strip() for ln in lines[1:]] == ["investigator", "b1", "b2"]
    for column in ("top-1", "top-3", "hallucinated evidence", "fix success", "decoy resistance"):
        assert column in table


def test_an_empty_method_is_left_out_rather_than_printing_nonsense():
    table = comparison({"investigator": score_cases(TRUTH, {"c0": diag()}), "b2": []})
    assert "b2" not in table
