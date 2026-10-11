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
