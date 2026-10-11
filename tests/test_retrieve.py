"""Requires the compose stack running and `python -m assistant.ingest` done."""
import psycopg
import pytest

from assistant import db
from assistant.config import load_config
from assistant.retrieve import retrieve


@pytest.fixture(scope="module")
def conn():
    try:
        c = db.connect()
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres not reachable: {e}")
    yield c
    c.close()


def test_refund_question_hits_refund_policy_v2(conn):
    cfg = load_config(conn)
    chunks = retrieve(conn, "How long do I have to return boots?", cfg.top_k, cfg.similarity_threshold)
    assert chunks, "retrieval returned no chunks"
    assert chunks[0].chunk_id.startswith("refund-policy@v2")
