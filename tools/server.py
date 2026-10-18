"""MCP server for the investigator's tools, over stdio.

  python -m tools.server                              # live stack
  TRACEWISE_BACKEND=snapshot:case-007 python -m tools.server
  npx @modelcontextprotocol/inspector .venv/Scripts/python.exe -m tools.server

The backend is chosen once, at start-up, from TRACEWISE_BACKEND. The tools never learn who is
calling them: the same server answers the Inspector, a script and the Day 5 agent.
"""
import functools

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

from tools import handlers  # noqa: E402
from tools.backend import get_backend  # noqa: E402

mcp = FastMCP("tracewise-tools")


@functools.lru_cache(maxsize=1)
def backend():
    return get_backend()


def register(fn):
    """Expose `fn` as a tool, with its docstring on one line as the description the model reads."""
    return mcp.tool(description=" ".join(fn.__doc__.split()))(fn)


@register
def get_trace(conversation_id: str) -> dict:
    """Span tree for a conversation's newest trace: each span's ref, name, parent, duration_ms,
    status and key attributes (prompt version, model, top_k, threshold, result_count, chunk_ids,
    tool latency). Start here: it shows which step looks wrong and gives refs for the other tools."""
    return handlers.get_trace(backend(), conversation_id)


@register
def get_retrieved_chunks(span_ref: str) -> dict:
    """The chunks a `retrieve` span returned, in rank order: ref, doc_id, version, score and text
    (cut at 300 characters). Pass the span ref of the `retrieve` span from get_trace."""
    return handlers.get_retrieved_chunks(backend(), span_ref)


@register
def document_history(doc_id: str) -> dict:
    """Every version of a document, oldest first: version, published_at, how many of its chunks
    are indexed, and a short diff against the previous version. It does not say which version is
    current or correct."""
    return handlers.document_history(backend(), doc_id)


@register
def search_logs(component: str = "", query: str = "", start: str = "", end: str = "", limit: int = 20) -> dict:
    """Search application logs, oldest first. component: exact name or a prefix before a dot
    (`tool` matches `tool.lookup_order`); query: words that must all appear in the message;
    start/end: ISO 8601. Returns ref, timestamp, level, component, message and `total_matches`."""
    return handlers.search_logs(backend(), component, query, start, end, limit)


@register
def recent_changes(start: str = "", end: str = "", limit: int = 30) -> dict:
    """Changes to prompts, documents and config between start and end (ISO 8601), newest first:
    ref, kind, target, description, author, created_at. The list is everything that changed, not
    only what caused the incident."""
    return handlers.recent_changes(backend(), start, end, limit)


@register
def diff_prompt_versions(a: str, b: str) -> dict:
    """Unified line diff between two prompt versions (e.g. a="v1", b="v2"), at most 80 lines."""
    return handlers.diff_prompt_versions(backend(), a, b)


@register
def query_metrics(metric: str, start: str = "", end: str = "") -> dict:
    """A metric over a window (ISO 8601): min, max, mean and up to 10 sample points per series,
    each with a ref. Example metric: kestrel_canary_accuracy (labels: suite, category)."""
    return handlers.query_metrics(backend(), metric, start, end)


@register
def replay_conversation(conversation_id: str, patch: dict) -> dict:
    """Replay a conversation with a patch applied and return the new answer. Not implemented yet."""
    return handlers.replay_conversation(backend(), conversation_id, patch)


@register
def search_playbooks(query: str, k: int = 3) -> dict:
    """Search the runbook playbooks. Not implemented yet."""
    return handlers.search_playbooks(backend(), query, k)


@register
def similar_incidents(summary: str, k: int = 3) -> dict:
    """Find past incidents similar to a summary. Not implemented yet."""
    return handlers.similar_incidents(backend(), summary, k)


if __name__ == "__main__":
    mcp.run(transport="stdio")
