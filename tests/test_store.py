"""Needs the compose stack up and `python -m assistant.bootstrap` run."""
import psycopg
import pytest

from assistant import db, tools
from assistant.config import load_config


@pytest.fixture()
def conn():
    try:
        c = db.connect()
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres not reachable: {e}")
    yield c
    c.close()


def test_only_latest_version_is_indexed(conn):
    rows = conn.execute(
        "SELECT doc_id, count(DISTINCT version) FROM chunks GROUP BY doc_id HAVING count(DISTINCT version) > 1"
    ).fetchall()
    assert len(rows) == 8
    for doc_id, _ in rows:
        versions = dict(conn.execute(
            "SELECT version, bool_or(indexed) FROM chunks WHERE doc_id = %s GROUP BY version", (doc_id,)
        ).fetchall())
        assert versions == {"v1": False, "v2": True}, doc_id


def test_config_is_read_on_every_request(conn):
    before = load_config(conn).top_k
    try:
        conn.execute("UPDATE app_config SET value = '9' WHERE key = 'top_k'")
        assert load_config(conn).top_k == 9
    finally:
        conn.execute("UPDATE app_config SET value = %s WHERE key = 'top_k'", (str(before),))


def test_tool_returns_orders_and_records_the_call(conn):
    conn.execute("DELETE FROM tool_calls WHERE conversation_id = 'test-conv'")
    rows = tools.execute_tool(conn, "lookup_order", {"customer_id": "C-0001"}, 2000, "test-conv")
    assert rows and all("order_id" in r for r in rows)
    n = conn.execute("SELECT count(*) FROM tool_calls WHERE conversation_id = 'test-conv'").fetchone()[0]
    assert n == 1


def test_tool_timeout_returns_empty_list_and_leaves_span_ok(conn, monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import StatusCode

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tools, "tracer", provider.get_tracer("test"))
    monkeypatch.setitem(tools.TOOLS, "lookup_order", lambda c, t, **kw: tools._query(c, 50, "SELECT pg_sleep(1)", ()))

    conn.execute("DELETE FROM tool_calls WHERE conversation_id = 'test-timeout'")
    rows = tools.execute_tool(conn, "lookup_order", {"customer_id": "C-0001"}, 50, "test-timeout")

    assert rows == []
    span = exporter.get_finished_spans()[0]
    assert span.name == "tool.lookup_order"
    assert span.status.status_code != StatusCode.ERROR
    assert span.attributes["kestrel.tool.result_count"] == 0
    timed_out = conn.execute(
        "SELECT timed_out FROM tool_calls WHERE conversation_id = 'test-timeout'"
    ).fetchone()[0]
    assert timed_out is True
