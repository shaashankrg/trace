"""Structured JSON logs, batched into OpenSearch (kestrel-logs-YYYY.MM.DD)."""
import contextvars
import logging
import os
import queue
import sys
import threading
import uuid
from datetime import datetime, timezone

from opensearchpy import OpenSearch, helpers
from opentelemetry import trace

OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
INDEX_PREFIX = "kestrel-logs"

# Set per request so every log line carries the conversation it belongs to.
conversation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "conversation_id", default=None
)

INDEX_TEMPLATE = {
    "index_patterns": [f"{INDEX_PREFIX}-*"],
    "template": {
        "mappings": {
            "properties": {
                "log_id": {"type": "keyword"},
                "timestamp": {"type": "date"},
                "level": {"type": "keyword"},
                "component": {"type": "keyword"},
                "trace_id": {"type": "keyword"},
                "span_id": {"type": "keyword"},
                "conversation_id": {"type": "keyword"},
                "message": {"type": "text"},
            }
        }
    },
}


class OpenSearchHandler(logging.Handler):
    """Queues records on emit and bulk-indexes them from a background thread,
    so a request never waits on OpenSearch."""

    def __init__(self, client: OpenSearch, batch_size: int = 200, flush_interval_s: float = 1.0):
        super().__init__()
        self.client = client
        self.batch_size = batch_size
        self.flush_interval_s = flush_interval_s
        self.queue: queue.Queue[dict] = queue.Queue(maxsize=10_000)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="opensearch-logs", daemon=True)
        self._thread.start()

    def to_doc(self, record: logging.LogRecord) -> dict:
        # Read the span context here, on the emitting thread, not later in the flusher.
        ctx = trace.get_current_span().get_span_context()
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return {
            "log_id": str(uuid.uuid4()),
            "timestamp": ts.isoformat(timespec="milliseconds"),
            "level": "WARN" if record.levelname == "WARNING" else record.levelname,
            "component": getattr(record, "component", record.name),
            "trace_id": format(ctx.trace_id, "032x") if ctx.is_valid else None,
            "span_id": format(ctx.span_id, "016x") if ctx.is_valid else None,
            "conversation_id": conversation_id_var.get(),
            "message": record.getMessage(),
        }

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(self.to_doc(record))
        except queue.Full:
            pass  # drop rather than block the request
        except Exception:
            self.handleError(record)

    def _drain(self) -> list[dict]:
        batch = []
        while len(batch) < self.batch_size:
            try:
                batch.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _index(self, batch: list[dict]) -> None:
        actions = [
            {"_index": f"{INDEX_PREFIX}-{doc['timestamp'][:10].replace('-', '.')}",
             "_id": doc["log_id"], "_source": doc}
            for doc in batch
        ]
        try:
            helpers.bulk(self.client, actions)
        except Exception as e:  # never log from here: it would recurse
            print(f"opensearch log indexing failed: {e}", file=sys.stderr)

    def _run(self) -> None:
        while not self._stop.wait(self.flush_interval_s):
            self.flush()

    def flush(self) -> None:
        while batch := self._drain():
            self._index(batch)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.flush()
        super().close()


def setup_logging() -> OpenSearchHandler | None:
    logger = logging.getLogger("kestrel")
    logger.setLevel(logging.INFO)
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(stream)

    client = OpenSearch(OPENSEARCH_URL)
    try:
        client.indices.put_index_template(name=INDEX_PREFIX, body=INDEX_TEMPLATE)
    except Exception as e:
        logger.warning("OpenSearch unavailable, logs stay local: %s", e)
        return None
    handler = OpenSearchHandler(client)
    logger.addHandler(handler)
    return handler
