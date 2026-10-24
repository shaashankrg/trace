"""The ten investigator tools as plain functions: `handler(backend, **args) -> dict`.

server.py registers each one with FastMCP. Keeping them free of MCP lets the tests call them
directly against any backend.

Every factual item carries a `ref` (see refs.py). Lists carry a `total`. Text is clipped and
the response says so with `"truncated": true`.
"""
import difflib
from datetime import datetime

from tools import refs, replay, trace_model
from tools.backend import Backend, iso
from tools.output import ToolError, clip, tool

TEXT_LIMIT = 300      # chunk text, log messages
DESC_LIMIT = 200      # change descriptions
ATTR_LIMIT = 200      # one span attribute value
DIFF_LINES = 80       # prompt diff
DOC_DIFF_LINES = 14   # document version diff
MAX_LOGS = 50
POINTS_PER_SERIES = 10
MAX_SERIES = 6


def _time(value: str | None, name: str) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return refs.parse_ts(value)
    except refs.RefError as e:
        raise ToolError(f"{name} is not an ISO 8601 timestamp: {value!r}",
                        "use e.g. 2026-10-14T03:00:00Z") from e


def _window(start: str | None, end: str | None) -> tuple[datetime | None, datetime | None]:
    s, e = _time(start, "start"), _time(end, "end")
    if s and e and s > e:
        raise ToolError("start is after end", "swap them")
    return s, e


def _attr(value):
    if isinstance(value, str):
        return clip(value, ATTR_LIMIT)
    if isinstance(value, list) and len(str(value)) > ATTR_LIMIT:
        return clip(str(value), ATTR_LIMIT)
    return value


# 1 ------------------------------------------------------------------------------------------

@tool("spans")
def get_trace(backend: Backend, conversation_id: str) -> dict:
    """Span tree of the newest trace for a conversation (a trace id also works)."""
    trace_id = backend.trace_id_for(conversation_id)
    flat = trace_model.spans(backend.trace(trace_id), trace_id)
    if not flat:
        raise ToolError(f"trace {trace_id} has no spans", "it may not have been exported yet; retry shortly")
    t0 = flat[0]["start_us"]
    out = []
    for s in trace_model.ordered_tree(flat):
        item = {
            "ref": s["ref"], "name": s["name"],
            "parent_ref": next((p["ref"] for p in flat if p["span_id"] == s["parent_id"]), None),
            "offset_ms": round((s["start_us"] - t0) / 1000, 1),
            "duration_ms": s["duration_ms"], "status": s["status"],
            "attrs": {k: _attr(v) for k, v in trace_model.key_attrs(s).items()},
        }
        if s["status"] == "error" and (msg := trace_model.exception_message(s)):
            item["error"] = clip(msg, 200)
        out.append(item)
    return {"conversation_id": conversation_id, "trace_id": trace_id, "total_spans": len(out), "spans": out}


# 2 ------------------------------------------------------------------------------------------

@tool("chunks")
def get_retrieved_chunks(backend: Backend, span_ref: str) -> dict:
    """The chunks a `retrieve` span returned, in rank order, with score and text."""
    try:
        parsed = refs.parse_ref(span_ref)
    except refs.RefError as e:
        raise ToolError(str(e), "pass a span ref from get_trace, e.g. span:<trace_id>/<span_id>") from e
    if parsed.kind != "span":
        raise ToolError(f"{span_ref!r} is a {parsed.kind} ref, not a span ref",
                        "pass the ref of the `retrieve` span from get_trace")
    trace_id, span_id = parsed.parts
    span = next((s for s in trace_model.spans(backend.trace(trace_id), trace_id) if s["span_id"] == span_id), None)
    if span is None:
        raise ToolError(f"no span {span_id} in trace {trace_id}", "take the ref from get_trace")
    attrs = span["attrs"]
    if "kestrel.retrieval.chunk_ids" not in attrs:
        raise ToolError(f"span {span['name']!r} is not a retrieval span",
                        "pass the ref of the span named `retrieve`")
    ids = attrs["kestrel.retrieval.chunk_ids"] or []
    scores = attrs.get("kestrel.retrieval.scores") or []
    found = {c["chunk_id"]: c for c in backend.chunks(ids)}
    chunks, clipped = [], False
    for i, cid in enumerate(ids):
        row = found.get(cid)
        if row is None:
            chunks.append({"ref": refs.chunk_ref(cid), "missing": True})
            continue
        text = clip(row["text"], TEXT_LIMIT)
        clipped |= text != row["text"]
        chunks.append({"ref": refs.chunk_ref(cid), "doc_id": row["doc_id"], "version": row["version"],
                       "score": scores[i] if i < len(scores) else None, "text": text})
    out = {"span_ref": refs.format_ref(parsed), "top_k": attrs.get("kestrel.retrieval.top_k"),
           "threshold": attrs.get("kestrel.retrieval.threshold"), "total": len(ids), "chunks": chunks}
    if clipped:
        out["truncated"] = True
    return out


# 3 ------------------------------------------------------------------------------------------

def _doc_diff(old: str, new: str) -> str:
    """Word-level changes with a few words of context: `...within [30 -> 14] days of...`.
    A line diff would hide a changed number inside a long paragraph."""
    a, b = old.split(), new.split()
    ops = [op for op in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if op[0] != "equal"]
    if not ops:
        return "no text change"
    ctx = 4
    shown = []
    for tag, i1, i2, j1, j2 in ops[:DOC_DIFF_LINES]:
        before, after = " ".join(a[max(i1 - ctx, 0):i1]), " ".join(a[i2:i2 + ctx])
        gone, added = clip(" ".join(a[i1:i2]), 120), clip(" ".join(b[j1:j2]), 120)
        change = {"delete": f"[-{gone}]", "insert": f"[+{added}]"}.get(tag, f"[{gone} -> {added}]")
        shown.append(f"...{before} {change} {after}...")
    if len(ops) > DOC_DIFF_LINES:
        shown.append(f"... {len(ops) - DOC_DIFF_LINES} more changes")
    return "\n".join(shown)


@tool("versions")
def document_history(backend: Backend, doc_id: str) -> dict:
    """Every version of a document, oldest first, with how many of its chunks are indexed.
    There is deliberately no `current` or `correct` field: working that out is the investigator's job."""
    rows = backend.documents(doc_id)
    if not rows:
        raise ToolError(f"no document {doc_id!r}", "doc ids look like 'refund-policy'; "
                        "get_retrieved_chunks shows the doc_id of anything the assistant retrieved")
    versions, prev = [], None
    for row in rows:
        item = {"ref": refs.doc_ref(row["doc_id"], row["version"]), "version": row["version"],
                "title": row.get("title"), "published_at": iso(row["published_at"]),
                "chunk_count": row["chunk_count"], "indexed_chunk_count": row["indexed_chunks"],
                "diff_vs_previous": "first version" if prev is None else _doc_diff(prev, row["body"])}
        versions.append(item)
        prev = row["body"]
    return {"doc_id": doc_id, "total_versions": len(versions), "versions": versions}


# 4 ------------------------------------------------------------------------------------------

@tool("logs")
def search_logs(backend: Backend, component: str = "", query: str = "", start: str = "", end: str = "",
                limit: int = 20) -> dict:
    """Log lines, oldest first. `component` matches exactly or as a prefix before a dot
    (`tool` finds `tool.lookup_order`); `query` needs every word in the message."""
    s, e = _window(start, end)
    limit = max(1, min(int(limit), MAX_LOGS))
    rows, total = backend.logs(component or None, query or None, s, e, limit)
    logs, clipped = [], False
    for r in rows:
        msg = clip(r.get("message", ""), TEXT_LIMIT)
        clipped |= msg != r.get("message", "")
        logs.append({"ref": refs.log_ref(r["log_id"]), "timestamp": r["timestamp"], "level": r["level"],
                     "component": r["component"], "message": msg})
    out = {"total_matches": total, "returned": len(logs), "logs": logs}
    if clipped or total > len(logs):
        out["truncated"] = True
    return out


# 5 ------------------------------------------------------------------------------------------

@tool("changes")
def recent_changes(backend: Backend, start: str = "", end: str = "", limit: int = 30) -> dict:
    """Changes to prompts, documents and config in the window, newest first. Everything in the
    change history is listed, whether or not it matters to the incident."""
    s, e = _window(start, end)
    limit = max(1, min(int(limit), 100))
    rows = backend.changes(s, e)
    changes = [{"ref": refs.change_ref(r["change_id"]), "kind": r["kind"], "target": r["target"],
                "description": clip(r["description"], DESC_LIMIT), "author": r["author"],
                "created_at": iso(r["created_at"])} for r in rows[:limit]]
    out = {"total": len(rows), "returned": len(changes), "changes": changes}
    if len(rows) > len(changes) or any(len(r["description"] or "") > DESC_LIMIT for r in rows[:limit]):
        out["truncated"] = True
    return out


# 6 ------------------------------------------------------------------------------------------

@tool()
def diff_prompt_versions(backend: Backend, a: str, b: str) -> dict:
    """Unified line diff between two prompt versions, capped at 80 lines."""
    versions = {v["version"]: v for v in backend.prompt_versions()}
    for name in (a, b):
        if name not in versions:
            raise ToolError(f"no prompt version {name!r}", f"known versions: {', '.join(sorted(versions)) or 'none'}")
    lines = list(difflib.unified_diff(versions[a]["template"].splitlines(), versions[b]["template"].splitlines(),
                                      fromfile=a, tofile=b, lineterm="", n=1))
    meta = lambda v: {"ref": refs.prompt_ref(v["version"]), "author": v.get("author"),  # noqa: E731
                      "created_at": iso(v.get("created_at")), "note": v.get("note")}
    out = {"a": meta(versions[a]), "b": meta(versions[b]), "identical": not lines,
           "diff_lines": len(lines), "diff": "\n".join(clip(ln, 400) for ln in lines[:DIFF_LINES])}
    if len(lines) > DIFF_LINES:
        out["truncated"] = True
    return out


# 7 ------------------------------------------------------------------------------------------

def _pick(values: list, n: int) -> list:
    if len(values) <= n:
        return values
    idx = sorted({round(i * (len(values) - 1) / (n - 1)) for i in range(n)})
    return [values[i] for i in idx]


@tool("series")
def query_metrics(backend: Backend, metric: str, start: str = "", end: str = "") -> dict:
    """min/max/mean per series and up to 10 sample points, each with a ref."""
    s, e = _window(start, end)
    series = backend.metric(metric, s, e)
    if not series:
        known = backend.metric_names()
        raise ToolError(f"no samples for {metric!r} in that window",
                        f"known metrics: {', '.join(known) or 'none'}; widen the window or check the name")
    out_series = []
    for ser in series[:MAX_SERIES]:
        vals = [v for _, v in ser["values"]]
        out_series.append({
            "labels": ser["labels"], "count": len(vals),
            "min": round(min(vals), 4), "max": round(max(vals), 4), "mean": round(sum(vals) / len(vals), 4),
            "points": [{"ref": refs.metric_ref(metric, ts), "ts": refs.format_ts(ts), "value": round(v, 4)}
                       for ts, v in _pick(ser["values"], POINTS_PER_SERIES)],
        })
    out = {"metric": metric, "total_series": len(series), "series": out_series}
    if len(series) > MAX_SERIES:
        out["truncated"] = True
    return out


# stubs --------------------------------------------------------------------------------------

def _not_implemented(day: str) -> dict:
    return {"error": "not_implemented", "hint": f"this tool is built on {day}; use the other tools"}


@tool()
def replay_conversation(backend: Backend, conversation_id: str, patch: dict | None = None) -> dict:
    """Re-run the flagged conversation with exactly one patch applied and check the new answer
    against the reviewer's correction. Recorded cases only: the replay runs on the snapshot."""
    case_dir = getattr(backend, "dir", None)
    if case_dir is None:
        raise ToolError("replay needs a recorded case", "start the server with TRACEWISE_BACKEND=snapshot:<case_id>")
    if replay.IN_SERVER and not replay.PRELOADED:
        raise ToolError("replay is not enabled on this server",
                        "start it with TRACEWISE_REPLAY=1, which loads the assistant before serving")
    snap = replay.Snapshot(case_dir)
    if conversation_id not in (snap.alert.get("conversation_id"), snap.alert.get("trace_id")):
        raise ToolError(f"no recorded conversation {conversation_id!r}",
                        f"this case's conversation is {snap.alert.get('conversation_id')}")
    try:
        record = replay.run_replay(snap, patch, replay.log_path(snap.case_id))
    except replay.PatchError as e:
        raise ToolError(f"patch rejected: {e}", e.hint) from e
    return replay.summary(record)


@tool()
def search_playbooks(backend: Backend, query: str, k: int = 3) -> dict:
    """Search the runbook corpus. Built on Day 10."""
    return _not_implemented("Day 10")


@tool()
def similar_incidents(backend: Backend, summary: str, k: int = 3) -> dict:
    """Find past incidents similar to a summary. Built on Day 10."""
    return _not_implemented("Day 10")


HANDLERS = {fn.__name__: fn for fn in (
    get_trace, get_retrieved_chunks, document_history, search_logs, recent_changes,
    diff_prompt_versions, query_metrics, replay_conversation, search_playbooks, similar_incidents)}
