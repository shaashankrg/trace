"""Jaeger trace JSON -> a flat, ordered span list the tools can reason about."""
import json

from tools.refs import span_ref

# Span attributes worth showing. Everything else (http.*, net.*, otel.scope...) is noise.
_KEEP_PREFIXES = ("kestrel.",)
_KEEP_KEYS = {"gen_ai.tool.name", "gen_ai.request.model", "gen_ai.response.model",
              "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.response.finish_reasons"}
_DROP_KEYS = {"kestrel.conversation_id"}  # already at the top of get_trace
_CONFIG = "kestrel.config."


def _value(raw):
    """Jaeger flattens list attributes to JSON strings; turn them back into lists."""
    if isinstance(raw, str) and raw[:1] == "[":
        try:
            return json.loads(raw)
        except ValueError:
            pass
    return raw


def tags(span: dict) -> dict:
    return {t["key"]: _value(t["value"]) for t in span.get("tags", [])}


def is_error(attrs: dict) -> bool:
    return attrs.get("error") in (True, "true") or attrs.get("otel.status_code") == "ERROR"


def spans(raw: dict, trace_id: str | None = None) -> list[dict]:
    """Spans in start order, each with: span_id, parent_id, name, start_us, duration_ms, status,
    attrs (all tags), ref."""
    out = []
    for t in raw.get("data", []):
        tid = t.get("traceID", trace_id)
        for s in t.get("spans", []):
            parent = next((r["spanID"] for r in s.get("references", []) if r.get("refType") == "CHILD_OF"), None)
            attrs = tags(s)
            out.append({
                "trace_id": tid, "span_id": s["spanID"], "parent_id": parent,
                "name": s["operationName"], "start_us": s["startTime"],
                "duration_ms": round(s["duration"] / 1000, 1),
                "status": "error" if is_error(attrs) else "ok",
                "attrs": attrs, "ref": span_ref(tid, s["spanID"]),
                "events": s.get("logs", []),
            })
    return sorted(out, key=lambda s: (s["start_us"], s["span_id"]))


def key_attrs(span: dict) -> dict:
    """The attributes the investigator needs, with the config prefix shortened."""
    out = {}
    for k, v in span["attrs"].items():
        if k in _DROP_KEYS:
            continue
        if k.startswith(_CONFIG):
            out["config." + k[len(_CONFIG):]] = v
        elif k in _KEEP_KEYS or k.startswith(_KEEP_PREFIXES):
            out[k.removeprefix("kestrel.")] = v
    return out


def exception_message(span: dict) -> str | None:
    for event in span.get("events", []):
        fields = {f["key"]: f["value"] for f in event.get("fields", [])}
        if fields.get("exception.message") or fields.get("event") == "exception":
            return str(fields.get("exception.message") or fields.get("exception.type") or "exception")
    return None


def ordered_tree(all_spans: list[dict]) -> list[dict]:
    """Depth-first order, parents before children, siblings by start time; adds `depth`."""
    ids = {s["span_id"] for s in all_spans}
    children: dict[str | None, list[dict]] = {}
    for s in all_spans:
        key = s["parent_id"] if s["parent_id"] in ids else None
        children.setdefault(key, []).append(s)
    out: list[dict] = []

    def walk(parent, depth):
        for s in children.get(parent, []):
            out.append({**s, "depth": depth})
            walk(s["span_id"], depth + 1)

    walk(None, 0)
    return out
