"""Baseline B2 must be a fair comparison and must not see anything the investigator cannot.

The point of B2 is to isolate the value of the investigation, so these tests are mostly about what
it is *not* given: no ground truth, no fault labels, no marking of which change is a decoy. If B2
ever got one of those, the comparison would flatter the tools instead of testing them.
"""
import json

import pytest

from agent.schema import CATEGORIES, COMPONENTS
from eval import b2


@pytest.fixture
def case(tmp_path):
    """A recorded case: a stale document still indexed next to its newer version, plus a decoy."""
    d = tmp_path / "case-900"
    d.mkdir()
    (d / "alert.json").write_text(json.dumps({
        "type": "flagged_conversation",
        "conversation_id": "conv-abc",
        "question": "Can I return boots I bought 20 days ago?",
        "answer": "Yes, returns are allowed within 30 days.",
        "reviewer_correction": "Returns are allowed within 14 days.",
    }), encoding="utf-8")
    (d / "trace.json").write_text(json.dumps({"data": [{"spans": [{
        "operationName": "retrieve",
        "tags": [{"key": "kestrel.retrieval.chunk_ids",
                  "value": ["refund-policy@v1#0", "shipping@v1#0"]}],
    }]}]}), encoding="utf-8")
    (d / "chunks.json").write_text(json.dumps([
        {"chunk_id": "refund-policy@v1#0", "doc_id": "refund-policy", "version": "v1",
         "text": "Returns are accepted within 30 days of delivery.", "indexed": True},
        {"chunk_id": "shipping@v1#0", "doc_id": "shipping", "version": "v1",
         "text": "Standard shipping takes 3-5 business days.", "indexed": True},
        {"chunk_id": "refund-policy@v2#0", "doc_id": "refund-policy", "version": "v2",
         "text": "Returns are accepted within 14 days of delivery.", "indexed": False},
    ]), encoding="utf-8")
    (d / "changes.json").write_text(json.dumps([
        {"change_id": "change:doc-update-11", "kind": "doc_update", "target": "doc:refund-policy",
         "description": "Shorten the refund window", "author": "ana",
         "created_at": "2026-10-30T10:00:00+00:00", "diff": "- 30 days\n+ 14 days\n"},
        {"change_id": "change:prompt-edit-22", "kind": "prompt_edit", "target": "prompt",
         "description": "Fix a typo in the greeting", "author": "bo",
         "created_at": "2026-10-31T09:00:00+00:00"},
    ]), encoding="utf-8")
    return d


def test_the_prompt_carries_the_conversation_chunks_and_changes(case):
    p = b2.build_prompt(case)
    assert "Can I return boots I bought 20 days ago?" in p
    assert "Returns are allowed within 14 days." in p, "the reviewer's correction is the target"
    assert "within 30 days of delivery" in p, "the retrieved chunk's text"
    assert "change:doc-update-11" in p and "change:prompt-edit-22" in p


def test_the_prompt_shows_only_the_chunks_retrieval_returned(case):
    """Handing it every chunk in the store would be a different, easier problem: it could diff v1
    against v2 directly instead of reasoning about what retrieval actually served."""
    p = b2.build_prompt(case)
    assert "refund-policy@v1#0" in p
    assert "refund-policy@v2#0" not in p, "v2 was never retrieved; B2 has no document history"
    assert "within 14 days of delivery" not in p


def test_the_prompt_leaks_no_ground_truth_or_fault_label(case):
    """The anti-cheating rule. A real investigation has no file saying what broke."""
    p = (b2.build_prompt(case) + b2._vocabulary()).lower()
    for leak in ("ground_truth", "ground truth", "stale_document", "fault", "injected",
                 "root_cause", "is correct", "decoy", "red herring"):
        assert leak not in p, f"B2's prompt leaks {leak!r}"


def test_the_decoy_is_present_and_indistinguishable(case):
    """The innocent prompt edit has to be in the list, formatted exactly like the real change."""
    p = b2.build_prompt(case)
    real = [ln for ln in p.splitlines() if "change:doc-update-11" in ln][0]
    decoy = [ln for ln in p.splitlines() if "change:prompt-edit-22" in ln][0]
    for field in ("[", "target=", "by ", "at "):
        assert field in real and field in decoy, f"{field!r} must appear in both"


def test_the_vocabulary_is_the_investigators_vocabulary():
    """Scoring compares strings, so a baseline given different words would lose on wording."""
    v = b2._vocabulary()
    for name in COMPONENTS:
        assert name in v
    for name in CATEGORIES:
        assert name in v


def test_retrieved_chunk_ids_handles_a_json_encoded_tag():
    """OTLP tags arrive as a JSON string in some exports and a list in others."""
    as_list = {"data": [{"spans": [{"operationName": "retrieve", "tags": [
        {"key": "kestrel.retrieval.chunk_ids", "value": ["a@v1#0"]}]}]}]}
    as_str = {"data": [{"spans": [{"operationName": "retrieve", "tags": [
        {"key": "kestrel.retrieval.chunk_ids", "value": '["a@v1#0"]'}]}]}]}
    assert b2.retrieved_chunk_ids(as_list) == ["a@v1#0"]
    assert b2.retrieved_chunk_ids(as_str) == ["a@v1#0"]


def test_a_guardrail_block_leaves_no_retrieved_chunks(case):
    """The guardrail can refuse before retrieval runs. B2 must still produce a prompt."""
    (case / "trace.json").write_text(json.dumps(
        {"data": [{"spans": [{"operationName": "guardrail", "tags": []}]}]}), encoding="utf-8")
    p = b2.build_prompt(case)
    assert "retrieval returned nothing, or never ran" in p


def test_an_unparseable_reply_scores_as_a_wrong_answer_not_a_crash(case, monkeypatch):
    """A baseline that cannot answer in the required shape has failed that case on its own terms;
    crashing would silently drop it from the denominator and flatter B2."""
    class FakeChat:
        def __init__(self, *a, **k):
            self.model = "fake"

        def send(self, turn):
            class U:
                input_tokens = output_tokens = 1

                def cost(self, model):
                    return 0.0
            return type("R", (), {"text": "I think the retriever broke.", "usage": U()})()

    monkeypatch.setattr(b2.llm, "Chat", FakeChat)
    out = b2.b2_diagnose(case)
    assert out["root_component"] is None and out["category"] is None
    assert "unparseable" in out["reason"]


def test_a_diagnosis_has_the_fields_the_scorer_reads(case, monkeypatch):
    """It must drop into the results table as a third column with no scorer changes."""
    class FakeChat:
        def __init__(self, *a, **k):
            self.model = "fake"

        def send(self, turn):
            class U:
                input_tokens = output_tokens = 1

                def cost(self, model):
                    return 0.0
            body = json.dumps({"root_component": "knowledge_base", "category": "doc_update",
                               "cause_ref": "change:doc-update-11", "reason": "v1 still indexed"})
            return type("R", (), {"text": body, "usage": U()})()

    monkeypatch.setattr(b2.llm, "Chat", FakeChat)
    out = b2.b2_diagnose(case)
    for field in ("root_component", "category", "cause_ref", "cost_usd"):
        assert field in out, field
    assert out["root_component"] == "knowledge_base"
