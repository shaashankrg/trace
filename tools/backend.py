"""Where the tools read from: the live stack, or a snapshot recorded by eval.run_case.

Every tool goes through `Backend`, so the same tool code runs against Jaeger / OpenSearch /
Postgres / Prometheus or against eval/runs/<case_id>/*.json. Pick one with

    TRACEWISE_BACKEND=live
    TRACEWISE_BACKEND=snapshot:<case_id>

Backends return plain data (JSON-like dicts, ISO timestamp strings). Raw trace JSON is the
Jaeger API shape in both; turning it into something compact is the tools' job.

Ground truth stays out of reach by construction: LiveBackend connects as the `tools` Postgres
role, and SnapshotBackend opens only the files in SNAPSHOT_FILES inside one case folder.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

RUNS_DIR = Path(__file__).resolve().parent.parent / "eval" / "runs"

# The only files a SnapshotBackend will open. Case specs (injector/cases), the eval schema and
# facts.yaml are not snapshot files and are never read.
SNAPSHOT_FILES = frozenset({
    "alert", "trace", "logs", "metrics", "changes", "documents", "chunks",
    "prompt_versions", "config", "tool_io", "store",
})

_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")


class Backend(Protocol):
    def trace_id_for(self, conversation_id: str) -> str:
        """The newest trace of a conversation (a 32-hex trace id passes through). LookupError if none."""
    def trace(self, trace_id: str) -> dict:
        """Raw Jaeger trace JSON, {"data": [{"traceID", "spans", "processes"}]}. LookupError if none."""
    def logs(self, component: str | None, query: str | None, start: datetime | None,
             end: datetime | None, limit: int) -> tuple[list[dict], int]:
        """(first `limit` matches oldest first, total matches)."""
    def log(self, log_id: str) -> dict | None: ...
    def changes(self, start: datetime | None, end: datetime | None) -> list[dict]:
        """Newest first."""
    def documents(self, doc_id: str) -> list[dict]:
        """Every version of one doc, with body, chunk_count and indexed_chunks."""
    def chunks(self, ids: list[str]) -> list[dict]: ...
    def prompt_versions(self) -> list[dict]: ...
    def metric(self, name: str, start: datetime | None, end: datetime | None) -> list[dict]:
        """[{"labels": {...}, "values": [(unix_ts, float), ...]}] oldest first."""
    def metric_names(self) -> list[str]: ...


def iso(value) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


def as_dt(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _in_window(ts, start: datetime | None, end: datetime | None) -> bool:
    t = as_dt(ts)
    return (start is None or t >= start) and (end is None or t <= end)


def _matches(message: str, query: str | None) -> bool:
    return not query or all(tok in message.lower() for tok in query.lower().split())


def _component_matches(component: str, wanted: str | None) -> bool:
    return not wanted or component == wanted or component.startswith(wanted + ".")


def _sample_values(series: dict) -> list[tuple[int, float]]:
    return [(int(float(ts)), float(v)) for ts, v in series["values"] if str(v) not in ("NaN", "+Inf", "-Inf")]


def flatten_metric(result: list[dict]) -> list[dict]:
    """Prometheus matrix result -> [{"labels", "values"}], dropping the series-identity labels."""
    out = []
    for s in result:
        labels = {k: v for k, v in s.get("metric", {}).items() if k not in ("__name__", "job", "instance")}
        values = _sample_values(s)
        if values:
            out.append({"labels": labels, "values": values})
    return out


# --- live ---------------------------------------------------------------------------------

class LiveBackend:
    def __init__(self) -> None:
        self.jaeger = os.environ.get("JAEGER_URL", "http://localhost:16686")
        self.prometheus = os.environ.get("PROMETHEUS_URL",
                                         f"http://localhost:{os.environ.get('PROMETHEUS_PORT', '9092')}")
        self.opensearch_url = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
        self.log_index = os.environ.get("LOG_INDEX", "kestrel-logs-*")
        self.service = os.environ.get("TRACE_SERVICE", "kestrel-assistant")
        self._os = None

    # plumbing
    def _get(self, url: str, timeout: int = 20) -> dict:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.load(resp)

    def _pg(self) -> psycopg.Connection:
        """The `tools` role: can read the assistant's tables, cannot see the eval schema."""
        parts = conninfo_to_dict(os.environ.get(
            "DATABASE_URL", "postgresql://kestrel:kestrel@localhost:5432/kestrel"))
        parts.update(user=os.environ.get("TOOLS_DB_USER", "tools"),
                     password=os.environ.get("TOOLS_DB_PASSWORD", "tools"))
        return psycopg.connect(make_conninfo(**parts), autocommit=True)

    def _rows(self, sql: str, params=()) -> list[dict]:
        with self._pg() as conn:
            cur = conn.execute(sql, params)
            cols = [c.name for c in cur.description]
            return [{c: iso(v) if isinstance(v, datetime) else v for c, v in zip(cols, row)}
                    for row in cur.fetchall()]

    def _opensearch(self):
        if self._os is None:
            from opensearchpy import OpenSearch
            self._os = OpenSearch(self.opensearch_url)
        return self._os

    # traces
    def trace_id_for(self, conversation_id: str) -> str:
        if _TRACE_ID.match(conversation_id):
            return conversation_id
        now = datetime.now(timezone.utc)
        params = urllib.parse.urlencode({
            "service": self.service, "limit": 20,
            "start": int((now - timedelta(days=7)).timestamp() * 1e6), "end": int(now.timestamp() * 1e6),
            "tags": json.dumps({"kestrel.conversation_id": conversation_id}),
        })
        found = self._get(f"{self.jaeger}/api/traces?{params}").get("data") or []
        if not found:
            raise LookupError(f"no trace for conversation {conversation_id!r}")
        newest = max(found, key=lambda t: max(s["startTime"] for s in t["spans"]))
        return newest["traceID"]

    def trace(self, trace_id: str) -> dict:
        try:
            data = self._get(f"{self.jaeger}/api/traces/{trace_id}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise LookupError(f"no trace {trace_id!r}") from e
            raise
        if not data.get("data"):
            raise LookupError(f"no trace {trace_id!r}")
        return data

    # logs
    def logs(self, component, query, start, end, limit):
        filters: list[dict] = []
        if component:
            filters.append({"bool": {"should": [{"term": {"component": component}},
                                                {"prefix": {"component": component + "."}}],
                                     "minimum_should_match": 1}})
        if query:
            filters.append({"match": {"message": {"query": query, "operator": "and"}}})
        rng = {}
        if start:
            rng["gte"] = start.isoformat()
        if end:
            rng["lte"] = end.isoformat()
        if rng:
            filters.append({"range": {"timestamp": rng}})
        body = {"size": limit, "sort": [{"timestamp": "asc"}], "track_total_hits": True,
                "query": {"bool": {"filter": filters}} if filters else {"match_all": {}}}
        hits = self._opensearch().search(index=self.log_index, body=body, ignore_unavailable=True)["hits"]
        return [h["_source"] for h in hits["hits"]], hits["total"]["value"]

    def log(self, log_id: str) -> dict | None:
        hits = self._opensearch().search(
            index=self.log_index, body={"size": 1, "query": {"term": {"log_id": log_id}}},
            ignore_unavailable=True)["hits"]["hits"]
        return hits[0]["_source"] if hits else None

    # postgres
    def changes(self, start, end):
        return self._rows(
            """SELECT change_id, kind, target, description, diff, author, created_at FROM changes
               WHERE (%(s)s::timestamptz IS NULL OR created_at >= %(s)s)
                 AND (%(e)s::timestamptz IS NULL OR created_at <= %(e)s)
               ORDER BY created_at DESC, change_id""", {"s": start, "e": end})

    def documents(self, doc_id: str):
        return self._rows(
            """SELECT d.doc_id, d.version, d.title, d.published_at, d.body,
                      count(c.chunk_id) AS chunk_count,
                      count(c.chunk_id) FILTER (WHERE c.indexed) AS indexed_chunks
               FROM documents d LEFT JOIN chunks c USING (doc_id, version)
               WHERE d.doc_id = %s
               GROUP BY d.doc_id, d.version, d.title, d.published_at, d.body
               ORDER BY d.published_at, d.version""", (doc_id,))

    def chunks(self, ids):
        return self._rows(
            """SELECT chunk_id, doc_id, version, chunk_index, text, indexed FROM chunks
               WHERE chunk_id = ANY(%s)""", (list(ids),))

    def prompt_versions(self):
        return self._rows(
            """SELECT p.version, p.template, p.created_at, p.author, p.note,
                      (p.version = (SELECT value FROM app_config WHERE key = 'prompt_version')) AS active
               FROM prompt_versions p ORDER BY p.created_at""")

    # prometheus
    def metric(self, name, start, end):
        end = end or datetime.now(timezone.utc)
        start = start or end - timedelta(hours=6)
        step = max(15, int((end - start).total_seconds() // 300))
        params = urllib.parse.urlencode({"query": name, "start": start.timestamp(),
                                         "end": end.timestamp(), "step": step})
        return flatten_metric(self._get(f"{self.prometheus}/api/v1/query_range?{params}")["data"]["result"])

    def replay(self, replay_id: str) -> dict | None:
        return None  # replays run on recorded cases only

    def metric_names(self):
        names = self._get(f"{self.prometheus}/api/v1/label/__name__/values")["data"]
        return sorted(n for n in names if n.startswith("kestrel_"))


# --- snapshot -----------------------------------------------------------------------------

class SnapshotBackend:
    """Reads <runs_dir>/<case_id>/<name>.json for the names in SNAPSHOT_FILES, nothing else."""

    def __init__(self, case_id: str, runs_dir: Path | str | None = None) -> None:
        if not _CASE_ID.match(case_id):
            raise ValueError(f"bad case id {case_id!r}")
        self.case_id = case_id
        self.root = (Path(runs_dir) if runs_dir else RUNS_DIR).resolve()
        self.dir = (self.root / case_id).resolve()
        if self.dir.parent != self.root or not self.dir.is_dir():
            raise FileNotFoundError(f"no snapshot for {case_id!r} in {self.root}")
        self._cache: dict[str, object] = {}

    def _load(self, name: str):
        if name not in SNAPSHOT_FILES:
            raise PermissionError(f"{name!r} is not a snapshot file")
        if name not in self._cache:
            path = self.dir / f"{name}.json"
            self._cache[name] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        return self._cache[name]

    # traces
    def trace_id_for(self, conversation_id: str) -> str:
        if _TRACE_ID.match(conversation_id):
            return conversation_id
        alert = self._load("alert") or {}
        if alert.get("conversation_id") == conversation_id and alert.get("trace_id"):
            return alert["trace_id"]
        raise LookupError(f"no trace for conversation {conversation_id!r}")

    def trace(self, trace_id: str) -> dict:
        data = self._load("trace") or {"data": []}
        traces = [t for t in data.get("data", []) if t.get("traceID") == trace_id]
        if not traces:
            raise LookupError(f"no trace {trace_id!r}")
        return {"data": traces}

    # logs
    def _all_logs(self) -> list[dict]:
        return sorted(self._load("logs") or [], key=lambda r: r["timestamp"])

    def logs(self, component, query, start, end, limit):
        hits = [r for r in self._all_logs()
                if _component_matches(r.get("component", ""), component)
                and _matches(r.get("message", ""), query)
                and _in_window(r["timestamp"], start, end)]
        return hits[:limit], len(hits)

    def log(self, log_id: str) -> dict | None:
        return next((r for r in self._all_logs() if r.get("log_id") == log_id), None)

    # database state
    def changes(self, start, end):
        rows = [r for r in (self._load("changes") or []) if _in_window(r["created_at"], start, end)]
        return sorted(rows, key=lambda r: (as_dt(r["created_at"]), r["change_id"]), reverse=True)

    def documents(self, doc_id: str):
        rows = [r for r in (self._load("documents") or []) if r["doc_id"] == doc_id]
        return sorted(rows, key=lambda r: (as_dt(r["published_at"]), r["version"]))

    def chunks(self, ids):
        wanted = set(ids)
        return [r for r in (self._load("chunks") or []) if r["chunk_id"] in wanted]

    def prompt_versions(self):
        return list(self._load("prompt_versions") or [])

    # metrics
    def metric(self, name, start, end):
        result = (self._load("metrics") or {}).get(name) or []
        out = []
        for series in flatten_metric(result):
            values = [(ts, v) for ts, v in series["values"]
                      if (start is None or ts >= int(start.timestamp()))
                      and (end is None or ts <= int(end.timestamp()))]
            if values:
                out.append({"labels": series["labels"], "values": values})
        return out

    def metric_names(self):
        return sorted(n for n, r in (self._load("metrics") or {}).items() if r)

    # replays (Day 6)
    def replay(self, replay_id: str) -> dict | None:
        from tools.replay import log_path, read_log
        return next((r for r in read_log(log_path(self.case_id)) if r["replay_id"] == replay_id), None)


def get_backend(spec: str | None = None, runs_dir: Path | str | None = None) -> Backend:
    spec = (spec or os.environ.get("TRACEWISE_BACKEND") or "live").strip()
    if spec == "live":
        return LiveBackend()
    kind, _, case_id = spec.partition(":")
    if kind == "snapshot" and case_id:
        return SnapshotBackend(case_id, runs_dir or os.environ.get("TRACEWISE_RUNS_DIR"))
    raise ValueError(f"TRACEWISE_BACKEND must be 'live' or 'snapshot:<case_id>', got {spec!r}")
