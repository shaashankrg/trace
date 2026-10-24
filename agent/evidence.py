"""Tool results -> typed Evidence items: a ref the verifier can check and a one-line claim.

Done in code, not by the model, so every claim says only what the tool returned and every ref is
copied, never typed. A document version or prompt version counts as `change` evidence: both are
published artifacts in the change history.
"""
from agent.state import Evidence

CLAIM = 220


def _short(text, n: int = CLAIM) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _attrs(attrs: dict, keys: tuple[str, ...]) -> str:
    return ", ".join(f"{k}={attrs[k]}" for k in keys if k in attrs)


SPAN_KEYS = ("retrieval.result_count", "retrieval.top_k", "retrieval.threshold", "tool.result_count",
             "tool.latency_ms", "tool.cache_age_s", "prompt.version", "gen_ai.request.model",
             "guardrail.decision", "guardrail.matched_rule")
ROOT_KEYS = ("config.top_k", "config.similarity_threshold", "config.prompt_version", "config.model",
             "config.tool_timeout_ms", "config.max_response_tokens", "config.chunk_size")


def from_result(tool: str, result: dict) -> list[Evidence]:
    if not isinstance(result, dict) or "error" in result:
        return []
    out: list[Evidence] = []
    if tool == "get_trace":
        for s in result.get("spans", []):
            keys = ROOT_KEYS if s.get("parent_ref") is None else SPAN_KEYS
            detail = _attrs(s.get("attrs", {}), keys)
            if s["name"] == "retrieve" and "retrieval.chunk_ids" in s.get("attrs", {}):
                detail += f", chunk_ids={s['attrs']['retrieval.chunk_ids']}"
            claim = f"span {s['name']} status={s['status']} {s['duration_ms']}ms" + (f": {detail}" if detail else "")
            if s.get("error"):
                claim += f"; error: {s['error']}"
            out.append({"type": "trace", "ref": s["ref"], "claim": _short(claim, 320)})
    elif tool == "get_retrieved_chunks":
        for c in result.get("chunks", []):
            if c.get("missing"):
                out.append({"type": "chunk", "ref": c["ref"], "claim": "chunk retrieved but no longer in the store"})
            else:
                out.append({"type": "chunk", "ref": c["ref"],
                            "claim": _short(f"retrieved {c['doc_id']}@{c['version']} score {c['score']}: {c['text']}")})
    elif tool == "document_history":
        for v in result.get("versions", []):
            out.append({"type": "change", "ref": v["ref"], "claim": _short(
                f"{result['doc_id']} {v['version']} published {v['published_at'][:10]}, "
                f"{v['indexed_chunk_count']}/{v['chunk_count']} chunks indexed; diff: {v['diff_vs_previous']}")})
    elif tool == "search_logs":
        for r in result.get("logs", []):
            out.append({"type": "log", "ref": r["ref"],
                        "claim": _short(f"{r['timestamp']} {r['level']} {r['component']}: {r['message']}")})
    elif tool == "recent_changes":
        for c in result.get("changes", []):
            out.append({"type": "change", "ref": c["ref"], "claim": _short(
                f"{c['kind']} on {c['target']} at {c['created_at']} by {c['author']}: {c['description']}")})
    elif tool == "diff_prompt_versions":
        a, b = result["a"], result["b"]
        first = [ln for ln in result.get("diff", "").splitlines() if ln[:1] in "+-" and ln[:3] not in ("---", "+++")]
        claim = (f"prompt {a['ref']} vs {b['ref']}: identical" if result.get("identical") else
                 f"prompt {a['ref']} -> {b['ref']} ({b.get('author')}, {b.get('note')}): " + " | ".join(first[:4]))
        out.append({"type": "change", "ref": b["ref"], "claim": _short(claim, 320)})
    elif tool == "query_metrics":
        for s in result.get("series", []):
            if not s.get("points"):
                continue
            low = min(s["points"], key=lambda p: p["value"])
            out.append({"type": "metric", "ref": low["ref"], "claim": _short(
                f"{result['metric']} {s['labels']}: min {s['min']} max {s['max']} mean {s['mean']} over "
                f"{s['count']} samples; lowest {low['value']} at {low['ts']}")})
    return out


def merge(existing: list[Evidence], new: list[Evidence]) -> list[Evidence]:
    """New items whose ref is not already in the pool (a later claim about the same ref is dropped)."""
    seen = {e["ref"] for e in existing}
    fresh = []
    for e in new:
        if e["ref"] not in seen:
            seen.add(e["ref"])
            fresh.append(e)
    return fresh
