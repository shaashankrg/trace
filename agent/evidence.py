"""Tool results -> typed Evidence items: a ref the verifier can check and a one-line claim.

Done in code, not by the model, so every claim says only what the tool returned and every ref is
copied, never typed. A document version or prompt version counts as `change` evidence: both are
published artifacts in the change history.

`meta` carries the facts the verification gate (agent/verify.py) needs to decide which component an
item can support: a span's name and attributes, a log's component, a change's target, a replay's
outcome. It comes from the tool output, never from the model.
"""
import json

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
                detail += f", retrieval.chunk_ids={s['attrs']['retrieval.chunk_ids']}"
            claim = f"span {s['name']} status={s['status']} {s['duration_ms']}ms" + (f": {detail}" if detail else "")
            if s.get("error"):
                claim += f"; error: {s['error']}"
            out.append({"type": "trace", "ref": s["ref"], "claim": _short(claim, 320),
                        "meta": {"span": s["name"], "root": s.get("parent_ref") is None, "attrs": s.get("attrs", {})}})
    elif tool == "get_retrieved_chunks":
        for c in result.get("chunks", []):
            if c.get("missing"):
                out.append({"type": "chunk", "ref": c["ref"], "claim": "chunk retrieved but no longer in the store",
                            "meta": {"doc_id": c["ref"].removeprefix("chunk:").split("@")[0]}})
            else:
                out.append({"type": "chunk", "ref": c["ref"],
                            "claim": _short(f"retrieved {c['doc_id']}@{c['version']} score {c['score']}: {c['text']}"),
                            "meta": {"doc_id": c["doc_id"]}})
    elif tool == "document_history":
        for v in result.get("versions", []):
            out.append({"type": "change", "ref": v["ref"], "claim": _short(
                f"{result['doc_id']} {v['version']} published {v['published_at'][:10]}, "
                f"{v['indexed_chunk_count']}/{v['chunk_count']} chunks indexed; diff: {v['diff_vs_previous']}"),
                "meta": {"target": f"doc:{result['doc_id']}"}})
    elif tool == "search_logs":
        for r in result.get("logs", []):
            out.append({"type": "log", "ref": r["ref"],
                        "claim": _short(f"{r['timestamp']} {r['level']} {r['component']}: {r['message']}"),
                        "meta": {"component": r["component"]}})
    elif tool == "recent_changes":
        for c in result.get("changes", []):
            out.append({"type": "change", "ref": c["ref"], "claim": _short(
                f"{c['kind']} on {c['target']} at {c['created_at']} by {c['author']}: {c['description']}"),
                "meta": {"target": c["target"], "kind": c["kind"]}})
    elif tool == "diff_prompt_versions":
        a, b = result["a"], result["b"]
        first = [ln for ln in result.get("diff", "").splitlines() if ln[:1] in "+-" and ln[:3] not in ("---", "+++")]
        claim = (f"prompt {a['ref']} vs {b['ref']}: identical" if result.get("identical") else
                 f"prompt {a['ref']} -> {b['ref']} ({b.get('author')}, {b.get('note')}): " + " | ".join(first[:4]))
        out.append({"type": "change", "ref": b["ref"], "claim": _short(claim, 320), "meta": {"target": "prompt"}})
    elif tool == "query_metrics":
        for s in result.get("series", []):
            if not s.get("points"):
                continue
            low = min(s["points"], key=lambda p: p["value"])
            out.append({"type": "metric", "ref": low["ref"], "claim": _short(
                f"{result['metric']} {s['labels']}: min {s['min']} max {s['max']} mean {s['mean']} over "
                f"{s['count']} samples; lowest {low['value']} at {low['ts']}"),
                "meta": {"metric": result["metric"], "labels": s["labels"]}})
    elif tool == "replay_conversation":
        patch = json.dumps(result.get("patch"))
        proof = ("" if "proves_fix" not in result else
                 f"; unpatched control matches={result['control_matches_correction']}, proves_fix={result['proves_fix']}")
        out.append({"type": "replay", "ref": result["replay_ref"], "claim": _short(
            f"replay with {patch} (targets {result.get('target_component')}): matches_correction="
            f"{result['matches_correction']} votes={result.get('votes')}{proof}; answer: {result.get('answer', '')}", 360),
            "meta": {"target_component": result.get("target_component"),
                     "matches_correction": result["matches_correction"],
                     "proves_fix": result.get("proves_fix", False)}})
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
