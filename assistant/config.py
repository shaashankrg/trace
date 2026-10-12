"""Runtime config from app_config, read on every request."""
import json
import os
from dataclasses import asdict, dataclass

import psycopg

DEFAULTS = {
    "top_k": "4",
    "similarity_threshold": "0.3",
    "chunk_size": "300",
    "model": os.environ.get("LLM_MODEL") or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
    "prompt_version": "v1",
    "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
    "guardrail_blocked_topics": json.dumps(
        ["lawsuit", "legal advice", "medical advice", "weapon", "firearm",
         "social security number", "hack"]
    ),
    "max_response_tokens": "500",
    "tool_timeout_ms": "2000",
}


@dataclass(frozen=True)
class Config:
    top_k: int
    similarity_threshold: float
    chunk_size: int
    model: str
    prompt_version: str
    embedding_model: str
    guardrail_blocked_topics: list[str]
    max_response_tokens: int
    tool_timeout_ms: int

    def span_attributes(self) -> dict:
        return {f"kestrel.config.{k}": v for k, v in asdict(self).items()}


def seed_config(conn: psycopg.Connection) -> None:
    """Insert defaults for keys that are missing; never overwrite existing values."""
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO app_config (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
            list(DEFAULTS.items()),
        )


def reset_config(conn: psycopg.Connection) -> None:
    """Put every key back to its default (used to undo injected faults)."""
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO app_config (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            list(DEFAULTS.items()),
        )


def load_config(conn: psycopg.Connection) -> Config:
    raw = dict(DEFAULTS)
    raw.update(dict(conn.execute("SELECT key, value FROM app_config").fetchall()))
    return Config(
        top_k=int(raw["top_k"]),
        similarity_threshold=float(raw["similarity_threshold"]),
        chunk_size=int(raw["chunk_size"]),
        model=raw["model"],
        prompt_version=raw["prompt_version"],
        embedding_model=raw["embedding_model"],
        guardrail_blocked_topics=json.loads(raw["guardrail_blocked_topics"]),
        max_response_tokens=int(raw["max_response_tokens"]),
        tool_timeout_ms=int(raw["tool_timeout_ms"]),
    )
