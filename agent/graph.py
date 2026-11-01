"""The investigator graph.

    triage ──► hypothesize ──► investigate ⟲ ──► draft ──► verify ─┬─► conclude   (gate passed)
                   ▲                                              ├─► hypothesize (failed, budget left)
                   └──────────────────────────────────────────────┘
                                                                  └─► abstain     (failed, budget gone)

investigate is one model turn per visit, until the model stops calling tools or the budget is
gone. draft is the model writing the diagnosis; verify is code (agent/verify.py) that decides
whether the evidence is enough. conclude and abstain are code too: the model never decides
that its own diagnosis is good enough.

Budget (F7): Toolbox.call_tool refuses past the tool and replay budgets, the router stops after
MAX_ROUNDS drafts or when the tools run out, and LangGraph's recursion_limit is the backstop.
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
from agent.verify import gate

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "investigator.md"
RECURSION_LIMIT = 90
MAX_POOL_IN_PROMPT = 120
MAX_ROUNDS = 3  # drafts the gate may judge before the investigator has to abstain
# What the model fills in; the abstain fields are the abstain node's job.
DRAFT_FIELDS = ("root_component", "category", "cause_ref", "confidence", "summary", "evidence", "ruled_out",
                "suggested_fix", "fix_patch", "abstained")
DRAFT_INSTRUCTIONS = (
    "Write the diagnosis as one JSON object with the fields " + ", ".join(DRAFT_FIELDS) + ". "
    "evidence: the items that support the diagnosis, each {type, ref, claim} with a ref copied from the list "
    "above, plus where it applies: attribute and value (a span attribute with its full name exactly as it appears "
    'before the "=" in that item, e.g. "tool.result_count" with 0, or "config.top_k" on the root span; for a '
    'replay, "matches_correction" and the value it showed), or quote (only for chunk and log items: a few words '
    "copied exactly from the chunk or log text shown after its \"retrieved ...:\" or level and component; never "
    "quote a summary, a count or a diff line). "
    "cause_ref: the ref of the change that caused the failure, or null if no change did. "
    "ruled_out: each recent change you considered and dismissed, {change, reason, ref}, where ref is that "
    "change's ref or a replay that backs the reason. suggested_fix: one sentence. fix_patch: the same fix as "
    "one replay patch object, or null. confidence: 0-1. Set abstained to true only if the evidence cannot "
    "separate the candidates.\n"
    'fix_patch shapes: {"kind": "use_doc_version", "doc_id": "...", "version": "..."}, '
    '{"kind": "restore_doc", "doc_id": "..."}, {"kind": "set_config", "key": "<config key>", "value": "..."}, '
    '{"kind": "set_prompt_version", "version": "..."}, {"kind": "rerun_tool", "tool": "..."}, '
    '{"kind": "bypass_cache", "tool": "check_inventory"}. Only versions and values the history shows are '
    "accepted.\n\n"
    "The diagnosis is checked in code and accepted only if the evidence relevant to its root_component covers "
    "at least 2 types (trace, chunk, log, change, metric), or 1 type plus a replay that patched that component "
    "and fixed the answer (proves_fix true). Evidence about other components does not count, and every ref "
    "must be one a tool returned.")


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


# metric/before/after/failing_samples are only on a quality_drop alert (Day 8); a
# flagged_conversation alert simply does not have them.
ALERT_KEYS = ("type", "fired_at", "window", "conversation_id", "trace_id", "question", "answer",
              "reviewer_correction", "metric", "before", "after", "failing_samples")
MAX_SAMPLES_IN_PROMPT = 5


def alert_text(alert: dict) -> str:
    view = {k: alert[k] for k in ALERT_KEYS if k in alert}
    samples = view.get("failing_samples")
    if samples:
        # The reviewer sees a handful of examples, not every failing conversation.
        view["failing_samples"] = [{k: v for k, v in s.items() if k != "answer_check"}
                                   for s in samples[:MAX_SAMPLES_IN_PROMPT]]
    return json.dumps(view, indent=1)


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
                detail += f", retrieval.chunk_ids={s['attrs'].get('retrieval.chunk_ids')}"
            lines.append(f"  - {s['name']} [{s['status']}, {s['duration_ms']}ms] {detail}  ref={s['ref']}")
    if "error" in changes:
        lines.append(f"recent_changes failed: {changes['error']}")
    else:
        lines.append(f"Changes in the 7 days before the alert: {changes['total']} "
                     f"(showing {changes['returned']}, newest first):")
        lines += [f"  - {c['created_at']} {c['kind']} {c['target']} by {c['author']}: {c['description']}  ref={c['ref']}"
                  for c in changes["changes"]]
    return "\n".join(lines)


def rejection(state: InvestigationState) -> str:
    g = state.get("gate")
    if not g or g["passed"]:
        return ""
    return f"The diagnosis naming {g.get('hypothesis')} was rejected by the evidence check: {g['reason']}."


async def ask(chat: Chat, turn) -> Reply:
    return await asyncio.to_thread(chat.send, turn)


def build_graph(toolbox: Toolbox, tools: list[dict], model: str | None = None):
    model = model or model_name()
    system = system_prompt()
    session: dict = {}  # the investigate conversation, carried across visits of that node

    def left() -> str:
        return (f"You have {toolbox.budget - toolbox.used} tool calls left, of which at most "
                f"{toolbox.replay_budget - toolbox.replays} replays.")

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
        feedback = ""
        if rejection(state):
            feedback = (f"\n\n{rejection(state)}\nEvidence collected so far:\n{pool_text(state.get('evidence', []))}\n"
                        "Re-rank the candidates; keep that component only if more evidence could still support it.")
        reply = await ask(chat,
            f"Alert:\n{alert_text(state['alert'])}\n\nTriage:\n{state['triage']}{feedback}\n\n"
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
        return {"candidates": candidates or state.get("candidates", []),
                "transcript": [llm_entry("hypothesize", reply, model)]}

    async def investigate(state: InvestigationState) -> dict:
        ranked = "\n".join(f"{i + 1}. {c['root_component']} / {c['category']} ({c['score']:.2f}): {c['rationale']}"
                           for i, c in enumerate(state.get("candidates", []))) or "(none)"
        if "chat" not in session:
            session["chat"] = Chat(system, tools=tools, model=model)
            turn = (f"Alert:\n{alert_text(state['alert'])}\n\nTriage:\n{state['triage']}\n\n"
                    f"Candidates:\n{ranked}\n\n"
                    f"Investigate the top candidates with the tools. {left()} When you can name the root "
                    "component with evidence, or have ruled the candidates out, stop calling tools and reply "
                    "with a short summary of what you found.")
        elif "pending" in session:
            turn = session.pop("pending")
        else:  # back from a rejected draft
            turn = (f"{rejection(state)}\nRe-ranked candidates:\n{ranked}\n\n"
                    f"Gather the missing evidence, or test another candidate. {left()} Stop calling tools and "
                    "summarize when you are done.")
        reply = await ask(session["chat"], turn)
        entries = [llm_entry("investigate", reply, model)]
        if not reply.tool_calls:
            return {"done": True, "notes": reply.text or state.get("notes", ""), "transcript": entries}

        results, new = [], []
        for call in reply.tool_calls:
            result, entry = await toolbox.call_tool(call.name, call.input)
            entries.append({**entry, "node": "investigate"})
            new += ev.from_result(call.name, result)
            results.append((call, {**result, "_calls_left": toolbox.budget - toolbox.used,
                                   "_replays_left": toolbox.replay_budget - toolbox.replays}))
        session["pending"] = results
        return {"evidence": ev.merge(state.get("evidence", []), new), "transcript": entries,
                "tool_calls_used": toolbox.used, "replays_used": toolbox.replays,
                "notes": reply.text or state.get("notes", "")}

    def after_investigate(state: InvestigationState) -> str:
        return "draft" if state.get("done") or toolbox.exhausted else "investigate"

    async def draft(state: InvestigationState) -> dict:
        chat = Chat(system, json_mode=True, model=model)
        rejected = f"\n\n{rejection(state)}" if rejection(state) else ""
        reply = await ask(chat,
            f"Alert:\n{alert_text(state['alert'])}\n\nTriage:\n{state['triage']}\n\n"
            f"Investigator notes:\n{state.get('notes') or '(none)'}{rejected}\n\n"
            f"Evidence collected (cite only these refs, exactly as written):\n{pool_text(state.get('evidence', []))}\n\n"
            + DRAFT_INSTRUCTIONS)
        entries = [llm_entry("draft", reply, model)]
        error = None
        for attempt in (1, 2):
            try:
                diagnosis = Diagnosis.model_validate(parse_json(reply.text)).model_dump()
                return {"draft": diagnosis, "transcript": entries}
            except (ValueError, ValidationError) as e:
                error = str(e)[:1500]
                if attempt == 2:
                    break
                reply = await ask(chat, f"That JSON failed validation:\n{error}\nReturn the corrected JSON object only.")
                entries.append(llm_entry("draft-retry", reply, model))
        return {"draft": {"abstained": True, "invalid": f"no valid diagnosis after one retry: {error}"},
                "transcript": entries}

    def verify(state: InvestigationState) -> dict:
        d = state.get("draft") or {}
        if d.get("invalid"):
            result = {"passed": False, "reason": d["invalid"], "hypothesis": None}
        else:
            result = gate(d, state.get("evidence", [])).as_dict()
        return {"gate": result, "rounds": state.get("rounds", 0) + 1, "done": False,
                "transcript": [{"kind": "gate", "node": "verify", **result}]}

    def after_verify(state: InvestigationState) -> str:
        if state["gate"]["passed"]:
            return "conclude"
        if state["rounds"] < MAX_ROUNDS and not toolbox.exhausted:
            return "hypothesize"
        return "abstain"

    def conclude(state: InvestigationState) -> dict:
        return {"diagnosis": state["draft"], "transcript": [{"kind": "end", "node": "conclude"}]}

    def abstain_node(state: InvestigationState) -> dict:
        d = state.get("draft") or {}
        tops = [{"root_component": d["root_component"], "category": d.get("category")}] if d.get("root_component") else []
        tops += [c for c in state.get("candidates", []) if c["root_component"] not in {t["root_component"] for t in tops}]
        reason = state["gate"]["reason"]
        diagnosis = abstain(f"evidence not sufficient after {state['rounds']} draft(s): {reason}", tops, reason)
        return {"diagnosis": diagnosis, "transcript": [{"kind": "end", "node": "abstain"}]}

    g = StateGraph(InvestigationState)
    for name, fn in (("triage", triage), ("hypothesize", hypothesize), ("investigate", investigate),
                     ("draft", draft), ("verify", verify), ("conclude", conclude), ("abstain", abstain_node)):
        g.add_node(name, fn)
    g.add_edge(START, "triage")
    g.add_edge("triage", "hypothesize")
    g.add_edge("hypothesize", "investigate")
    g.add_conditional_edges("investigate", after_investigate, {"investigate": "investigate", "draft": "draft"})
    g.add_edge("draft", "verify")
    g.add_conditional_edges("verify", after_verify,
                            {"conclude": "conclude", "hypothesize": "hypothesize", "abstain": "abstain"})
    g.add_edge("conclude", END)
    g.add_edge("abstain", END)
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
                                          "tool_calls_used": 0, "replays_used": 0, "rounds": 0,
                                          "draft": None, "gate": None, "diagnosis": None},
                                         config={"recursion_limit": RECURSION_LIMIT}, stream_mode="values"):
            if progress is not None:
                progress.clear()
                progress.update(state)
    return state
