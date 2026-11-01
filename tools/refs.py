"""Typed references to evidence, defined once.

  span    span:<trace_id>/<span_id>
  chunk   chunk:<doc_id>@<version>#<n>[/c<size>]  (the suffix names a non-default chunking)
  doc     doc:<doc_id>@<version>
  log     log:<log_id>
  change  change:<id>                         (the change table's own ids already start with `change:`)
  prompt  prompt:<version>
  metric  metric:<name>@<iso_ts>
  replay  replay:<replay_id>                  (Day 6: a line in the replay log)

Every factual item a tool returns carries one. The Day 6 evidence verifier calls `resolve_ref`
on each ref a diagnosis cites, so a ref is only as good as the lookup behind it.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timezone

KINDS = ("span", "chunk", "doc", "log", "change", "prompt", "metric", "replay")


class RefError(ValueError):
    """The ref is malformed, or well-formed but nothing in the backend matches it."""


@dataclass(frozen=True)
class Ref:
    kind: str
    parts: tuple[str, ...]

    def __str__(self) -> str:
        return format_ref(self)


# --- builders -----------------------------------------------------------------------------

def span_ref(trace_id: str, span_id: str) -> str:
    return f"span:{trace_id}/{span_id}"


def chunk_ref(chunk_id: str) -> str:
    """`chunk_id` is the chunks table key, e.g. 'refund-policy@v2#3' or 'refund-policy@v2#3/c80'."""
    return f"chunk:{chunk_id}"


def chunk_key(doc_id: str, version: str, index: int | str, size: int | str | None = None) -> str:
    """The chunks table key. `size` names a non-default chunking and is omitted for the default one."""
    base = f"{doc_id}@{version}#{index}"
    return base if size in (None, "") else f"{base}/c{size}"


def doc_ref(doc_id: str, version: str) -> str:
    return f"doc:{doc_id}@{version}"


def log_ref(log_id: str) -> str:
    return f"log:{log_id}"


def change_ref(change_id: str) -> str:
    return change_id if change_id.startswith("change:") else f"change:{change_id}"


def prompt_ref(version: str) -> str:
    return f"prompt:{version}"


def metric_ref(name: str, ts: int | float | datetime) -> str:
    return f"metric:{name}@{format_ts(ts)}"


def format_ts(ts: int | float | datetime) -> str:
    """UTC, second resolution; whole minutes drop the seconds (2026-10-14T03:00Z)."""
    dt = ts if isinstance(ts, datetime) else datetime.fromtimestamp(ts, tz=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%MZ" if dt.second == 0 else "%Y-%m-%dT%H:%M:%SZ")


def parse_ts(text: str) -> datetime:
    """ISO 8601, with a trailing Z or an offset. Naive input is taken as UTC."""
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError as e:
        raise RefError(f"not an ISO timestamp: {text!r}") from e
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --- parsing ------------------------------------------------------------------------------

_SPAN = re.compile(r"^([0-9a-f]{32})/([0-9a-f]{16})$")
_CHUNK = re.compile(r"^([^@#\s]+)@([^@#\s]+)#(\d+)(?:/c(\d+))?$")
_DOC = re.compile(r"^([^@#\s]+)@([^@#\s]+)$")
_METRIC = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)@(.+)$")


def parse_ref(ref: str) -> Ref:
    """Split a ref into its kind and parts, validating the shape. Does not touch a backend.

    span   -> (trace_id, span_id)
    chunk  -> (doc_id, version, n) or (doc_id, version, n, chunk_size)
    doc    -> (doc_id, version)
    log    -> (log_id,)
    change -> (change_id,)          the full 'change:...' id
    prompt -> (version,)
    metric -> (name, iso_ts)        iso_ts normalized by format_ts
    """
    if not isinstance(ref, str) or ":" not in ref:
        raise RefError(f"not a ref: {ref!r}")
    kind, _, rest = ref.partition(":")
    rest = rest.strip()
    if kind not in KINDS or not rest:
        raise RefError(f"unknown ref kind in {ref!r}; expected one of {', '.join(KINDS)}")

    if kind == "span":
        m = _SPAN.match(rest)
        if not m:
            raise RefError(f"bad span ref {ref!r}; expected span:<trace_id>/<span_id>")
        return Ref(kind, m.groups())
    if kind == "chunk":
        m = _CHUNK.match(rest)
        if not m:
            raise RefError(f"bad chunk ref {ref!r}; expected chunk:<doc_id>@<version>#<n>[/c<size>]")
        doc_id, version, n, size = m.groups()
        return Ref(kind, (doc_id, version, n) if size is None else (doc_id, version, n, size))
    if kind == "doc":
        m = _DOC.match(rest)
        if not m:
            raise RefError(f"bad doc ref {ref!r}; expected doc:<doc_id>@<version>")
        return Ref(kind, m.groups())
    if kind == "metric":
        m = _METRIC.match(rest)
        if not m:
            raise RefError(f"bad metric ref {ref!r}; expected metric:<name>@<iso_ts>")
        return Ref(kind, (m.group(1), format_ts(parse_ts(m.group(2)))))
    if kind == "change":
        return Ref(kind, (change_ref(rest),))
    return Ref(kind, (rest,))  # log, prompt, replay


def format_ref(ref: Ref) -> str:
    kind, p = ref.kind, ref.parts
    if kind == "span":
        return span_ref(*p)
    if kind == "chunk":
        return chunk_ref(chunk_key(*p))
    if kind == "doc":
        return doc_ref(*p)
    if kind == "log":
        return log_ref(p[0])
    if kind == "change":
        return change_ref(p[0])
    if kind == "prompt":
        return prompt_ref(p[0])
    if kind == "replay":
        return f"replay:{p[0]}"
    return f"metric:{p[0]}@{p[1]}"


# --- resolving ----------------------------------------------------------------------------

def resolve_ref(backend, ref: str) -> dict:
    """Look the ref up in `backend` and return the thing it names, or raise RefError.

    The result is {"ref", "kind", "record"}, where `record` is the thing as the
    backend holds it. This is what the evidence verifier uses to check that a cited ref exists.
    """
    parsed = parse_ref(ref)
    kind, p = parsed.kind, parsed.parts
    canonical = format_ref(parsed)

    def found(record: dict | None, what: str) -> dict:
        if record is None:
            raise RefError(f"{canonical} does not resolve: no such {what}")
        return {"ref": canonical, "kind": kind, "record": record}

    if kind == "span":
        try:
            raw = backend.trace(p[0])
        except LookupError:
            raw = None
        span = next((s for t in (raw or {}).get("data", []) for s in t.get("spans", [])
                     if s["spanID"] == p[1]), None)
        return found(None if span is None else {"trace_id": p[0], "span_id": p[1],
                                                "name": span["operationName"]}, "span")
    if kind == "chunk":
        rows = backend.chunks([chunk_key(*p)])
        return found(rows[0] if rows else None, "chunk")
    if kind == "doc":
        row = next((d for d in backend.documents(p[0]) if d["version"] == p[1]), None)
        return found(None if row is None else {"doc_id": row["doc_id"], "version": row["version"],
                                               "published_at": _iso(row["published_at"])}, "document version")
    if kind == "log":
        return found(backend.log(p[0]), "log line")
    if kind == "change":
        row = next((c for c in backend.changes(None, None) if c["change_id"] == p[0]), None)
        return found(row, "change")
    if kind == "prompt":
        row = next((v for v in backend.prompt_versions() if v["version"] == p[0]), None)
        return found(None if row is None else {"version": row["version"]}, "prompt version")
    if kind == "replay":
        rec = backend.replay(p[0]) if hasattr(backend, "replay") else None
        return found(None if rec is None else {k: rec[k] for k in ("replay_id", "patch", "target_component",
                                                                   "matches_correction", "judge")}, "replay")

    name, iso = p
    at = parse_ts(iso)
    want = int(at.timestamp())
    for series in backend.metric(name, at, at):
        for ts, value in series["values"]:
            if int(ts) == want:
                return found({"metric": name, "ts": iso, "value": value, "labels": series["labels"]},
                             "metric sample")
    return found(None, "metric sample")


def _iso(value) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)
