"""The ten tools, against every recorded case and (when it is up) the live stack."""
import json

import pytest
from conftest import collect_refs, make_env, retrieve_ref, run_all_tools, window

from tools import handlers, refs
from tools.output import MAX_CHARS

STUBS = {"replay_conversation", "search_playbooks", "similar_incidents"}


def test_every_tool_returns_json_under_the_size_cap(env):
    outputs = run_all_tools(env)
    assert set(outputs) == set(handlers.HANDLERS)
    for name, out in outputs.items():
        text = json.dumps(out)
        assert json.loads(text) == out, name
        assert len(text) <= MAX_CHARS, f"{name} is {len(text)} chars"
        assert "Traceback" not in text, name
        if name in STUBS:
            assert out["error"] == "not_implemented" and out["hint"], name
        elif env.name == "live" and name == "query_metrics":
            continue  # Prometheus may have aged out the window
        else:
            assert "error" not in out, f"{name}: {out}"


def test_every_ref_in_every_output_resolves(env):
    seen: dict[str, int] = {}
    for name, out in run_all_tools(env).items():
        for ref in collect_refs(out):
            resolved = refs.resolve_ref(env.backend, ref)
            assert resolved["ref"] == ref and resolved["kind"] == refs.parse_ref(ref).kind
            seen[name] = seen.get(name, 0) + 1
    expected = {"get_trace", "get_retrieved_chunks", "document_history", "search_logs",
                "recent_changes", "diff_prompt_versions"}
    assert expected <= set(seen), f"tools that returned no refs: {expected - set(seen)}"
    if env.name != "live":
        assert "query_metrics" in seen


def test_lists_carry_a_total(env):
    out = run_all_tools(env)
    assert out["get_trace"]["total_spans"] == len(out["get_trace"]["spans"])
    assert out["get_retrieved_chunks"]["total"] == len(out["get_retrieved_chunks"]["chunks"])
    assert out["document_history"]["total_versions"] == len(out["document_history"]["versions"])
    assert out["search_logs"]["total_matches"] >= out["search_logs"]["returned"] == len(out["search_logs"]["logs"])
    assert out["recent_changes"]["total"] >= out["recent_changes"]["returned"] == len(out["recent_changes"]["changes"])
    assert out["query_metrics"].get("total_series") or env.name == "live"


def test_get_trace_is_a_tree_with_the_attributes_an_investigator_needs(env):
    out = handlers.get_trace(env.backend, env.conversation_id)
    by_ref = {s["ref"]: s for s in out["spans"]}
    roots = [s for s in out["spans"] if s["parent_ref"] is None]
    assert len(roots) == 1 and roots[0]["name"] == "POST /chat"
    assert all(s["parent_ref"] in by_ref for s in out["spans"] if s["parent_ref"])
    names = {s["name"] for s in out["spans"]}
    assert {"retrieve", "llm.chat"} <= names
    attrs = by_ref[retrieve_ref(out)]["attrs"]
    assert {"retrieval.top_k", "retrieval.threshold", "retrieval.result_count", "retrieval.chunk_ids"} <= set(attrs)
    llm = next(s for s in out["spans"] if s["name"] == "llm.chat")
    assert "prompt.version" in llm["attrs"] and "gen_ai.request.model" in llm["attrs"]
    assert {"config.top_k", "config.similarity_threshold", "config.prompt_version"} <= set(roots[0]["attrs"])


def test_get_trace_accepts_a_trace_id_too(snap):
    by_conversation = handlers.get_trace(snap.backend, snap.conversation_id)
    by_trace_id = handlers.get_trace(snap.backend, by_conversation["trace_id"])
    assert by_trace_id["spans"] == by_conversation["spans"]


def test_silent_tool_failure_shows_an_empty_ok_tool_span(snap):
    if snap.kind != "silent_tool_failure":
        pytest.skip("not a silent-tool-failure case")
    spans = handlers.get_trace(snap.backend, snap.conversation_id)["spans"]
    tool_spans = [s for s in spans if s["name"].startswith("tool.")]
    assert tool_spans, "the alert question should have called a tool"
    assert all(s["attrs"]["tool.result_count"] == 0 for s in tool_spans)
    assert all(s["status"] == "ok" for s in tool_spans)  # the failure is silent
    logs = handlers.search_logs(snap.backend, "tool", "timed out")
    assert logs["total_matches"] >= 1 and logs["logs"][0]["level"] == "WARN"


def test_threshold_fault_retrieves_nothing(snap):
    if snap.kind != "threshold_too_strict":
        pytest.skip("not a threshold case")
    trace = handlers.get_trace(snap.backend, snap.conversation_id)
    chunks = handlers.get_retrieved_chunks(snap.backend, retrieve_ref(trace))
    assert chunks["total"] == len(chunks["chunks"]) <= chunks["top_k"]
    assert chunks["threshold"] > 0.3  # the healthy default


def test_stale_document_shows_both_versions_with_indexed_chunks(snap):
    if snap.kind != "stale_document":
        pytest.skip("not a stale-document case")
    trace = handlers.get_trace(snap.backend, snap.conversation_id)
    chunks = handlers.get_retrieved_chunks(snap.backend, retrieve_ref(trace))["chunks"]
    stale = []
    for doc_id in sorted({c["doc_id"] for c in chunks}):
        versions = handlers.document_history(snap.backend, doc_id)["versions"]
        if len([v for v in versions if v["indexed_chunk_count"] > 0]) >= 2:
            stale.append(doc_id)
    assert stale, "expected a retrieved document with two indexed versions"
    assert {c["version"] for c in chunks if c["doc_id"] == stale[0]} >= {"v1"}


def test_missing_document_shows_a_version_with_no_indexed_chunks(snap):
    if snap.kind != "missing_document":
        pytest.skip("not a missing-document case")
    deletes = [c for c in handlers.recent_changes(snap.backend)["changes"] if c["kind"] == "doc_delete"]
    assert deletes, "the removal should be in the change history"
    doc_id = deletes[0]["target"].removeprefix("doc:")
    versions = handlers.document_history(snap.backend, doc_id)["versions"]
    assert versions and all(v["indexed_chunk_count"] == 0 and v["chunk_count"] > 0 for v in versions)


def test_prompt_regression_shows_in_the_prompt_diff(snap):
    if snap.kind != "prompt_regression":
        pytest.skip("not a prompt-regression case")
    prompts = snap.backend.prompt_versions()
    assert len(prompts) >= 2
    active = next(v["version"] for v in prompts if v["active"])
    other = next(v["version"] for v in prompts if not v["active"])  # injected edits are back-dated: order says nothing
    out = handlers.diff_prompt_versions(snap.backend, other, active)
    assert not out["identical"] and out["diff"].count("\n") < 80
    trace = handlers.get_trace(snap.backend, snap.conversation_id)
    llm = next(s for s in trace["spans"] if s["name"] == "llm.chat")
    assert llm["attrs"]["prompt.version"] == active


def test_document_history_never_says_which_version_is_current(snap):
    forbidden = {"current", "is_current", "latest", "correct", "is_correct", "active", "stale", "valid"}
    for doc_id in ("refund-policy", "shipping-domestic", "returns-process"):
        out = handlers.document_history(snap.backend, doc_id)
        assert "error" not in out
        for version in out["versions"]:
            assert not forbidden & set(version), version
        assert not forbidden & set(out)
        assert [v["published_at"] for v in out["versions"]] == sorted(v["published_at"] for v in out["versions"])


def test_get_retrieved_chunks_truncates_text_and_says_so(snap):
    trace = handlers.get_trace(snap.backend, snap.conversation_id)
    out = handlers.get_retrieved_chunks(snap.backend, retrieve_ref(trace))
    assert all(len(c["text"]) <= handlers.TEXT_LIMIT for c in out["chunks"])
    full = {c["chunk_id"]: c["text"] for c in snap.backend.chunks(
        [r.removeprefix("chunk:") for r in collect_refs(out)])}
    cut = any(len(t) > handlers.TEXT_LIMIT for t in full.values())
    assert out.get("truncated", False) == cut


def test_search_logs_filters_by_component_query_and_window(snap):
    alert = snap.alert
    start, end = window(alert)
    everything = handlers.search_logs(snap.backend, "", "", start, end, 50)
    assert everything["total_matches"] >= 1
    chat = handlers.search_logs(snap.backend, "chat", "", start, end, 50)
    assert chat["logs"] and all(r["component"] == "chat" for r in chat["logs"])
    none = handlers.search_logs(snap.backend, "", "no-such-phrase-anywhere", start, end)
    assert none["total_matches"] == 0 and none["logs"] == []
    outside = handlers.search_logs(snap.backend, "", "", "2000-01-01T00:00:00Z", "2000-01-02T00:00:00Z")
    assert outside["total_matches"] == 0
    stamps = [r["timestamp"] for r in everything["logs"]]
    assert stamps == sorted(stamps)


def test_recent_changes_lists_decoys_in_the_same_format(snap):
    out = handlers.recent_changes(snap.backend, limit=100)
    assert out["total"] >= out["returned"] > 0
    keys = {frozenset(c) for c in out["changes"]}
    assert keys == {frozenset({"ref", "kind", "target", "description", "author", "created_at"})}
    stamps = [c["created_at"] for c in out["changes"]]
    assert stamps == sorted(stamps, reverse=True)
    window_start = snap.alert["window"]["start"]
    recent = handlers.recent_changes(snap.backend, start=window_start)
    assert recent["total"] <= out["total"]


def test_query_metrics_summarizes_and_caps_points(snap):
    out = handlers.query_metrics(snap.backend, "kestrel_canary_accuracy")
    assert out["total_series"] >= 1
    for series in out["series"]:
        assert 1 <= len(series["points"]) <= handlers.POINTS_PER_SERIES
        values = [p["value"] for p in series["points"]]
        assert series["min"] <= min(values) and max(values) <= series["max"]
        assert series["min"] <= series["mean"] <= series["max"]


def test_errors_come_back_as_data(snap):
    b = snap.backend
    cases = [
        handlers.get_trace(b, "conv-does-not-exist"),
        handlers.get_retrieved_chunks(b, "not-a-ref"),
        handlers.get_retrieved_chunks(b, "prompt:v1"),
        handlers.get_retrieved_chunks(b, f"span:{'0' * 32}/{'1' * 16}"),
        handlers.document_history(b, "no-such-doc"),
        handlers.diff_prompt_versions(b, "v1", "v999"),
        handlers.query_metrics(b, "no_such_metric"),
        handlers.search_logs(b, start="yesterday"),
        handlers.recent_changes(b, start="2026-10-02T00:00:00Z", end="2026-10-01T00:00:00Z"),
    ]
    for out in cases:
        assert set(out) == {"error", "hint"} and out["error"] and out["hint"], out
        assert "Traceback" not in json.dumps(out) and "File \"" not in json.dumps(out)


def test_get_retrieved_chunks_rejects_a_span_that_did_not_retrieve(snap):
    trace = handlers.get_trace(snap.backend, snap.conversation_id)
    llm = next(s for s in trace["spans"] if s["name"] == "llm.chat")
    out = handlers.get_retrieved_chunks(snap.backend, llm["ref"])
    assert "not a retrieval span" in out["error"] and "retrieve" in out["hint"]


def test_an_unexpected_backend_failure_is_not_a_stack_trace(snap, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise RuntimeError("database is on fire")
    monkeypatch.setattr(snap.backend, "changes", boom)
    out = handlers.recent_changes(snap.backend)
    assert out["error"].startswith("RuntimeError") and out["hint"]
    assert "Traceback" not in json.dumps(out)


def test_live_and_snapshot_agree_on_the_trace():
    live = make_env("live")
    from conftest import CASES
    snap = make_env(next(c for c in CASES if load_conv(c) == live.conversation_id))
    a = handlers.get_trace(live.backend, live.conversation_id)
    b = handlers.get_trace(snap.backend, snap.conversation_id)
    assert a == b
    chunks_a = handlers.get_retrieved_chunks(live.backend, retrieve_ref(a))
    chunks_b = handlers.get_retrieved_chunks(snap.backend, retrieve_ref(b))
    assert [c["ref"] for c in chunks_a["chunks"]] == [c["ref"] for c in chunks_b["chunks"]]


def load_conv(case: str) -> str:
    from conftest import load_alert
    return load_alert(case)["conversation_id"]
