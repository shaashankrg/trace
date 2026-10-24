"""Replay a recorded conversation with exactly one patch applied (Day 6).

The assistant's own chat loop (assistant.pipeline.answer) runs on an in-memory `Overrides` built
from the snapshot. Nothing is written to the database:

  retrieval  the snapshot's chunks, re-embedded, filtered by their index flags and the overrides
  prompt     the snapshot's prompt_versions
  tools      the responses recorded for the alert's own trace (tool_io.json); a patched tool is
             re-run against the snapshot's store tables (store.json) instead
  config     the snapshot's config.json

An empty patch must reproduce the original answer; eval/replay_determinism.py checks that.

Patches, one per replay. The component a patch targets is derived here from its kind and never
taken from the caller (failure scenario F5):

  use_doc_version     {"doc_id", "version"}   knowledge_base
  restore_doc         {"doc_id"}              knowledge_base   (index the newest version again)
  set_config          {"key", "value"}        retriever for top_k / similarity_threshold /
                                              embedding_model, model, guardrail, else config
  set_prompt_version  {"version"}             prompt
  rerun_tool          {"tool"}                tool:<tool>      (run it again on the store)
  bypass_cache        {"tool"}                tool:<tool>      (read the source, not the cache)

Anti-cheating, in code: doc and prompt versions must exist in the snapshot, and a set_config
value must be one the change log shows the key holding, or the value already on the trace.

Every replay is appended to a JSONL replay log so `replay:<id>` refs can be checked later.
"""
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPLAYS_DIR = ROOT / "eval" / "replays"
SNAPSHOT_FILES = ("alert", "config", "chunks", "documents", "prompt_versions", "changes", "tool_io",
                  "trace", "store")

TOOL_NAMES = ("lookup_order", "refund_status", "check_inventory")
CACHED_TOOLS = {"check_inventory"}
CONFIG_TARGETS = {"top_k": "retriever", "similarity_threshold": "retriever", "embedding_model": "retriever",
                  "model": "model", "guardrail_blocked_topics": "guardrail"}
NOT_PATCHABLE = {"prompt_version": "use a set_prompt_version patch",
                 "chunk_size": "documents are already chunked; a replay cannot re-chunk them"}
PATCH_FIELDS = {
    "none": (),
    "use_doc_version": ("doc_id", "version"),
    "restore_doc": ("doc_id",),
    "set_config": ("key", "value"),
    "set_prompt_version": ("version",),
    "rerun_tool": ("tool",),
    "bypass_cache": ("tool",),
}
# Column order of each tool's SQL. The recording went through jsonb, which reorders keys; the
# model originally saw them in this order, so replays restore it.
COLUMNS = {"lookup_order": ("order_id", "placed_at", "delivered_at", "status", "total", "items"),
           "refund_status": ("refund_id", "order_id", "status", "updated_at"),
           "check_inventory": ("product_id", "name", "qty", "refreshed_at")}
ITEM_COLUMNS = ("product_id", "name", "qty")

JUDGE_MODEL = os.environ.get("REPLAY_JUDGE_MODEL", "gemini-3.5-flash-lite")


# Set by tools/server.py. Inside the MCP server, the assistant's native libraries (numpy, torch,
# ssl...) must be imported before the stdin reader starts: on Windows a DLL loading while another
# thread is blocked reading the stdin pipe waits for that read, so a lazy import hangs forever.
IN_SERVER = False
PRELOADED = False


def preload() -> None:
    """Import everything a replay needs and load the embedding model. Takes ~30 s."""
    global PRELOADED
    import numpy  # noqa: F401
    from google import genai  # noqa: F401
    from google.genai import types  # noqa: F401
    from assistant import llm, pipeline  # noqa: F401
    from assistant.config import DEFAULTS
    from assistant.retrieve import embed
    _span_capture()
    embed(["warm up"], DEFAULTS["embedding_model"])
    PRELOADED = True


class PatchError(ValueError):
    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


# --- patches ------------------------------------------------------------------------------

def _text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def normalize_patch(patch) -> dict:
    """{"kind": ..., <its fields>}. None or {} is the empty patch, {"kind": "none"}."""
    if isinstance(patch, str):
        try:
            patch = json.loads(patch) if patch.strip() else None
        except ValueError as e:
            raise PatchError("patch is not JSON", 'e.g. {"kind": "set_prompt_version", "version": "v1"}') from e
    if not patch:
        return {"kind": "none"}
    if not isinstance(patch, dict):
        raise PatchError("patch must be an object", 'e.g. {"kind": "rerun_tool", "tool": "lookup_order"}')
    kind = patch.get("kind")
    if kind not in PATCH_FIELDS:
        raise PatchError(f"unknown patch kind {kind!r}", f"one of: {', '.join(k for k in PATCH_FIELDS if k != 'none')}")
    fields = PATCH_FIELDS[kind]
    missing = [f for f in fields if patch.get(f) in (None, "")]
    if missing:
        raise PatchError(f"{kind} needs {', '.join(missing)}", f"{kind} takes exactly: {', '.join(fields)}")
    extra = sorted(set(patch) - {"kind", *fields})
    if extra:
        raise PatchError(f"{kind} does not take {', '.join(extra)}", "a replay changes exactly one thing")
    return {"kind": kind, **{f: _text(patch[f]) for f in fields}}


def target_component(patch: dict) -> str | None:
    kind = patch["kind"]
    if kind in ("use_doc_version", "restore_doc"):
        return "knowledge_base"
    if kind == "set_config":
        return CONFIG_TARGETS.get(patch["key"], "config")
    if kind == "set_prompt_version":
        return "prompt"
    if kind in ("rerun_tool", "bypass_cache"):
        return f"tool:{patch['tool']}"
    return None


def _same(a: str, b: str) -> bool:
    a, b = a.strip(), b.strip()
    for parse in (float, json.loads):
        try:
            return parse(a) == parse(b)
        except (ValueError, TypeError):
            pass
    return a == b


# --- snapshot -----------------------------------------------------------------------------

class Snapshot:
    """The files of one recorded case, read directly. Only SNAPSHOT_FILES are opened."""

    def __init__(self, case_dir: Path | str) -> None:
        self.dir = Path(case_dir)
        self.case_id = self.dir.name
        data = {}
        for name in SNAPSHOT_FILES:
            path = self.dir / f"{name}.json"
            data[name] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        self.alert = data["alert"] or {}
        self.config = {r["key"]: r["value"] for r in data["config"] or []}
        self.chunks = data["chunks"] or []
        self.documents = data["documents"] or []
        self.prompts = {p["version"]: p["template"] for p in data["prompt_versions"] or []}
        self.changes = data["changes"] or []
        self.tool_io = data["tool_io"] or []
        self.trace = data["trace"] or {"data": []}
        self.store = data["store"]

    def recorded_calls(self) -> list[dict]:
        """Tool calls of the alert's own trace. tool_io.json also holds the healthy pre-fault run."""
        return [c for c in self.tool_io if c["trace_id"] == self.alert.get("trace_id")]

    def trace_config(self) -> dict[str, str]:
        """kestrel.config.* on the root span of the alert's trace."""
        for t in self.trace.get("data", []):
            if t.get("traceID") != self.alert.get("trace_id"):
                continue
            for s in t["spans"]:
                if not s.get("references"):
                    return {tag["key"].removeprefix("kestrel.config."): _text(tag["value"])
                            for tag in s.get("tags", []) if tag["key"].startswith("kestrel.config.")}
        return {}

    def config_values(self, key: str) -> list[str]:
        """Every value the change log shows `key` holding, plus the value on the trace."""
        pattern = re.compile(rf"^[-+]\s*{re.escape(key)}:\s*(.*?)\s*$", re.MULTILINE)
        seen = [m.group(1) for c in self.changes if c.get("target") == f"config:{key}"
                for m in pattern.finditer(c.get("diff") or "")]
        if key in self.trace_config():
            seen.append(self.trace_config()[key])
        return list(dict.fromkeys(seen))


@dataclass
class Overrides:
    config: dict[str, str]
    prompt_version: str
    include_chunks: set[str] = field(default_factory=set)   # forced into the index
    exclude_chunks: set[str] = field(default_factory=set)   # forced out of the index
    tool_responses: dict[str, list[dict]] = field(default_factory=dict)  # tool key -> recorded calls, in order
    live_tools: set[str] = field(default_factory=set)       # re-run on the store, not served from the recording
    bypass_cache: set[str] = field(default_factory=set)


def tool_key(name: str, tool_input: dict) -> str:
    return f"{name}:{json.dumps(tool_input, sort_keys=True)}"


def baseline(snap: Snapshot) -> Overrides:
    """Exactly the state the snapshot recorded."""
    responses: dict[str, list[dict]] = {}
    for call in snap.recorded_calls():
        responses.setdefault(tool_key(call["tool_name"], call["input"]), []).append(call)
    return Overrides(config=dict(snap.config), prompt_version=snap.config.get("prompt_version", "v1"),
                     tool_responses=responses)


def _doc_chunks(snap: Snapshot, doc_id: str, version: str) -> set[str]:
    return {c["chunk_id"] for c in snap.chunks if c["doc_id"] == doc_id and c["version"] == version}


def apply_patch(ov: Overrides, patch: dict, snap: Snapshot) -> Overrides:
    """The overrides with `patch` applied. Raises PatchError for anything the snapshot does not back."""
    kind = patch["kind"]
    if kind == "none":
        return ov
    if kind in ("use_doc_version", "restore_doc"):
        doc_id = patch["doc_id"]
        versions = sorted((d for d in snap.documents if d["doc_id"] == doc_id),
                          key=lambda d: (d["published_at"], d["version"]))
        if not versions:
            raise PatchError(f"no document {doc_id!r} in the history", "take doc ids from document_history")
        version = patch.get("version") or versions[-1]["version"]
        if version not in {d["version"] for d in versions}:
            raise PatchError(f"{doc_id} has no version {version!r}",
                             f"versions in the history: {', '.join(d['version'] for d in versions)}")
        keep = _doc_chunks(snap, doc_id, version)
        if not keep:
            raise PatchError(f"{doc_id}@{version} has no chunks to index", "pick a version with chunks")
        others = {c["chunk_id"] for c in snap.chunks if c["doc_id"] == doc_id} - keep
        return replace(ov, include_chunks=(ov.include_chunks - others) | keep,
                       exclude_chunks=(ov.exclude_chunks - keep) | others)
    if kind == "set_config":
        key, value = patch["key"], patch["value"]
        if key in NOT_PATCHABLE:
            raise PatchError(f"{key} cannot be patched with set_config", NOT_PATCHABLE[key])
        if key not in ov.config:
            raise PatchError(f"no config key {key!r}", f"keys: {', '.join(sorted(ov.config))}")
        allowed = snap.config_values(key)
        if not any(_same(value, v) for v in allowed):
            raise PatchError(f"{key}={value} is not a value the change log or the trace shows {key} holding",
                             f"values on record: {', '.join(allowed) or 'only the current one'}")
        return replace(ov, config={**ov.config, key: value})
    if kind == "set_prompt_version":
        version = patch["version"]
        if version not in snap.prompts:
            raise PatchError(f"no prompt version {version!r}", f"versions: {', '.join(sorted(snap.prompts))}")
        return replace(ov, prompt_version=version, config={**ov.config, "prompt_version": version})
    tool = patch["tool"]
    if tool not in TOOL_NAMES:
        raise PatchError(f"no tool {tool!r}", f"tools: {', '.join(TOOL_NAMES)}")
    if kind == "bypass_cache":
        if tool not in CACHED_TOOLS:
            raise PatchError(f"{tool} has no cache", f"cached tools: {', '.join(sorted(CACHED_TOOLS))}")
        return replace(ov, live_tools=ov.live_tools | {tool}, bypass_cache=ov.bypass_cache | {tool})
    return replace(ov, live_tools=ov.live_tools | {tool})


# --- the store, for re-run tools ---------------------------------------------------------------

def ordered(name: str, rows: list[dict]) -> list[dict]:
    cols = COLUMNS.get(name)
    if not cols:
        return rows
    out = []
    for r in rows:
        row = {c: r.get(c) for c in cols if c in r}
        if "items" in row:
            row["items"] = [{c: i.get(c) for c in ITEM_COLUMNS} for i in row["items"]]
        out.append(row)
    return out


def _ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Store:
    """The three store tools over store.json, returning what their SQL returns. `now` is the
    alert time, so cache ages read as they did when the case was recorded."""

    def __init__(self, tables: dict, now: str) -> None:
        if not tables:
            raise PatchError("this recording has no store snapshot, so tools cannot be re-run",
                             "run python -m eval.backfill_snapshots for this case")
        self.t = tables
        self.now = _ts(now)
        self.names = {p["product_id"]: p["name"] for p in tables["products"]}

    def run(self, name: str, args: dict, bypass_cache: bool = False) -> list[dict]:
        if name == "lookup_order":
            return self.lookup_order(**args)
        if name == "refund_status":
            return self.refund_status(**args)
        if name == "check_inventory":
            return self.check_inventory(**args, bypass_cache=bypass_cache)
        raise KeyError(f"unknown tool {name!r}")

    def lookup_order(self, customer_id: str) -> list[dict]:
        out = []
        for o in self.t["orders"]:
            if o["customer_id"] != customer_id:
                continue
            items = sorted(({"product_id": i["product_id"], "name": self.names[i["product_id"]], "qty": i["qty"]}
                            for i in self.t["order_items"] if i["order_id"] == o["order_id"]),
                           key=lambda i: i["product_id"])
            if items:  # the SQL inner-joins order_items
                out.append({"order_id": o["order_id"], "placed_at": o["placed_at"],
                            "delivered_at": o["delivered_at"], "status": o["status"],
                            "total": o["total"], "items": items})
        return sorted(out, key=lambda o: _ts(o["placed_at"]), reverse=True)

    def refund_status(self, order_id: str) -> list[dict]:
        return [{"refund_id": r["refund_id"], "order_id": r["order_id"], "status": r["status"],
                 "updated_at": r["updated_at"]} for r in self.t["refunds"] if r["order_id"] == order_id]

    def check_inventory(self, product_id: str, bypass_cache: bool = False) -> list[dict]:
        table, stamp = ("inventory_source", "updated_at") if bypass_cache else ("inventory_cache", "refreshed_at")
        return [{"product_id": r["product_id"], "name": self.names[r["product_id"]], "qty": r["qty"],
                 "refreshed_at": r[stamp]} for r in self.t[table] if r["product_id"] == product_id]

    def cache_age_s(self, rows: list[dict]) -> float | None:
        return (self.now - _ts(rows[0]["refreshed_at"])).total_seconds() if rows and rows[0].get("refreshed_at") else None


# --- answer check -------------------------------------------------------------------------

def _found(pattern, text: str) -> bool:
    options = pattern if isinstance(pattern, list) else [pattern]
    return any(re.search(p, text, re.IGNORECASE | re.MULTILINE) for p in options)


def code_check(check: dict, answer: str) -> str:
    """"match", "no_match" or "inconclusive" (every required pattern is there, but so is a
    forbidden one: an answer that states both the right and the wrong fact)."""
    musts = [_found(p, answer) for p in check.get("must_contain", [])]
    forbidden = any(_found(p, answer) for p in check.get("must_not_contain", []))
    if not all(musts):
        return "no_match"
    return "inconclusive" if forbidden else "match"


def llm_judge(question: str, correction: str, answer: str) -> tuple[bool, dict]:
    """Fallback for inconclusive code checks: does the answer agree with the reviewer?"""
    from google import genai
    from google.genai import types

    client = genai.Client(http_options=types.HttpOptions(timeout=60_000))
    prompt = (f"A reviewer corrected a support assistant.\nQuestion: {question}\n"
              f"Reviewer's correction: {correction}\nNew answer: {answer}\n\n"
              "Does the new answer give the customer what the correction says is right, without "
              'asserting the wrong version as true? Reply as JSON: {"agrees": true|false}.')
    r = client.models.generate_content(model=JUDGE_MODEL, contents=prompt, config=types.GenerateContentConfig(
        temperature=0, response_mime_type="application/json",
        thinking_config=types.ThinkingConfig(thinking_level="MINIMAL")))
    try:
        agrees = bool(json.loads(r.text or "{}").get("agrees"))
    except ValueError:
        agrees = False
    u = r.usage_metadata
    return agrees, {"model": JUDGE_MODEL, "input_tokens": (u.prompt_token_count or 0) if u else 0,
                    "output_tokens": (u.candidates_token_count or 0) if u else 0}


def check_answer(alert: dict, answer: str, judge=llm_judge) -> dict:
    check = alert.get("answer_check")
    verdict = code_check(check, answer) if check else "inconclusive"
    if verdict != "inconclusive":
        return {"matches_correction": verdict == "match", "judge": "code", "code_verdict": verdict}
    if judge is None:  # the determinism test compares code verdicts only
        return {"matches_correction": None, "judge": "skipped", "code_verdict": verdict}
    agrees, usage = judge(alert["question"], alert["reviewer_correction"], answer)
    return {"matches_correction": agrees, "judge": "llm", "code_verdict": verdict, "judge_usage": usage}


# --- running a replay ---------------------------------------------------------------------

_exporter = None
_capture_lock = threading.Lock()


def _span_capture():
    """Collect replay spans in memory (each replay's trace goes into the replay log)."""
    global _exporter
    with _capture_lock:
        return _exporter or _install_capture()


def _install_capture():
    global _exporter
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider(resource=Resource.create({"service.name": "kestrel-replay"}))
        trace.set_tracer_provider(provider)
    _exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(_exporter))
    return _exporter


_vectors: dict[tuple[str, str], object] = {}


def _embed(model: str, texts: list[str]):
    import numpy as np
    from assistant.retrieve import embed

    todo = list(dict.fromkeys(t for t in texts if (model, t) not in _vectors))
    if todo:
        for t, v in zip(todo, embed(todo, model)):
            _vectors[(model, t)] = v
    return np.array([_vectors[(model, t)] for t in texts]) if texts else np.zeros((0, 1))


def retrieve(snap: Snapshot, ov: Overrides, cfg, question: str):
    """assistant.retrieve.retrieve over the snapshot's chunks instead of pgvector."""
    from opentelemetry import trace
    from assistant.retrieve import Chunk

    with trace.get_tracer("kestrel.assistant").start_as_current_span("retrieve") as span:
        span.set_attribute("kestrel.conversation_id", snap.alert.get("conversation_id", ""))
        span.set_attribute("kestrel.retrieval.top_k", cfg.top_k)
        span.set_attribute("kestrel.retrieval.threshold", cfg.similarity_threshold)
        span.set_attribute("kestrel.embedding.model", cfg.embedding_model)
        pool = [c for c in snap.chunks
                if (c["indexed"] or c["chunk_id"] in ov.include_chunks) and c["chunk_id"] not in ov.exclude_chunks]
        chunks = []
        if pool:
            q = _embed(cfg.embedding_model, [question])[0]
            scores = _embed(cfg.embedding_model, [c["text"] for c in pool]) @ q
            ranked = sorted(range(len(pool)), key=lambda i: -float(scores[i]))[: cfg.top_k]
            chunks = [Chunk(pool[i]["chunk_id"], pool[i]["text"], float(scores[i])) for i in ranked
                      if float(scores[i]) >= cfg.similarity_threshold]
        span.set_attribute("kestrel.retrieval.result_count", len(chunks))
        span.set_attribute("kestrel.retrieval.chunk_ids", [c.chunk_id for c in chunks])
        span.set_attribute("kestrel.retrieval.scores", [round(c.score, 4) for c in chunks])
        return chunks


class ToolRunner:
    """Serves recorded tool responses in order; re-runs patched tools (and any call the recording
    does not have) against the store."""

    def __init__(self, snap: Snapshot, ov: Overrides) -> None:
        self.snap, self.ov = snap, ov
        self.queues = {k: list(v) for k, v in ov.tool_responses.items()}
        self._store = None
        self.calls: list[dict] = []

    def store(self) -> Store:
        if self._store is None:
            self._store = Store(self.snap.store, self.snap.alert["fired_at"])
        return self._store

    def __call__(self, name: str, tool_input: dict) -> list[dict]:
        from opentelemetry import trace

        with trace.get_tracer("kestrel.assistant").start_as_current_span(f"tool.{name}") as span:
            span.set_attribute("gen_ai.operation.name", "execute_tool")
            span.set_attribute("gen_ai.tool.name", name)
            queue = self.queues.get(tool_key(name, tool_input))
            latency, timed_out = None, False
            if name in self.ov.live_tools:
                rows, source = self.store().run(name, tool_input, name in self.ov.bypass_cache), "store"
            elif queue:
                call = queue.pop(0) if len(queue) > 1 else queue[0]  # a repeated call gets the last response again
                rows, source = ordered(name, call["output"]), "recording"
                latency, timed_out = call["latency_ms"], call["timed_out"]
            else:
                rows, source = self.store().run(name, tool_input), "store:unrecorded"
            span.set_attribute("kestrel.tool.result_count", len(rows))
            span.set_attribute("kestrel.tool.served_from", source)
            if latency is not None:
                span.set_attribute("kestrel.tool.latency_ms", round(latency, 1))
            if name in CACHED_TOOLS and rows and source != "recording":
                span.set_attribute("kestrel.tool.cache_age_s", self.store().cache_age_s(rows))
            self.calls.append({"tool": name, "input": tool_input, "served_from": source,
                               "result_count": len(rows), "timed_out": timed_out})
            return rows


def _span_record(s) -> dict:
    return {"span_id": format(s.context.span_id, "016x"),
            "parent_id": format(s.parent.span_id, "016x") if s.parent else None,
            "name": s.name, "status": s.status.status_code.name.lower(),
            "attrs": {k: (list(v) if isinstance(v, tuple) else v) for k, v in (s.attributes or {}).items()}}


def _cost(model: str, input_tokens: int, output_tokens: int) -> float:
    from agent.llm import price
    p_in, p_out = price(model)
    return (input_tokens * p_in + output_tokens * p_out) / 1e6


def log_path(case_id: str) -> Path:
    override = os.environ.get("TRACEWISE_REPLAY_LOG")
    return Path(override) if override else REPLAYS_DIR / f"{case_id}.jsonl"


def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


SAMPLES = int(os.environ.get("TRACEWISE_REPLAY_SAMPLES", "3"))
_controls: dict[str, dict] = {}
_controls_lock = threading.Lock()


def _sample(snap: Snapshot, ov: Overrides, patch: dict, replay_id: str, n: int, judge) -> dict:
    """One run of the chat loop under `ov`, with its own trace."""
    from opentelemetry import trace
    from assistant import pipeline
    from assistant.config import from_values

    cfg = from_values(ov.config)
    template = snap.prompts.get(ov.prompt_version)
    if template is None:
        raise PatchError(f"prompt version {ov.prompt_version!r} is not in the snapshot")
    exporter = _span_capture()
    runner = ToolRunner(snap, ov)
    question = snap.alert["question"]
    with trace.get_tracer("kestrel.replay").start_as_current_span("replay") as root:
        root.set_attribute("kestrel.replay", True)
        root.set_attribute("kestrel.replay_of", snap.alert.get("conversation_id", ""))
        root.set_attribute("kestrel.replay.id", replay_id)
        root.set_attribute("kestrel.replay.sample", n)
        root.set_attribute("kestrel.replay.patch", json.dumps(patch))
        root.set_attribute("kestrel.replay.target_component", target_component(patch) or "")
        root.set_attributes(cfg.span_attributes() | {"kestrel.config.guardrail_blocked_topics":
                                                     json.dumps(cfg.guardrail_blocked_topics)})
        result = pipeline.answer(cfg, question, lambda: retrieve(snap, ov, cfg, question),
                                 lambda: template, runner)
        trace_id = root.get_span_context().trace_id
    spans = [s for s in exporter.get_finished_spans() if s.context.trace_id == trace_id]
    llm_spans = [s for s in spans if s.name == "llm.chat"]
    tokens_in = sum(s.attributes.get("gen_ai.usage.input_tokens", 0) for s in llm_spans)
    tokens_out = sum(s.attributes.get("gen_ai.usage.output_tokens", 0) for s in llm_spans)
    verdict = check_answer(snap.alert, result.answer, judge)
    cost = _cost(cfg.model, tokens_in, tokens_out)
    if "judge_usage" in verdict:
        u = verdict["judge_usage"]
        cost += _cost(u["model"], u["input_tokens"], u["output_tokens"])
    return {"sample": n, "trace_id": format(trace_id, "032x"), "answer": result.answer, **verdict,
            "retrieved": result.chunk_ids, "tools": runner.calls, "model": cfg.model,
            "input_tokens": tokens_in, "output_tokens": tokens_out, "cost_usd": cost,
            "spans": [_span_record(s) for s in spans]}


def _prepare(snap: Snapshot, ov: Overrides) -> None:
    """Import the assistant and embed the snapshot in the calling thread, before the samples start.
    First-time imports of numpy/torch from several threads at once deadlock on the import lock
    (seen in the MCP server process, where nothing had imported them yet)."""
    from assistant import pipeline  # noqa: F401
    from assistant.config import from_values

    _span_capture()
    model = from_values(ov.config).embedding_model
    _embed(model, [snap.alert["question"]] + [c["text"] for c in snap.chunks])


def _majority(samples: list[dict]):
    votes = [s["matches_correction"] for s in samples if s["matches_correction"] is not None]
    if not votes:
        return None
    return sum(votes) * 2 > len(votes)


def control(snap: Snapshot, log: Path | None = None, judge=llm_judge, purpose: str = "control") -> dict:
    """The empty-patch replay for this case, run once per process."""
    with _controls_lock:
        if snap.case_id not in _controls:
            _controls[snap.case_id] = run_replay(snap, {}, log, judge, purpose, with_control=False)
        return _controls[snap.case_id]


def run_replay(snap: Snapshot, patch, log: Path | None = None, judge=llm_judge, purpose: str = "investigation",
               samples: int | None = None, with_control: bool = True) -> dict:
    """Replay the alert's conversation with `patch`, `samples` times, and return (and log) the record.

    The assistant model is not deterministic even at temperature 0, so one run proves little:
    matches_correction is the majority over the samples. A patched replay is also compared with
    the empty-patch control: `proves_fix` only when the patch fixes the answer and the control
    does not (otherwise the answer was flaky, not broken by what the patch changed)."""
    from concurrent.futures import ThreadPoolExecutor

    patch = normalize_patch(patch)
    ov = apply_patch(baseline(snap), patch, snap)
    _prepare(snap, ov)
    replay_id = uuid.uuid4().hex[:12]
    n = samples or SAMPLES
    with ThreadPoolExecutor(n) as pool:
        runs = list(pool.map(lambda i: _sample(snap, ov, patch, replay_id, i, judge), range(n)))
    matches = _majority(runs)
    first = runs[0]
    record = {
        "replay_id": replay_id, "ref": f"replay:{replay_id}", "purpose": purpose,
        "case_id": snap.case_id, "replay_of": snap.alert.get("conversation_id"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "patch": patch, "target_component": target_component(patch),
        "matches_correction": matches,
        "judge": next((j for j in ("llm", "skipped") if any(r["judge"] == j for r in runs)), "code"),
        "votes": [r["matches_correction"] for r in runs],
        "trace_id": first["trace_id"], "answer": first["answer"], "retrieved": first["retrieved"],
        "tools": first["tools"], "model": first["model"], "prompt_version": ov.prompt_version,
        "input_tokens": sum(r["input_tokens"] for r in runs), "output_tokens": sum(r["output_tokens"] for r in runs),
        "cost_usd": round(sum(r["cost_usd"] for r in runs), 6),
        "samples": runs,
    }
    if patch["kind"] != "none" and with_control:
        ctl = control(snap, log, judge)
        record["control_ref"] = ctl["ref"]
        record["control_matches_correction"] = ctl["matches_correction"]
        record["proves_fix"] = bool(matches) and ctl["matches_correction"] is False
    path = log or log_path(snap.case_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _log_lock, path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    return record


_log_lock = threading.Lock()


def summary(record: dict, answer_chars: int = 700) -> dict:
    """What the investigator's tool returns: the verdict and enough of the answer to read."""
    answer = record["answer"]
    return {"replay_ref": record["ref"], "trace_id": record["trace_id"],
            "answer": answer if len(answer) <= answer_chars else answer[: answer_chars - 1] + "…",
            "matches_correction": record["matches_correction"], "votes": record["votes"],
            "judge": record["judge"], "patch": record["patch"], "target_component": record["target_component"],
            **({"control_matches_correction": record["control_matches_correction"],
                "proves_fix": record["proves_fix"]} if "proves_fix" in record else {}),
            "retrieved": record["retrieved"],
            "tools": [{k: c[k] for k in ("tool", "input", "served_from", "result_count")} for c in record["tools"]]}
