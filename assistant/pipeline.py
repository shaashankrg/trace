"""One chat turn: config -> guardrail -> retrieve -> Claude with tools."""
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import psycopg
from opentelemetry import trace

from assistant import guardrail, llm, tools
from assistant.config import Config, load_config
from assistant.logs import conversation_id_var
from assistant.prompts import load_prompt
from assistant.retrieve import Chunk, retrieve

MAX_LLM_TURNS = 5

log = logging.getLogger("kestrel.chat")


@dataclass
class ChatResult:
    answer: str
    chunk_ids: list[str] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)


def render_user_message(question: str, chunks: list[Chunk]) -> str:
    docs = "\n\n".join(
        f'<document id="{c.chunk_id}">\n{c.text}\n</document>' for c in chunks
    ) or "(no documents retrieved)"
    return f"<documents>\n{docs}\n</documents>\n\nQuestion: {question}"


def run(conn: psycopg.Connection, conversation_id: str, question: str) -> ChatResult:
    conversation_id_var.set(conversation_id)
    root = trace.get_current_span()
    root.set_attribute("kestrel.conversation_id", conversation_id)

    cfg = load_config(conn)
    root.set_attributes(cfg.span_attributes())
    return answer(
        cfg, question,
        lambda: retrieve(conn, question, cfg.top_k, cfg.similarity_threshold, cfg.embedding_model,
                         conversation_id),
        lambda: load_prompt(conn, cfg.prompt_version),
        lambda name, tool_input: tools.execute_tool(conn, name, tool_input, cfg.tool_timeout_ms,
                                                    conversation_id),
    )


def answer(cfg: Config, question: str, get_chunks: Callable[[], list[Chunk]],
           get_system: Callable[[], str], run_tool: Callable[[str, dict], list[dict]]) -> ChatResult:
    """guardrail -> retrieve -> model with tools. Where chunks, the prompt and tool results come
    from is the caller's choice: the live database here, a recorded snapshot in tools/replay.py."""
    decision = guardrail.check(question, cfg.guardrail_blocked_topics)
    if not decision.allowed:
        log.info("guardrail blocked question (rule=%s)", decision.matched_rule,
                 extra={"component": "guardrail"})
        return ChatResult(answer=guardrail.BLOCKED_ANSWER)

    chunks = get_chunks()
    system = get_system()

    messages: list = [{"role": "user", "content": render_user_message(question, chunks)}]
    tools_called: list[str] = []
    for turn in range(MAX_LLM_TURNS):
        response = llm.chat(cfg, system, messages, tools.TOOL_DEFINITIONS, turn)
        if response.stop_reason != "tool_use":
            break
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            tools_called.append(block.name)
            try:
                rows = run_tool(block.name, block.input)
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": tools.to_json(rows)}
                )
            except Exception as e:
                log.error("tool %s failed: %s", block.name, e, extra={"component": f"tool.{block.name}"})
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id,
                     "content": f"Tool error: {e}", "is_error": True}
                )
        messages.append({"role": "user", "content": results})

    text = "".join(b.text for b in response.content if b.type == "text")
    log.info(
        "chat answered (chunks=%d, tools=%s, stop_reason=%s)",
        len(chunks), ",".join(tools_called) or "none", response.stop_reason,
        extra={"component": "chat"},
    )
    return ChatResult(answer=text, chunk_ids=[c.chunk_id for c in chunks], tools_called=tools_called)
