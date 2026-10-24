"""The graph and its budget, with a scripted model in place of the real one and the real MCP
server over a fixture snapshot. No API calls."""
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

DIAGNOSIS = {
    "root_component": "tool:lookup_order", "category": "tool_outage", "cause_ref": None, "confidence": 0.7,
    "summary": "lookup_order returned nothing.", "ruled_out": [], "suggested_fix": "", "abstained": False,
    "evidence": [{"type": "trace", "ref": "span:c3ccf0cb7092aa97ad23c056bad187ac/30253ebd5cda320e",
                  "claim": "tool span returned 0 rows"}],
}


def make_fake(script):
    """A Chat stand-in. `script(kind, turn_no, chat)` returns the Reply for each send."""
    class FakeChat:
        sent: list = []

        def __init__(self, system, tools=None, json_mode=False, model=None):
            self.kind = "investigate" if tools else "json"
            self.tools, self.turns = tools, 0

        def send(self, turn):
            self.turns += 1
            FakeChat.sent.append((self.kind, turn))
            return script(self.kind, self.turns, self)
    return FakeChat


def run(monkeypatch, script, budget=15):
    fake = make_fake(script)
    monkeypatch.setattr(graph_module, "Chat", fake)
    state = asyncio.run(graph_module.investigate_alert(ALERT, ENV, model="fake", budget=budget))
    return state, fake


def test_a_model_that_never_stops_is_cut_off_at_the_budget(monkeypatch):
    calls = {"json": 0}

    def script(kind, n, chat):
        if kind == "investigate":
            return Reply("", [ToolCall(f"c{n}", "search_logs", {"query": "timed out"})], Usage(10, 5))
        calls["json"] += 1
        if calls["json"] == 1:
            return Reply(json.dumps({"candidates": []}))
        return Reply(json.dumps(DIAGNOSIS))

    state, fake = run(monkeypatch, script)
    assert state["tool_calls_used"] == 15
    tool_entries = [e for e in state["transcript"] if e["kind"] == "tool"]
    assert len([e for e in tool_entries if "skipped" not in e]) == 15
    assert state["diagnosis"]["root_component"] == "tool:lookup_order"
    assert [e["node"] for e in state["transcript"]][-1] == "conclude"


def test_extra_calls_in_the_last_turn_are_refused_not_run(monkeypatch):
    def script(kind, n, chat):
        if kind == "investigate":
            return Reply("", [ToolCall(f"c{n}{i}", "search_logs", {}) for i in range(4)])
        return Reply(json.dumps(DIAGNOSIS))  # hypothesize gets no valid candidates from this; that is fine

    state, _ = run(monkeypatch, script, budget=5)
    tools = [e for e in state["transcript"] if e["kind"] == "tool"]
    assert len([e for e in tools if "skipped" not in e]) == 5
    assert any(e.get("skipped") == "budget" for e in tools)


def test_the_full_path_triage_hypothesize_investigate_conclude(monkeypatch):
    seen_json = []

    def script(kind, n, chat):
        if kind == "investigate":
            if n == 1:
                return Reply("checking logs", [ToolCall("a", "search_logs", {"component": "tool"})])
            return Reply("lookup_order timed out; done.")
        seen_json.append(n)
        if len(seen_json) == 1:
            return Reply(json.dumps({"candidates": [
                {"root_component": "tool:lookup_order", "category": "tool_outage", "rationale": "0 rows", "score": 0.6},
                {"root_component": "made_up", "category": "nonsense", "rationale": "dropped", "score": 0.1}]}))
        return Reply(json.dumps(DIAGNOSIS))

    state, fake = run(monkeypatch, script)
    nodes = [e["node"] for e in state["transcript"]]
    assert nodes[:2] == ["triage", "triage"] and "hypothesize" in nodes and nodes[-1] == "conclude"
    assert [c["root_component"] for c in state["candidates"]] == ["tool:lookup_order"]  # vocabulary enforced
    assert state["tool_calls_used"] == 3  # get_trace + recent_changes + search_logs
    assert state["notes"] == "lookup_order timed out; done."
    types = {e["type"] for e in state["evidence"]}
    assert {"trace", "log"} <= types  # this outage left no change in the 7-day window
    # the tool results went back to the model, with the calls left
    results = [t for k, t in fake.sent if k == "investigate" and isinstance(t, list)]
    assert results and results[0][0][1]["_calls_left"] == 12


def test_invalid_json_twice_abstains(monkeypatch):
    def script(kind, n, chat):
        if kind == "investigate":
            return Reply("nothing to add")
        return Reply("not json at all")

    state, _ = run(monkeypatch, script)
    d = state["diagnosis"]
    assert d["abstained"] is True and d["root_component"] is None
    assert [e["node"] for e in state["transcript"]][-2:] == ["conclude", "conclude-retry"]


def test_one_bad_diagnosis_gets_one_retry(monkeypatch):
    replies = iter([json.dumps({"candidates": []}),
                    json.dumps({**DIAGNOSIS, "category": "stale_knowledge"}),  # not in the vocabulary
                    json.dumps(DIAGNOSIS)])

    def script(kind, n, chat):
        return Reply("done") if kind == "investigate" else Reply(next(replies))

    state, fake = run(monkeypatch, script)
    assert state["diagnosis"]["category"] == "tool_outage" and not state["diagnosis"]["abstained"]
    retry_prompt = [t for k, t in fake.sent if k == "json"][-1]
    assert "failed validation" in retry_prompt and "category" in retry_prompt


def test_the_stub_tools_are_not_offered(monkeypatch):
    async def names():
        async with Toolbox.start(ENV) as tb:
            return {t["name"] for t in await tb.definitions()}
    offered = asyncio.run(names())
    assert "replay_conversation" not in offered and "get_trace" in offered and len(offered) == 7


def test_the_system_prompt_names_no_injected_fault():
    text = graph_module.system_prompt().lower()
    for word in ("stale_document", "two versions", "threshold_too_strict", "tool_timeout_ms", "decoy",
                 "silent_tool_failure", "inventory_cache", "1ms"):
        assert word not in text, word
