"""The evidence verifier: checks every citation in a diagnosis against the recording itself.

It runs in the evaluator, after the investigation, and shares no code with the agent or the MCP
tools: it parses the raw snapshot files (trace.json, chunks.json, logs.json, ...) and the replay
log directly, so a bug in a tool cannot hide a bad citation.

For every evidence item (and every ruled_out ref and the cause_ref):
  - the ref exists in the snapshot (or, for replay:<id>, in the replay log)
  - a `quote` is a substring of the cited text, after normalizing whitespace and case
  - an `attribute` + `value` matches what the span (or metric sample) really holds
  - a replay's claimed result (`value` of `matches_correction`) is what the replay log shows

One failed item marks the whole diagnosis `hallucinated`, and the scorer counts it as wrong even
when the root component is right.
"""
import ast
import json
import re
from datetime import datetime, timezone
from pathlib import Path

SPAN = re.compile(r"^span:([0-9a-f]{32})/([0-9a-f]{16})$")
CHUNK = re.compile(r"^chunk:([^@#\s]+@[^@#\s]+#\d+(?:/c\d+)?)$")
DOC = re.compile(r"^doc:([^@#\s]+)@([^@#\s]+)$")
METRIC = re.compile(r"^metric:([A-Za-z_:][A-Za-z0-9_:]*)@(.+)$")
SIMPLE = re.compile(r"^(log|change|prompt|replay):(.+)$")
REL_TOL = 1e-3


def _norm(text) -> str:
    return " ".join(str(text).lower().split())


def _quote_found(quote: str, text: str) -> bool:
    q = _norm(quote).strip("\"'").rstrip("….").strip()  # tolerate a copied "…" from a clipped tool output
    return bool(q) and q in _norm(text)


def _parse(value):
    if isinstance(value, str):
        v = value.strip()
        if v.lower() in ("true", "false"):
            return v.lower() == "true"
        for parse in (json.loads, ast.literal_eval):  # the same list may come JSON- or Python-quoted
            try:
                return parse(v)
            except (ValueError, SyntaxError):
                pass
        return v
    return value


def same_value(claimed, actual) -> bool:
    a, b = _parse(claimed), _parse(actual)
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= REL_TOL * max(abs(a), abs(b), 1e-9) or abs(a - b) < 1e-9
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same_value(x, y) for x, y in zip(a, b))
    return _norm(a) == _norm(b)


def _ts(text: str) -> int:
    dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())


class Recording:
    """One case's snapshot files and replay log, as plain lookups."""

    def __init__(self, case_dir: Path, replay_logs: list[Path] | None = None) -> None:
        def load(name, default):
            path = case_dir / f"{name}.json"
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

        self.spans = {(t["traceID"], s["spanID"]): s for t in load("trace", {"data": []})["data"] for s in t["spans"]}
        self.chunks = {c["chunk_id"]: c for c in load("chunks", [])}
        self.docs = {(d["doc_id"], d["version"]): d for d in load("documents", [])}
        self.logs = {r["log_id"]: r for r in load("logs", [])}
        self.changes = {c["change_id"]: c for c in load("changes", [])}
        self.prompts = {p["version"]: p for p in load("prompt_versions", [])}
        self.metrics = load("metrics", {})
        self.replays = {}
        for path in replay_logs or []:
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        rec = json.loads(line)
                        self.replays[rec["replay_id"]] = rec

    def metric_value(self, name: str, ts: int):
        for series in self.metrics.get(name) or []:
            for t, v in series.get("values", []):
                if int(float(t)) == ts:
                    return float(v)
        return None


def check_item(rec: Recording, item: dict) -> str | None:
    """None if the item checks out, else what is wrong with it."""
    ref = item.get("ref") or ""
    quote, attribute, value = item.get("quote"), item.get("attribute"), item.get("value")

    if m := SPAN.match(ref):
        span = rec.spans.get(m.groups())
        if span is None:
            return "no such span in the trace"
        if attribute:
            tags = {t["key"]: t["value"] for t in span.get("tags", [])}
            key = next((k for k in (attribute, f"kestrel.{attribute}") if k in tags), None)
            if key is None:
                return f"span {span['operationName']} has no attribute {attribute}"
            if value is not None and not same_value(value, tags[key]):
                return f"{attribute} is {tags[key]!r}, not {value!r}"
        if quote and not _quote_found(quote, json.dumps(span.get("tags", []))):
            return "quote not in the span's attributes"
        return None
    if m := CHUNK.match(ref):
        chunk = rec.chunks.get(m.group(1))
        if chunk is None:
            return "no such chunk"
        if quote and not _quote_found(quote, chunk["text"]):
            return "quote not in the chunk text"
        return None
    if m := DOC.match(ref):
        doc = rec.docs.get(m.groups())
        if doc is None:
            return "no such document version"
        if quote and not _quote_found(quote, doc.get("body", "")):
            return "quote not in the document"
        return None
    if m := METRIC.match(ref):
        try:
            sample = rec.metric_value(m.group(1), _ts(m.group(2)))
        except ValueError:
            return "bad metric timestamp"
        if sample is None:
            return "no such metric sample"
        if value is not None and not same_value(value, sample):
            return f"sample is {sample}, not {value!r}"
        return None
    if m := SIMPLE.match(ref):
        kind, key = m.groups()
        if kind == "log":
            row = rec.logs.get(key)
            text = (row or {}).get("message", "")
        elif kind == "change":
            row = rec.changes.get(ref)
            text = f"{(row or {}).get('description', '')} {(row or {}).get('diff', '')}"
        elif kind == "prompt":
            row = rec.prompts.get(key)
            text = (row or {}).get("template", "")
        else:
            row = rec.replays.get(key)
            if row is None:
                return "no such replay in the replay log"
            if value is not None and not same_value(value, row["matches_correction"]):
                return f"replay {key} has matches_correction={row['matches_correction']}, cited as {value!r}"
            return None
        if row is None:
            return f"no such {kind}"
        if quote and not _quote_found(quote, text):
            return f"quote not in the {kind} text"
        return None
    return "not a ref"


def verify(diagnosis: dict, case_dir: Path, replay_logs: list[Path] | None = None) -> dict:
    """{"hallucinated", "checked", "failures": [{ref, where, problem}]} for one diagnosis."""
    rec = Recording(case_dir, replay_logs)
    items = [("evidence", e) for e in diagnosis.get("evidence") or []]
    items += [("ruled_out", {"ref": r["ref"]}) for r in diagnosis.get("ruled_out") or [] if r.get("ref")]
    if diagnosis.get("cause_ref"):
        items.append(("cause_ref", {"ref": diagnosis["cause_ref"]}))
    failures = []
    for where, item in items:
        problem = check_item(rec, item)
        if problem:
            failures.append({"ref": item.get("ref"), "where": where, "problem": problem})
    return {"hallucinated": bool(failures), "checked": len(items), "failures": failures}
