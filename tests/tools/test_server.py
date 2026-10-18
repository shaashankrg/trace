"""The MCP server itself: start it as a subprocess and talk to it over stdio, like the Inspector
or the Day 5 agent will."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from conftest import CASES, FIXTURES, load_alert, retrieve_ref

from tools import handlers

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = set(handlers.HANDLERS)


async def session_calls(case: str, calls: list[tuple[str, dict]]):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "tools.server"], cwd=str(ROOT),
        env={**os.environ, "TRACEWISE_BACKEND": f"snapshot:{case}", "TRACEWISE_RUNS_DIR": str(FIXTURES),
             "PYTHONPATH": str(ROOT)})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            results = [await session.call_tool(name, args) for name, args in calls]
            return listed, results


def payload(result) -> dict:
    assert not result.isError, result.content
    return json.loads(result.content[0].text)


@pytest.fixture(scope="module")
def case() -> str:
    if not CASES:
        pytest.skip("no fixtures")
    return CASES[0]


def test_server_lists_all_ten_tools_with_descriptions(case):
    listed, _ = asyncio.run(session_calls(case, []))
    tools = {t.name: t for t in listed.tools}
    assert set(tools) == EXPECTED
    assert all(t.description and len(t.description) > 20 for t in tools.values())
    assert not tools["search_logs"].inputSchema.get("required")  # every search_logs argument is optional
    assert tools["get_trace"].inputSchema["required"] == ["conversation_id"]
    assert tools["replay_conversation"].inputSchema["required"] == ["conversation_id", "patch"]


def test_a_tool_call_round_trips_over_stdio(case):
    conv = load_alert(case)["conversation_id"]
    _, (trace, chunks, bad, stub) = asyncio.run(session_calls(case, [
        ("get_trace", {"conversation_id": conv}),
        ("get_retrieved_chunks", {"span_ref": "span:" + "0" * 32 + "/" + "1" * 16}),
        ("document_history", {"doc_id": "no-such-doc"}),
        ("similar_incidents", {"summary": "wrong refund window"}),
    ]))
    t = payload(trace)
    assert t["conversation_id"] == conv and t["total_spans"] == len(t["spans"]) > 0
    assert set(payload(chunks)) == {"error", "hint"}
    assert set(payload(bad)) == {"error", "hint"}
    assert payload(stub)["error"] == "not_implemented"
    # a second call can use a ref from the first: this is the investigation loop
    _, (chunk_result,) = asyncio.run(session_calls(case, [
        ("get_retrieved_chunks", {"span_ref": retrieve_ref(t)})]))
    assert payload(chunk_result)["total"] == len(payload(chunk_result)["chunks"])
