"""The live view is a reader. These tests pin the two properties that matter:

1. It prints facts out of the state it is handed, never a scripted sequence. A step that did not
   happen prints nothing, and the "no error span" line follows the real span statuses.
2. It can never change or break a run. An observer that raises is swallowed by the graph, and the
   view itself survives output a cp1252 console cannot encode.
"""
import io

from agent.trace_view import TraceView


def view(width: int = 96):
    buf = io.StringIO()
    return TraceView("case-900", stream=buf, width=width), buf


TRIAGE_OK = """Trace abc (3 spans):
  - POST /chat [ok, 1192.9ms] config.top_k=4 ref=span:abc/1
  - retrieve [ok, 23.3ms] retrieval.result_count=4 ref=span:abc/2
  - tool.lookup_order [ok, 8.6ms] tool.result_count=0 ref=span:abc/3
Changes in the 7 days before the alert: 0 (showing 0, newest first):"""

TRIAGE_ERR = """Trace abc (2 spans):
  - POST /chat [ok, 10ms] ref=span:abc/1
  - tool.lookup_order [ERROR, 8.6ms] ref=span:abc/2
Changes in the 7 days before the alert: 0 (showing 0, newest first):"""


def test_the_no_error_line_follows_the_real_span_statuses():
    """The premise of the project, counted rather than asserted."""
    v, buf = view()
    v.on_state({"triage": TRIAGE_OK, "transcript": [{"kind": "tool", "tool": "get_trace",
                                                     "args": {}, "result_chars": 10, "node": "triage"}]})
    out = buf.getvalue()
    assert "no span reports an error (3 spans, all OK)" in out


def test_an_error_span_is_reported_as_an_error_not_hidden():
    """If the trace did show an error, the view must say so - the line is a count, not a slogan."""
    v, buf = view()
    v.on_state({"triage": TRIAGE_ERR, "transcript": [{"kind": "tool", "tool": "get_trace",
                                                      "args": {}, "result_chars": 10, "node": "triage"}]})
    out = buf.getvalue()
    assert "1 of 2 spans report ERROR" in out
    assert "nothing here looks broken" not in out


def test_a_step_that_did_not_happen_prints_nothing():
    """No replay in the state means no replay line. The view cannot invent a beat."""
    v, buf = view()
    v.on_state({"transcript": [{"kind": "tool", "tool": "search_logs", "args": {"query": "timeout"},
                                "result_chars": 55, "node": "investigate"}], "evidence": []})
    out = buf.getvalue()
    assert "search_logs" in out
    assert "replay" not in out.lower()
    assert "VERIFY" not in out


def test_a_refused_tool_call_is_shown_not_swallowed():
    """A rejected patch is part of the real investigation; hiding it would flatter the agent."""
    v, buf = view()
    v.on_state({"transcript": [{"kind": "tool", "tool": "replay_conversation", "args": {},
                                "result_chars": 0, "node": "investigate",
                                "error": "patch rejected: tool_timeout_ms=5000 is not a value the log shows"}]})
    assert "refused:" in buf.getvalue()
    assert "patch rejected" in buf.getvalue()


def test_evidence_beyond_the_cap_is_counted_not_dropped_silently():
    """One broad search_logs can add twenty items; the view shows a few and says how many more."""
    v, buf = view()
    ev = [{"type": "log", "ref": f"log:{i}", "claim": f"line {i}"} for i in range(9)]
    v.on_state({"transcript": [{"kind": "tool", "tool": "search_logs", "args": {},
                                "result_chars": 99, "node": "investigate"}], "evidence": ev})
    out = buf.getvalue()
    assert "log:0" in out and "log:2" in out
    assert "6 more item(s)" in out


def test_the_gate_verdict_comes_from_the_gate():
    v, buf = view()
    v.on_state({"rounds": 1, "draft": {"root_component": "tool:lookup_order", "category": "tool_outage"},
                "gate": {"passed": False, "reason": "only 1 evidence type (trace)"}})
    out = buf.getvalue()
    assert "REJECTED" in out and "only 1 evidence type" in out
    assert "ACCEPTED" not in out


def test_an_abstention_is_reported_as_an_abstention():
    v, buf = view()
    v.finish({"abstained": True, "top_candidate": {"root_component": "retriever", "category": "config_change"},
              "missing_evidence": "only 1 evidence type relevant to retriever"}, None, None, None)
    out = buf.getvalue()
    assert "ABSTAINED" in out and "retriever" in out


def test_a_hallucinated_diagnosis_is_labelled_by_the_evaluator():
    v, buf = view()
    v.finish({"root_component": "model", "category": "model_change", "confidence": 0.5,
              "summary": "s", "evidence": [{"type": "log", "ref": "log:nope"}]},
             {"hallucinated": True, "checked": 1, "failures": ["log:nope not found"]}, None, None)
    out = buf.getvalue()
    assert "HALLUCINATED" in out
    assert "log:nope not found" in out


def test_output_survives_text_a_cp1252_console_cannot_encode():
    """The view must never be the thing that fails a run."""
    v, buf = view()
    v.alert({"type": "flagged_conversation", "question": "café — 你好 \U0001f600",
             "answer": "✓ ok", "reviewer_correction": "—"})
    out = buf.getvalue()
    assert "ALERT" in out
    out.encode("ascii")  # raises if any non-ascii leaked through


def test_an_observer_that_raises_cannot_fail_the_investigation(monkeypatch):
    """Runs the real investigate_alert loop with a stubbed graph and toolbox: an on_state that
    blows up on every state must not stop the run or change the state it returns."""
    import asyncio
    import contextlib

    from agent import graph

    states = [{"step": 1, "diagnosis": None}, {"step": 2, "diagnosis": {"root_component": "model"}}]

    class FakeGraph:
        async def astream(self, *a, **k):
            for s in states:
                yield s

    class FakeToolbox:
        @staticmethod
        @contextlib.asynccontextmanager
        async def start(env, errlog=None, **kw):
            yield FakeToolbox()

        async def definitions(self):
            return []

    monkeypatch.setattr(graph, "Toolbox", FakeToolbox)
    monkeypatch.setattr(graph, "build_graph", lambda *a, **k: FakeGraph())

    seen = []

    def boom(state):
        seen.append(state)
        raise RuntimeError("observer blew up")

    out = asyncio.run(graph.investigate_alert({"type": "flagged_conversation"}, on_state=boom))
    assert len(seen) == 2, "the observer was called for every state"
    assert out == states[-1], "the run finished and returned the real final state"
