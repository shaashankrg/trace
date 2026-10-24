"""The graph, its budgets and its gate, with a scripted model in place of the real one and the real
MCP server over a fixture snapshot. No API calls."""
import asyncio
import json
from pathlib import Path

import pytest

from agent import graph as graph_module
from agent.llm import Reply, ToolCall, Usage
from agent.mcp_tools import Toolbox

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CASE = "case-007"
ENV = {"TRACEWISE_BACKEND": f"snapshot:{CASE}", "TRACEWISE_RUNS_DIR": str(FIXTURES)}
ALERT = json.loads((FIXTURES / CASE / "alert.json").read_text(encoding="utf-8"))

TOOL_SPAN = "span:c3ccf0cb7092aa97ad23c056bad187ac/30253ebd5cda320e"   # tool.lookup_order, 0 rows
TIMEOUT_LOG = "log:769a9660-4f81-46b4-b2c5-887857816d34"              # "lookup_order timed out after 1ms"
SPAN_ONLY = {
    "root_component": "tool:lookup_order", "category": "tool_outage", "cause_ref": None, "confidence": 0.7,
    "summary": "lookup_order returned nothing.", "ruled_out": [], "suggested_fix": "", "abstained": False,
    "evidence": [{"type": "trace", "ref": TOOL_SPAN, "claim": "tool span returned 0 rows",
                  "attribute": "tool.result_count", "value": 0}],
}
DIAGNOSIS = {**SPAN_ONLY, "evidence": SPAN_ONLY["evidence"] + [
    {"type": "log", "ref": TIMEOUT_LOG, "claim": "the tool timed out", "quote": "timed out after 1ms"}]}
CANDIDATES = {"candidates": [
    {"root_component": "tool:lookup_order", "category": "tool_outage", "rationale": "0 rows", "score": 0.6},
    {"root_component": "config", "category": "config_change", "rationale": "timeout", "score": 0.3},
    {"root_component": "made_up", "category": "nonsense", "rationale": "dropped", "score": 0.1}]}


def node_of(turn) -> str:
    if not isinstance(turn, str):
        return "tool_results"
    if "List 3 to 5 candidate" in turn:
        return "hypothesize"
    if "Write the diagnosis" in turn:
        return "draft"
    if "failed validation" in turn:
        return "retry"
    return "investigate"


def make_fake(script):
    """A Chat stand-in. `script(node, n, turn)` returns the Reply; n counts sends to that node."""
    class FakeChat:
        sent: list = []
        counts: dict = {}

        def __init__(self, system, tools=None, json_mode=False, model=None):
            self.tools = tools

        def send(self, turn):
            node = node_of(turn)
            if node == "tool_results":
                node = "investigate"
            FakeChat.counts[node] = FakeChat.counts.get(node, 0) + 1
            FakeChat.sent.append((node, turn))
            return script(node, FakeChat.counts[node], turn)
    return FakeChat


def run(monkeypatch, script, budget=15):
    fake = make_fake(script)
    monkeypatch.setattr(graph_module, "Chat", fake)
    state = asyncio.run(graph_module.investigate_alert(ALERT, ENV, model="fake", budget=budget))
    return state, fake


def nodes(state) -> list[str]:
    return [e["node"] for e in state["transcript"]]


def test_a_model_that_never_stops_is_cut_off_at_the_budget(monkeypatch):
    def script(node, n, turn):
        if node == "investigate":
            return Reply("", [ToolCall(f"c{n}", "search_logs", {"component": "tool"})], Usage(10, 5))
        return Reply(json.dumps(CANDIDATES if node == "hypothesize" else DIAGNOSIS))

    state, _ = run(monkeypatch, script)
    assert state["tool_calls_used"] == 15
    assert len([e for e in state["transcript"] if e["kind"] == "tool" and "skipped" not in e]) == 15
    assert state["gate"]["passed"] and state["diagnosis"]["root_component"] == "tool:lookup_order"
    assert nodes(state)[-3:] == ["draft", "verify", "conclude"]


def test_extra_calls_in_the_last_turn_are_refused_not_run(monkeypatch):
    def script(node, n, turn):
        if node == "investigate":
            return Reply("", [ToolCall(f"c{n}{i}", "search_logs", {}) for i in range(4)])
        return Reply(json.dumps(CANDIDATES if node == "hypothesize" else DIAGNOSIS))

    state, _ = run(monkeypatch, script, budget=5)
    tools = [e for e in state["transcript"] if e["kind"] == "tool"]
    assert len([e for e in tools if "skipped" not in e]) == 5
    assert any(e.get("skipped") == "budget" for e in tools)


def test_the_full_path_with_a_draft_the_gate_accepts(monkeypatch):
    def script(node, n, turn):
        if node == "investigate":
            if n == 1:
                return Reply("checking logs", [ToolCall("a", "search_logs", {"component": "tool"})])
            return Reply("lookup_order timed out; done.")
        return Reply(json.dumps(CANDIDATES if node == "hypothesize" else DIAGNOSIS))

    state, fake = run(monkeypatch, script)
    assert nodes(state)[:3] == ["triage", "triage", "hypothesize"]
    assert nodes(state)[-3:] == ["draft", "verify", "conclude"]
    assert [c["root_component"] for c in state["candidates"]] == ["tool:lookup_order", "config"]  # vocabulary
    assert state["tool_calls_used"] == 3 and state["rounds"] == 1
    assert state["gate"]["passed"] and state["gate"]["supporting_types"] == ["log", "trace"]
    assert state["notes"] == "lookup_order timed out; done."
    results = [t for k, t in fake.sent if k == "investigate" and isinstance(t, list)]
    assert results[0][0][1]["_calls_left"] == 12 and results[0][0][1]["_replays_left"] == 3


def test_a_rejected_draft_goes_back_to_hypothesize_with_the_reason(monkeypatch):
    def script(node, n, turn):
        if node == "investigate":
            if n == 2:  # after the rejection: go and get the log line
                return Reply("", [ToolCall("b", "search_logs", {"component": "tool"})])
            return Reply("done")
        if node == "hypothesize":
            return Reply(json.dumps(CANDIDATES))
        return Reply(json.dumps(SPAN_ONLY if n == 1 else DIAGNOSIS))

    state, fake = run(monkeypatch, script)
    assert state["rounds"] == 2 and state["gate"]["passed"]
    assert nodes(state).count("hypothesize") == 2 and nodes(state)[-1] == "conclude"
    gates = [e for e in state["transcript"] if e["kind"] == "gate"]
    assert not gates[0]["passed"] and "only 1 evidence type" in gates[0]["reason"]
    second_hypothesize = [t for k, t in fake.sent if k == "hypothesize"][1]
    assert "rejected" in second_hypothesize and "only 1 evidence type" in second_hypothesize
    resumed = [t for k, t in fake.sent if k == "investigate" and isinstance(t, str)][1]
    assert "rejected by the evidence check" in resumed


def test_never_enough_evidence_abstains_with_the_top_candidate(monkeypatch):
    def script(node, n, turn):
        if node == "investigate":
            return Reply("done")
        return Reply(json.dumps(CANDIDATES if node == "hypothesize" else SPAN_ONLY))

    state, _ = run(monkeypatch, script)
    d = state["diagnosis"]
    assert d["abstained"] and d["root_component"] is None and state["rounds"] == graph_module.MAX_ROUNDS
    assert d["top_candidate"] == {"root_component": "tool:lookup_order", "category": "tool_outage"}
    assert [c["root_component"] for c in d["top_candidates"]] == ["tool:lookup_order", "config"]
    assert "only 1 evidence type" in d["missing_evidence"]
    assert nodes(state)[-2:] == ["verify", "abstain"]


def test_a_cited_ref_no_tool_returned_is_rejected(monkeypatch):
    invented = {**DIAGNOSIS, "evidence": DIAGNOSIS["evidence"] + [
        {"type": "chunk", "ref": "chunk:refund-policy@v9#0", "claim": "made up"}]}

    def script(node, n, turn):
        if node == "investigate":
            return Reply("", [ToolCall("a", "search_logs", {"component": "tool"})]) if n == 1 else Reply("done")
        return Reply(json.dumps(CANDIDATES if node == "hypothesize" else invented))

    state, _ = run(monkeypatch, script)
    gate = next(e for e in state["transcript"] if e["kind"] == "gate")
    assert not gate["passed"] and gate["unresolved"] == ["chunk:refund-policy@v9#0"]
    assert state["diagnosis"]["abstained"]


def test_invalid_json_every_time_abstains_after_the_rounds(monkeypatch):
    def script(node, n, turn):
        if node == "investigate":
            return Reply("nothing to add")
        return Reply(json.dumps(CANDIDATES) if node == "hypothesize" else "not json at all")

    state, _ = run(monkeypatch, script)
    d = state["diagnosis"]
    assert d["abstained"] is True and d["root_component"] is None
    assert d["top_candidate"]["root_component"] == "tool:lookup_order"  # from the candidates
    assert nodes(state).count("draft-retry") == graph_module.MAX_ROUNDS
    assert "no valid diagnosis" in d["missing_evidence"]


def test_one_bad_diagnosis_gets_one_retry(monkeypatch):
    def script(node, n, turn):
        if node == "investigate":
            return Reply("", [ToolCall("a", "search_logs", {"component": "tool"})]) if n == 1 else Reply("done")
        if node == "hypothesize":
            return Reply(json.dumps(CANDIDATES))
        if node == "draft":
            return Reply(json.dumps({**DIAGNOSIS, "category": "stale_knowledge"}))  # not in the vocabulary
        return Reply(json.dumps(DIAGNOSIS))

    state, fake = run(monkeypatch, script)
    assert state["diagnosis"]["category"] == "tool_outage" and not state["diagnosis"]["abstained"]
    retry_prompt = [t for k, t in fake.sent if k == "retry"][-1]
    assert "failed validation" in retry_prompt and "category" in retry_prompt


def test_replay_is_offered_and_the_day_10_stubs_are_not():
    async def names():
        async with Toolbox.start(ENV) as tb:
            return {t["name"] for t in await tb.definitions()}
    offered = asyncio.run(names())
    assert "replay_conversation" in offered and "get_trace" in offered and len(offered) == 8
    assert not offered & {"search_playbooks", "similar_incidents"}


class FakeSession:
    """Answers every call_tool with a canned replay result (or error), counting the calls."""

    def __init__(self, error: bool = False):
        self.calls, self.error = 0, error

    async def call_tool(self, name, args, read_timeout_seconds=None):
        from types import SimpleNamespace
        self.calls += 1
        body = {"error": "patch rejected", "hint": "x"} if self.error else {"replay_ref": f"replay:{self.calls}"}
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(body))], isError=False)


def test_the_replay_budget_is_enforced_in_code():
    tb = Toolbox(FakeSession(), budget=15, replay_budget=3)
    results = [asyncio.run(tb.call_tool("replay_conversation", {"conversation_id": "c", "patch": {}}))
               for _ in range(5)]
    assert tb.replays == 3 and tb.session.calls == 3
    assert [r[0].get("error") for r in results[3:]] == ["replay_budget_exhausted"] * 2
    assert results[4][1]["skipped"] == "replay_budget"


def test_a_rejected_patch_costs_a_tool_call_but_no_replay():
    tb = Toolbox(FakeSession(error=True), budget=15, replay_budget=3)
    for _ in range(4):
        asyncio.run(tb.call_tool("replay_conversation", {"conversation_id": "c", "patch": {"kind": "x"}}))
    assert tb.replays == 0 and tb.used == 4


def test_the_system_prompt_names_no_injected_fault():
    text = graph_module.system_prompt().lower()
    for word in ("stale_document", "two versions", "threshold_too_strict", "tool_timeout_ms", "decoy",
                 "silent_tool_failure", "inventory_cache", "1ms"):
        assert word not in text, word
