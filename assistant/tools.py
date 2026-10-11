"""Store tools exposed to Claude, each traced as a `tool.<name>` span."""
import json
import logging
import time
from datetime import datetime
from decimal import Decimal

import psycopg
from opentelemetry import trace
from psycopg.types.json import Jsonb

tracer = trace.get_tracer("kestrel.assistant")
log = logging.getLogger("kestrel.tools")

TOOL_DEFINITIONS = [
    {
        "name": "lookup_order",
        "description": (
            "List a customer's orders, newest first, with order ID, placed and delivered "
            "dates, status, total, and the products in each order."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "Customer ID, e.g. C-0001"}
            },
            "required": ["customer_id"],
        },
    },
    {
        "name": "refund_status",
        "description": "Get the refund records (ID, status, last update) for one order.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "Order ID, e.g. O-1001"}
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "check_inventory",
        "description": "Get the current stock quantity for one product.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string", "description": "Product ID, e.g. P-001"}
            },
            "required": ["product_id"],
        },
    },
]


def _query(conn: psycopg.Connection, timeout_ms: int, sql: str, params: tuple) -> list[dict]:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
        cur.execute(sql, params)
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def lookup_order(conn, timeout_ms: int, customer_id: str) -> tuple[list[dict], dict]:
    rows = _query(
        conn,
        timeout_ms,
        """
        SELECT o.order_id, o.placed_at, o.delivered_at, o.status, o.total,
               json_agg(json_build_object('product_id', p.product_id, 'name', p.name, 'qty', oi.qty)
                        ORDER BY p.product_id) AS items
        FROM orders o
        JOIN order_items oi USING (order_id)
        JOIN products p USING (product_id)
        WHERE o.customer_id = %s
        GROUP BY o.order_id
        ORDER BY o.placed_at DESC
        """,
        (customer_id,),
    )
    return rows, {}


def refund_status(conn, timeout_ms: int, order_id: str) -> tuple[list[dict], dict]:
    rows = _query(
        conn,
        timeout_ms,
        "SELECT refund_id, order_id, status, updated_at FROM refunds WHERE order_id = %s",
        (order_id,),
    )
    return rows, {}


def check_inventory(conn, timeout_ms: int, product_id: str) -> tuple[list[dict], dict]:
    rows = _query(
        conn,
        timeout_ms,
        """
        SELECT c.product_id, p.name, c.qty, c.refreshed_at,
               extract(epoch FROM now() - c.refreshed_at)::float AS cache_age_s
        FROM inventory_cache c JOIN products p USING (product_id)
        WHERE c.product_id = %s
        """,
        (product_id,),
    )
    extra = {"kestrel.tool.cache_age_s": rows[0].pop("cache_age_s")} if rows else {}
    return rows, extra


TOOLS = {
    "lookup_order": lookup_order,
    "refund_status": refund_status,
    "check_inventory": check_inventory,
}


def _jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def to_json(rows: list[dict]) -> str:
    return json.dumps(rows, default=_jsonable)


def execute_tool(
    conn: psycopg.Connection,
    name: str,
    tool_input: dict,
    timeout_ms: int,
    conversation_id: str,
) -> list[dict]:
    with tracer.start_as_current_span(f"tool.{name}") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", name)

        start = time.perf_counter()
        timed_out = False
        try:
            rows, extra = TOOLS[name](conn, timeout_ms, **tool_input)
        except psycopg.errors.QueryCanceled:
            # Deliberate flaw (see DECISIONS.md): a timeout is logged and swallowed. The model
            # gets [] and the span stays OK, so the failure is silent unless you look at
            # result_count, latency or the WARN log.
            log.warning(
                "%s timed out after %dms; returning empty result",
                name, timeout_ms, extra={"component": f"tool.{name}"},
            )
            rows, extra, timed_out = [], {}, True
        latency_ms = (time.perf_counter() - start) * 1000

        span.set_attribute("kestrel.tool.result_count", len(rows))
        span.set_attribute("kestrel.tool.latency_ms", round(latency_ms, 1))
        span.set_attributes(extra)

        ctx = span.get_span_context()
        conn.execute(
            """
            INSERT INTO tool_calls
              (conversation_id, trace_id, span_id, tool_name, input, output, timed_out, latency_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                conversation_id, format(ctx.trace_id, "032x"), format(ctx.span_id, "016x"),
                name, Jsonb(tool_input), Jsonb(json.loads(to_json(rows))), timed_out, latency_ms,
            ),
        )
        return rows
