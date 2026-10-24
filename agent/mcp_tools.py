"""The Day 4 MCP server, seen from the investigator: start it over stdio, list its tools in
Anthropic's tool shape, and route calls to it through one function with the budgets in code:
TOOL_BUDGET tool calls in all, of which at most REPLAY_BUDGET replays."""
import json
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL_BUDGET = int(os.environ.get("INVESTIGATOR_TOOL_BUDGET", "15"))
REPLAY_BUDGET = int(os.environ.get("INVESTIGATOR_REPLAY_BUDGET", "3"))
TOOL_TIMEOUT = timedelta(seconds=float(os.environ.get("INVESTIGATOR_TOOL_TIMEOUT_S", "60")))
# A replay runs the assistant several times, plus an unpatched control the first time.
REPLAY_TIMEOUT = timedelta(seconds=float(os.environ.get("INVESTIGATOR_REPLAY_TIMEOUT_S", "300")))
REPLAY = "replay_conversation"
# Stubs until Day 10 (playbooks, incidents): offering them only burns budget.
NOT_YET = {"search_playbooks", "similar_incidents"}


class Toolbox:
    def __init__(self, session, budget: int = TOOL_BUDGET, replay_budget: int = REPLAY_BUDGET) -> None:
        self.session = session
        self.budget = budget
        self.used = 0
        self.replay_budget = replay_budget
        self.replays = 0

    @classmethod
    @asynccontextmanager
    async def start(cls, env: dict[str, str] | None = None, budget: int = TOOL_BUDGET,
                    errlog: Path | None = None):
        """`errlog`: where the server's stderr goes (default: discarded)."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable, args=["-m", "tools.server"], cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT), **(env or {})})
        async with AsyncExitStack() as stack:
            log = stack.enter_context(open(errlog or os.devnull, "w", encoding="utf-8"))
            read, write = await stack.enter_async_context(stdio_client(params, errlog=log))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            yield cls(session, budget)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.budget

    async def definitions(self) -> list[dict]:
        listed = await self.session.list_tools()
        return [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
                for t in listed.tools if t.name not in NOT_YET]

    async def call_tool(self, name: str, args: dict) -> tuple[dict, dict]:
        """Run one tool. Returns (result, transcript entry). Over budget, the tool is not called and
        the result says so; the graph then routes to conclude."""
        if self.exhausted:
            result = {"error": "budget_exhausted",
                      "hint": f"all {self.budget} tool calls are used; stop and conclude"}
            return result, {"kind": "tool", "tool": name, "args": args, "skipped": "budget", "result_chars": 0}
        if name == REPLAY and self.replays >= self.replay_budget:
            result = {"error": "replay_budget_exhausted",
                      "hint": f"all {self.replay_budget} replays are used; conclude with the evidence you have"}
            return result, {"kind": "tool", "tool": name, "args": args, "skipped": "replay_budget", "result_chars": 0}
        self.used += 1
        r = await self.session.call_tool(name, args,
                                         read_timeout_seconds=REPLAY_TIMEOUT if name == REPLAY else TOOL_TIMEOUT)
        text = "".join(getattr(c, "text", "") for c in r.content)
        try:
            result = json.loads(text)
        except ValueError:
            result = {"error": "tool returned non-JSON output", "hint": text[:200]}
        if r.isError and "error" not in result:
            result = {"error": text[:300], "hint": "check the arguments against the tool's schema"}
        error = result.get("error") if isinstance(result, dict) else None
        if name == REPLAY and not error:  # a rejected patch runs nothing, so it costs no replay
            self.replays += 1
        entry = {"kind": "tool", "n": self.used, "tool": name, "args": args, "result_chars": len(text),
                 "error": error}
        return result, entry
