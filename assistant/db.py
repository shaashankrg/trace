"""Postgres connection and schema."""
import os

import psycopg
from dotenv import load_dotenv
from pgvector.psycopg import register_vector

load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://kestrel:kestrel@localhost:5432/kestrel"
)
EMBEDDING_DIM = 384  # all-MiniLM-L6-v2

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS documents (
  doc_id       TEXT,
  version      TEXT,
  title        TEXT,
  body         TEXT,
  published_at TIMESTAMPTZ,
  PRIMARY KEY (doc_id, version)
);

CREATE TABLE IF NOT EXISTS chunks (
  chunk_id    TEXT PRIMARY KEY,          -- 'refund-policy@v2#0'
  doc_id      TEXT,
  version     TEXT,
  chunk_index INT,
  text        TEXT,
  embedding   vector({EMBEDDING_DIM}),
  indexed     BOOLEAN DEFAULT TRUE,
  FOREIGN KEY (doc_id, version) REFERENCES documents
);

-- Read on every request so faults can be injected by changing a row.
CREATE TABLE IF NOT EXISTS app_config (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS prompt_versions (
  version    TEXT PRIMARY KEY,
  template   TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  author     TEXT,
  note       TEXT
);

CREATE TABLE IF NOT EXISTS customers (
  customer_id TEXT PRIMARY KEY,
  name        TEXT,
  email       TEXT
);

CREATE TABLE IF NOT EXISTS products (
  product_id TEXT PRIMARY KEY,
  name       TEXT,
  price      NUMERIC(10, 2)
);

CREATE TABLE IF NOT EXISTS orders (
  order_id     TEXT PRIMARY KEY,
  customer_id  TEXT REFERENCES customers,
  placed_at    TIMESTAMPTZ,
  delivered_at TIMESTAMPTZ,
  status       TEXT,
  total        NUMERIC(10, 2)
);

CREATE TABLE IF NOT EXISTS order_items (
  order_id   TEXT REFERENCES orders,
  product_id TEXT REFERENCES products,
  qty        INT,
  PRIMARY KEY (order_id, product_id)
);

-- Ground truth stock levels.
CREATE TABLE IF NOT EXISTS inventory_source (
  product_id TEXT PRIMARY KEY REFERENCES products,
  qty        INT,
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- What check_inventory reads.
CREATE TABLE IF NOT EXISTS inventory_cache (
  product_id   TEXT PRIMARY KEY REFERENCES products,
  qty          INT,
  refreshed_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS refunds (
  refund_id  TEXT PRIMARY KEY,
  order_id   TEXT REFERENCES orders,
  status     TEXT,
  updated_at TIMESTAMPTZ
);

-- Change history the investigator can read. Real changes and decoys share this
-- format; nothing marks which is which, and outages leave no row.
CREATE TABLE IF NOT EXISTS changes (
  change_id   TEXT PRIMARY KEY,   -- 'change:doc-update-2211'
  kind        TEXT,               -- prompt_edit | doc_update | doc_delete | config_change | model_change
  target      TEXT,               -- 'prompt', 'doc:refund-policy', 'config:top_k'
  description TEXT,
  diff        TEXT,
  author      TEXT,
  created_at  TIMESTAMPTZ
);

-- Every tool input and output, for replay.
CREATE TABLE IF NOT EXISTS tool_calls (
  id              BIGSERIAL PRIMARY KEY,
  conversation_id TEXT,
  trace_id        TEXT,
  span_id         TEXT,
  tool_name       TEXT,
  input           JSONB,
  output          JSONB,
  timed_out       BOOLEAN,
  latency_ms      DOUBLE PRECISION,
  created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tool_calls_conversation_idx ON tool_calls (conversation_id);
"""


def connect() -> psycopg.Connection:
    conn = psycopg.connect(DATABASE_URL, autocommit=True)
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    register_vector(conn)
    return conn


def init_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA)


def chunk_id(doc_id: str, version: str, index: int) -> str:
    return f"{doc_id}@{version}#{index}"
