"""The investigator graph (Day 5: no verification gate yet).

    triage ──► hypothesize ──► investigate ──► conclude
                                 │    ▲
                                 └────┘  one model turn per visit, until it says it is done
                                         or the tool budget is gone

Budget (F7): Toolbox.call_tool refuses past the budget, the router sends the graph to conclude
as soon as it is used up, and LangGraph's recursion_limit is the backstop.
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from agent import evidence as ev
from agent.llm import Chat, Reply, model_name, parse_json
from agent.mcp_tools import Toolbox
from agent.schema import CATEGORIES, COMPONENTS, Diagnosis, abstain, valid_component
from agent.state import InvestigationState

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "investigator.md"
RECURSION_LIMIT = 40
MAX_POOL_IN_PROMPT = 120


def system_prompt() -> str:
    text = PROMPT_PATH.read_text(encoding="utf-8")
    return (text.replace("{components}", "\n".join(f"- `{k}`: {v}" for k, v in COMPONENTS.items()))
                .replace("{categories}", "\n".join(f"- `{k}`: {v}" for k, v in CATEGORIES.items())))


def llm_entry(node: str, reply: Reply, model: str) -> dict:
    u = reply.usage
    return {"kind": "llm", "node": node, "model": model, "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens, "thinking_tokens": u.thinking_tokens,
            "cost_usd": round(u.cost(model), 6), "tool_calls": [c.name for c in reply.tool_calls],
            "text": reply.text[:500]}


def alert_text(alert: dict) -> str:
    keep = ("type", "fired_at", "window", "conversation_id", "trace_id", "question", "answer", "reviewer_correction")
    return json.dumps({k: alert[k] for k in keep if k in alert}, indent=1)


def pool_text(pool: list[dict]) -> str:
    # Non-trace evidence first: the trace is already summarized in triage.
    ordered = sorted(pool, key=lambda e: e["type"] == "trace")[:MAX_POOL_IN_PROMPT]
    return "\n".join(f"- [{e['type']}] {e['ref']} :: {e['claim']}" for e in ordered)


def summarize_triage(trace: dict, changes: dict) -> str:
    lines = []
    if "error" in trace:
        lines.append(f"get_trace failed: {trace['error']}")
    else:
        lines.append(f"Trace {trace['trace_id']} ({trace['total_spans']} spans):")
        for s in trace["spans"]:
            keys = ev.ROOT_KEYS if s.get("parent_ref") is None else ev.SPAN_KEYS
            detail = ev._attrs(s.get("attrs", {}), keys)
            if s["name"] == "retrieve":
                detail += f", chunk_ids={s['attrs'].get('retrieval.chunk_ids')}"
            lines.append(f"  - {s['name']} [{s['status']}, {s['duration_ms']}ms] {detail}  ref={s['ref']}")
    if "error" in changes:
        lines.append(f"recent_changes failed: {changes['error']}")
    else:
        lines.append(f"Changes in the 7 days before the alert: {changes['total']} "
                     f"(showing {changes['returned']}, newest first):")
        lines += [f"  - {c['created_at']} {c['kind']} {c['target']} by {c['author']}: {c['description']}  ref={c['ref']}"
                  for c in changes["changes"]]
    return "\n".join(lines)


async def ask(chat: Chat, turn) -> Reply:
    return await asyncio.to_thread(chat.send, turn)


def build_graph(toolbox: Toolbox, tools: list[dict], model: str | None = None):
    model = model or model_name()
    system = system_prompt()
    session: dict = {}  # the investigate conversation, carried across visits of that node

    async def triage(state: InvestigationState) -> dict:
        alert = state["alert"]
        fired = datetime.fromisoformat(alert["fired_at"])
        trace, t1 = await toolbox.call_tool("get_trace", {"conversation_id": alert["conversation_id"]})
        changes, t2 = await toolbox.call_tool("recent_changes", {
            "start": (fired - timedelta(days=7)).isoformat(), "end": fired.isoformat()})
        new = ev.from_result("get_trace", trace) + ev.from_result("recent_changes", changes)
        return {"triage": summarize_triage(trace, changes), "evidence": ev.merge([], new),
                "transcript": [{**t1, "node": "triage"}, {**t2, "node": "triage"}],
                "tool_calls_used": toolbox.used, "replays_used": 0}

    async def hypothesize(state: InvestigationState) -> dict:
        chat = Chat(system, json_mode=True, model=model)
        reply = await ask(chat, 
            f"Alert:\n{alert_text(state['alert'])}\n\nTriage:\n{state['triage']}\n\n"
            "List 3 to 5 candidate root causes, most likely first, as JSON: "
            '{"candidates": [{"root_component": "...", "category": "...", "rationale": "one sentence", '
            '"score": 0.0-1.0}]}. Use only the vocabulary in your instructions.')
        try:
            raw = parse_json(reply.text)
            raw = raw.get("candidates", []) if isinstance(raw, dict) else raw
        except ValueError:
            raw = []
        candidates = [{"root_component": c.get("root_component", ""), "category": c.get("category", ""),
                       "rationale": str(c.get("rationale", ""))[:300], "score": float(c.get("score") or 0)}
                      for c in raw if isinstance(c, dict)]
        candidates = [c for c in candidates
                      if valid_component(c["root_component"]) and c["category"] in CATEGORIES][:5]
        return {"candidates": candidates, "transcript": [llm_entry("hypothesize", reply, model)]}

    async def investigate(state: InvestigationState) -> dict:
        if "chat" not in session:
            session["chat"] = Chat(system, tools=tools, model=model)
            ranked = "\n".join(f"{i + 1}. {c['root_component']} / {c['category']} ({c['score']:.2f}): {c['rationale']}"
                               for i, c in enumerate(state.get("candidates", []))) or "(none)"
            turn = (f"Alert:\n{alert_text(state['alert'])}\n\nTriage:\n{state['triage']}\n\n"
                    f"Candidates:\n{ranked}\n\n"
                    f"Investigate the top candidates with the tools. You have {toolbox.budget - toolbox.used} "
                    "tool calls left. When you can name the root component with evidence, or have ruled the "
                    "candidates out, stop calling tools and reply with a short summary of what you found.")
        else:
            turn = session.pop("pending")
        reply = await ask(session["chat"], turn)
        entries = [llm_entry("investigate", reply, model)]
        if not reply.tool_calls:
            return {"done": True, "notes": reply.text, "transcript": entries}

        results, new = [], []
        for call in reply.tool_calls:
            result, entry = await toolbox.call_tool(call.name, call.input)
            entries.append({**entry, "node": "investigate"})
            new += ev.from_result(call.name, result)
            results.append((call, {**result, "_calls_left": toolbox.budget - toolbox.used}))
        session["pending"] = results
        return {"evidence": ev.merge(state.get("evidence", []) + [], new), "transcript": entries,
                "tool_calls_used": toolbox.used, "notes": reply.text or state.get("notes", "")}

    def after_investigate(state: InvestigationState) -> str:
        return "conclude" if state.get("done") or toolbox.exhausted else "investigate"

    async def conclude(state: InvestigationState) -> dict:
        chat = Chat(system, json_mode=True, model=model)
        pool = state.get("evidence", [])
        fields = ", ".join(Diagnosis.model_fields)
        reply = await ask(chat, 
            f"Alert:\n{alert_text(state['alert'])}\n\nTriage:\n{state['triage']}\n\n"
            f"Investigator notes:\n{state.get('notes') or '(none)'}\n\n"
            f"Evidence collected (cite only these refs, exactly as written):\n{pool_text(pool)}\n\n"
            f"Write the diagnosis as one JSON object with the fields {fields}. "
            "evidence: the items that support the diagnosis, each {type, ref, claim} with a ref copied from the "
            "list above. cause_ref: the ref of the change that caused the failure, or null if no change did. "
            "ruled_out: each recent change you considered and dismissed, {change, reason}. confidence: 0-1. "
            "Set abstained to true only if the evidence cannot separate the candidates.")
        entries = [llm_entry("conclude", reply, model)]
        error = None
        for attempt in (1, 2):
            try:
                diagnosis = Diagnosis.model_validate(parse_json(reply.text)).model_dump()
                return {"diagnosis": diagnosis, "transcript": entries}
            except (ValueError, ValidationError) as e:
                error = str(e)[:1500]
                if attempt == 2:
                    break
                reply = await ask(chat, f"That JSON failed validation:\n{error}\nReturn the corrected JSON object only.")
                entries.append(llm_entry("conclude-retry", reply, model))
        return {"diagnosis": abstain(f"no valid diagnosis after one retry: {error}"), "transcript": entries}

    g = StateGraph(InvestigationState)
    for name, fn in (("triage", triage), ("hypothesize", hypothesize), ("investigate", investigate),
                     ("conclude", conclude)):
        g.add_node(name, fn)
    g.add_edge(START, "triage")
    g.add_edge("triage", "hypothesize")
    g.add_edge("hypothesize", "investigate")
    g.add_conditional_edges("investigate", after_investigate, {"investigate": "investigate", "conclude": "conclude"})
    g.add_edge("conclude", END)
    return g.compile()


async def investigate_alert(alert: dict, env: dict[str, str] | None = None, model: str | None = None,
                            budget: int | None = None, progress: dict | None = None,
                            errlog: Path | None = None) -> dict:
    """Run one investigation against a tool server started with `env` (e.g. the snapshot backend).
    `progress`, if given, always holds the latest state, so a crash or timeout keeps the transcript."""
    kwargs = {"budget": budget} if budget else {}
    state: dict = {}
    async with Toolbox.start(env, errlog=errlog, **kwargs) as toolbox:
        graph = build_graph(toolbox, await toolbox.definitions(), model)
        async for state in graph.astream({"alert": alert, "candidates": [], "evidence": [], "transcript": [],
                                          "tool_calls_used": 0, "replays_used": 0, "diagnosis": None},
                                         config={"recursion_limit": RECURSION_LIMIT}, stream_mode="values"):
            if progress is not None:
                progress.clear()
                progress.update(state)
    return state
