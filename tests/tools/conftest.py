"""Fixtures for the tool tests.

Every backend-agnostic test runs once per recorded case in tests/fixtures/ and once against the
live stack (skipped when the stack is down or does not know the fixture conversations).

tests/fixtures/<case_id>/ is a copy of eval/runs/<case_id>/ from a Day 3 run.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tools import handlers, refs
from tools.backend import Backend, LiveBackend, SnapshotBackend

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CASES = sorted(p.name for p in FIXTURES.iterdir() if (p / "alert.json").exists()) if FIXTURES.is_dir() else []

# What each fixture is, for the tests that check a specific symptom. Tests may know this; the
# tools and their backends never do.
KIND = {
    "case-001": "prompt_regression",
    "case-002": "threshold_too_strict",
    "case-003": "stale_document",
    "case-005": "stale_tool_data",
    "case-006": "missing_document",
    "case-007": "silent_tool_failure",
}


@dataclass
class Env:
    name: str
    backend: Backend
    alert: dict

    @property
    def conversation_id(self) -> str:
        return self.alert["conversation_id"]

    @property
    def kind(self) -> str | None:
        return KIND.get(self.name)


def load_alert(case: str) -> dict:
    return json.loads((FIXTURES / case / "alert.json").read_text(encoding="utf-8"))


def make_env(param: str) -> Env:
    if param != "live":
        return Env(param, SnapshotBackend(param, FIXTURES), load_alert(param))
    backend = LiveBackend()
    for case in CASES:
        alert = load_alert(case)
        try:
            backend.trace_id_for(alert["conversation_id"])
            backend.changes(None, None)
            backend.metric_names()
            return Env("live", backend, alert)
        except Exception:
            continue
    pytest.skip("live stack is down or has none of the fixture conversations")


@pytest.fixture(params=[*CASES, "live"])
def env(request) -> Env:
    return make_env(request.param)


@pytest.fixture(params=CASES)
def snap(request) -> Env:
    return make_env(request.param)


def window(alert: dict) -> tuple[str, str]:
    start = datetime.fromisoformat(alert["window"]["start"]) - timedelta(minutes=15)
    end = datetime.fromisoformat(alert["window"]["end"]) + timedelta(minutes=1)
    return start.isoformat(), end.isoformat()


def retrieve_ref(trace_out: dict) -> str:
    return next(s["ref"] for s in trace_out["spans"] if s["name"] == "retrieve")


def run_all_tools(env: Env) -> dict[str, dict]:
    """Call all ten tools the way an investigator would, chaining ids from earlier outputs."""
    b = env.backend
    start, end = window(env.alert)
    out = {"get_trace": handlers.get_trace(b, env.conversation_id)}
    out["get_retrieved_chunks"] = handlers.get_retrieved_chunks(b, retrieve_ref(out["get_trace"]))
    doc_ids = [c["doc_id"] for c in out["get_retrieved_chunks"]["chunks"] if "doc_id" in c] or ["refund-policy"]
    out["document_history"] = handlers.document_history(b, doc_ids[0])
    out["search_logs"] = handlers.search_logs(b, "", "", start, end, 20)
    out["recent_changes"] = handlers.recent_changes(b, "", "")
    versions = [v["version"] for v in b.prompt_versions()]
    out["diff_prompt_versions"] = handlers.diff_prompt_versions(b, versions[0], versions[-1])
    out["query_metrics"] = handlers.query_metrics(b, "kestrel_canary_accuracy", start, end)
    out["replay_conversation"] = handlers.replay_conversation(b, env.conversation_id, {"top_k": 8})
    out["search_playbooks"] = handlers.search_playbooks(b, "stale document")
    out["similar_incidents"] = handlers.similar_incidents(b, "wrong refund window")
    return out


def collect_refs(value, found: list[str] | None = None) -> list[str]:
    """Every ref in a tool output. A ref lives under a key named `ref`, `refs`, or ending in
    `_ref` / `_refs`; other fields (like a change's `target`, `doc:refund-policy`) are plain text."""
    found = [] if found is None else found
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "ref" or k.endswith("_ref"):
                if isinstance(v, str):
                    found.append(v)
            elif k == "refs" or k.endswith("_refs"):
                found.extend(x for x in v if isinstance(x, str))
            else:
                collect_refs(v, found)
    elif isinstance(value, list):
        for v in value:
            collect_refs(v, found)
    return found
