"""Replay: patch validation, overrides, the store, and full replays with a scripted assistant model
in place of the real one. No API calls (the embedding model runs locally)."""
import json
from pathlib import Path

import pytest

from assistant import llm
from tools import replay
from tools.replay import PatchError, Snapshot, apply_patch, baseline, normalize_patch, target_component

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def snap(case: str) -> Snapshot:
    return Snapshot(FIXTURES / case)


# --- patches ------------------------------------------------------------------------------

@pytest.mark.parametrize("patch, target", [
    ({"kind": "use_doc_version", "doc_id": "warranty", "version": "v2"}, "knowledge_base"),
    ({"kind": "restore_doc", "doc_id": "warranty"}, "knowledge_base"),
    ({"kind": "set_config", "key": "similarity_threshold", "value": "0.3"}, "retriever"),
    ({"kind": "set_config", "key": "max_response_tokens", "value": 500}, "config"),
    ({"kind": "set_prompt_version", "version": "v1"}, "prompt"),
    ({"kind": "rerun_tool", "tool": "lookup_order"}, "tool:lookup_order"),
    ({"kind": "bypass_cache", "tool": "check_inventory"}, "tool:check_inventory"),
])
def test_each_patch_kind_targets_the_component_code_derives(patch, target):
    assert target_component(normalize_patch(patch)) == target


def test_the_caller_cannot_name_the_target():
    with pytest.raises(PatchError, match="does not take target_component"):
        normalize_patch({"kind": "rerun_tool", "tool": "lookup_order", "target_component": "knowledge_base"})


@pytest.mark.parametrize("patch", [None, {}, ""])
def test_no_patch_is_the_empty_patch(patch):
    assert normalize_patch(patch) == {"kind": "none"}


@pytest.mark.parametrize("patch, message", [
    ({"kind": "swap_model"}, "unknown patch kind"),
    ({"kind": "use_doc_version", "doc_id": "warranty"}, "needs version"),
    ({"kind": "set_prompt_version", "version": "v1", "doc_id": "x"}, "exactly one thing"),
    ("not json", "not JSON"),
])
def test_malformed_patches_are_rejected(patch, message):
    with pytest.raises(PatchError) as e:
        normalize_patch(patch)
    assert message in str(e.value) + e.value.hint


@pytest.mark.parametrize("case, patch, message", [
    ("case-003", {"kind": "use_doc_version", "doc_id": "warranty", "version": "v3"}, "no version 'v3'"),
    ("case-003", {"kind": "restore_doc", "doc_id": "no-such-doc"}, "no document"),
    ("case-003", {"kind": "set_prompt_version", "version": "v9"}, "no prompt version"),
    # not a value the change log or the trace shows the key holding: no invented values
    ("case-002", {"kind": "set_config", "key": "similarity_threshold", "value": "0.1"}, "not a value"),
    ("case-007", {"kind": "set_config", "key": "tool_timeout_ms", "value": "2000"}, "not a value"),
    ("case-003", {"kind": "set_config", "key": "chunk_size", "value": "300"}, "cannot be patched"),
    ("case-003", {"kind": "set_config", "key": "prompt_version", "value": "v1"}, "cannot be patched"),
    ("case-003", {"kind": "rerun_tool", "tool": "delete_orders"}, "no tool"),
    ("case-003", {"kind": "bypass_cache", "tool": "lookup_order"}, "has no cache"),
])
def test_patches_the_snapshot_does_not_back_are_rejected_in_code(case, patch, message):
    s = snap(case)
    with pytest.raises(PatchError, match=message):
        apply_patch(baseline(s), normalize_patch(patch), s)


def test_set_config_accepts_a_value_from_the_change_log_or_the_trace():
    s = snap("case-002")
    assert set(s.config_values("similarity_threshold")) == {"0.3", "0.85"}  # the diff's - and + sides
    ov = apply_patch(baseline(s), normalize_patch({"kind": "set_config", "key": "similarity_threshold",
                                                   "value": 0.3}), s)
    assert ov.config["similarity_threshold"] == "0.3"
    s7 = snap("case-007")  # no change record, but the trace shows the value it ran with
    assert s7.config_values("tool_timeout_ms") == ["1"]


def test_use_doc_version_indexes_exactly_that_version():
    s = snap("case-003")
    ov = apply_patch(baseline(s), normalize_patch({"kind": "use_doc_version", "doc_id": "warranty", "version": "v2"}), s)
    assert all(c.startswith("warranty@v2#") for c in ov.include_chunks)
    assert ov.exclude_chunks and all(c.startswith("warranty@v1#") for c in ov.exclude_chunks)


def test_baseline_serves_only_the_alert_traces_tool_calls():
    s = snap("case-007")
    assert len(s.tool_io) > len(s.recorded_calls()) == 1   # tool_io also holds the healthy pre-fault run
    (calls,) = baseline(s).tool_responses.values()
    assert calls[0]["timed_out"] and calls[0]["output"] == []


# --- the store ----------------------------------------------------------------------------

def test_store_lookup_matches_the_healthy_recording_column_for_column():
    s = snap("case-007")
    healthy = next(c for c in s.tool_io if c["tool_name"] == "lookup_order" and not c["timed_out"])
    rows = replay.Store(s.store, s.alert["fired_at"]).run("lookup_order", healthy["input"])
    assert rows == replay.ordered("lookup_order", healthy["output"])
    assert list(rows[0]) == list(replay.COLUMNS["lookup_order"])


def test_bypass_cache_reads_the_source_of_truth():
    s = snap("case-005")
    store = replay.Store(s.store, s.alert["fired_at"])
    cached = store.run("check_inventory", {"product_id": "P-005"})
    source = store.run("check_inventory", {"product_id": "P-005"}, bypass_cache=True)
    assert cached[0]["qty"] == 0 and source[0]["qty"] == 18
    assert store.cache_age_s(cached) > 2 * 24 * 3600


# --- answer check -------------------------------------------------------------------------

CHECK = {"must_contain": [r"\b(?:2|two)[- ]years?\b"], "must_not_contain": [r"\b(?:1|one)[- ]year\b"]}


@pytest.mark.parametrize("answer, verdict", [
    ("It has a 2-year warranty.", "match"),
    ("It has a 1-year warranty.", "no_match"),
    ("I don't know.", "no_match"),
    ("It is 2 years now; older docs said 1-year.", "inconclusive"),
])
def test_the_code_check(answer, verdict):
    assert replay.code_check(CHECK, answer) == verdict


def test_only_an_inconclusive_check_falls_back_to_the_judge():
    alert = {"question": "q", "reviewer_correction": "2 years", "answer_check": CHECK}
    calls = []

    def judge(q, c, a):
        calls.append(a)
        return True, {"model": "fake", "input_tokens": 0, "output_tokens": 0}

    assert replay.check_answer(alert, "a 2-year warranty", judge)["judge"] == "code"
    out = replay.check_answer(alert, "2 years now; it used to be 1-year", judge)
    assert out == {"matches_correction": True, "judge": "llm", "code_verdict": "inconclusive",
                   "judge_usage": {"model": "fake", "input_tokens": 0, "output_tokens": 0}}
    assert len(calls) == 1


# --- full replays with a scripted model ---------------------------------------------------

def response(blocks, stop="end_turn"):
    return llm.Response(blocks, stop, "fake", "r1", llm.Usage(100, 20))


@pytest.fixture
def warranty_model(monkeypatch):
    """Answers from whatever warranty chunks it was given, like the real assistant would."""
    def chat(cfg, system, messages, tools, turn):
        docs = messages[0]["content"]
        text = "The warranty is 1 year." if "warranty@v1#" in docs else "The warranty is 2 years."
        return response([llm.Block("text", text=text)])
    monkeypatch.setattr(llm, "chat", chat)


@pytest.fixture
def inventory_model(monkeypatch):
    """Calls check_inventory, then reports the qty it got back."""
    def chat(cfg, system, messages, tools, turn):
        if turn == 0:
            return response([llm.Block("tool_use", id="t1", name="check_inventory",
                                       input={"product_id": "P-005"})], "tool_use")
        rows = json.loads(messages[-1]["content"][0]["content"])
        qty = rows[0]["qty"] if rows else 0
        return response([llm.Block("text", text=f"In stock: {qty} units." if qty else "It is out of stock.")])
    monkeypatch.setattr(llm, "chat", chat)


@pytest.fixture(autouse=True)
def fresh_controls(monkeypatch):
    monkeypatch.setattr(replay, "_controls", {})


def test_a_fix_is_proven_only_against_an_unpatched_control(warranty_model, tmp_path):
    s, log = snap("case-003"), tmp_path / "replays.jsonl"
    rec = replay.run_replay(s, {"kind": "use_doc_version", "doc_id": "warranty", "version": "v2"}, log, judge=None)
    assert rec["matches_correction"] is True and rec["votes"] == [True] * replay.SAMPLES
    assert rec["target_component"] == "knowledge_base" and rec["control_matches_correction"] is False
    assert rec["proves_fix"] is True
    assert not any(c.startswith("warranty@v1#") for c in rec["retrieved"])
    logged = replay.read_log(log)
    assert [r["purpose"] for r in logged] == ["control", "investigation"]
    assert logged[1]["ref"] == rec["ref"] and logged[1]["matches_correction"] is True


def test_replay_traces_are_marked_as_replays(warranty_model, tmp_path):
    rec = replay.run_replay(snap("case-003"), {}, tmp_path / "r.jsonl", judge=None)
    sample = rec["samples"][0]
    root = next(sp for sp in sample["spans"] if sp["parent_id"] is None)
    assert root["attrs"]["kestrel.replay"] is True and root["attrs"]["kestrel.replay_of"] == snap("case-003").alert["conversation_id"]
    # llm.chat's span is opened inside the function the fake replaces, so it is absent here
    assert {sp["name"] for sp in sample["spans"]} >= {"replay", "guardrail", "retrieve"}
    assert len({s["trace_id"] for s in rec["samples"]}) == replay.SAMPLES  # each sample is its own trace


def test_an_unrelated_patch_that_changes_nothing_does_not_prove_a_fix(warranty_model, tmp_path):
    rec = replay.run_replay(snap("case-003"), {"kind": "set_config", "key": "max_response_tokens", "value": "500"},
                            tmp_path / "r.jsonl", judge=None)
    assert rec["matches_correction"] is False and rec["proves_fix"] is False and rec["target_component"] == "config"


def test_bypass_cache_fixes_the_stale_stock_answer_and_rerun_does_not(inventory_model, tmp_path):
    s, log = snap("case-005"), tmp_path / "r.jsonl"
    stale = replay.run_replay(s, {}, log, judge=None)
    assert stale["matches_correction"] is False and stale["tools"][0]["served_from"] == "recording"
    rerun = replay.run_replay(s, {"kind": "rerun_tool", "tool": "check_inventory"}, log, judge=None)
    assert rerun["matches_correction"] is False and rerun["tools"][0]["served_from"] == "store"
    fixed = replay.run_replay(s, {"kind": "bypass_cache", "tool": "check_inventory"}, log, judge=None)
    assert fixed["proves_fix"] and fixed["target_component"] == "tool:check_inventory"


def test_the_mcp_tool_returns_a_summary_and_logs_the_replay(warranty_model, tmp_path, monkeypatch):
    from tools import handlers
    from tools.backend import SnapshotBackend

    monkeypatch.setenv("TRACEWISE_REPLAY_LOG", str(tmp_path / "log.jsonl"))
    b = SnapshotBackend("case-003", FIXTURES)
    out = handlers.replay_conversation(b, b._load("alert")["conversation_id"],
                                       {"kind": "use_doc_version", "doc_id": "warranty", "version": "v2"})
    assert out["proves_fix"] is True and out["replay_ref"].startswith("replay:")
    from tools.refs import resolve_ref
    assert resolve_ref(b, out["replay_ref"])["record"]["matches_correction"] is True
    bad = handlers.replay_conversation(b, "conv-nope", {})
    assert "no recorded conversation" in bad["error"]
