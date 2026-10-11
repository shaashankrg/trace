"""pgvector top-k retrieval, traced as the `retrieve` span."""
import os
from dataclasses import dataclass
from functools import lru_cache

import psycopg
from opentelemetry import trace
from sentence_transformers import SentenceTransformer

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
TOP_K = int(os.environ.get("RETRIEVAL_TOP_K", "4"))
THRESHOLD = float(os.environ.get("RETRIEVAL_THRESHOLD", "0.2"))

tracer = trace.get_tracer("kestrel.assistant")


@dataclass
class Chunk:
    chunk_id: str
    text: str
    score: float


@lru_cache(maxsize=1)
def _model() -> SentenceTransformer:
    return SentenceTransformer(EMBEDDING_MODEL)


def embed(texts: list[str]):
    return _model().encode(texts, normalize_embeddings=True)


def retrieve(
    conn: psycopg.Connection,
    question: str,
    top_k: int = TOP_K,
    threshold: float = THRESHOLD,
    conversation_id: str | None = None,
) -> list[Chunk]:
    # start_as_current_span records exceptions and sets ERROR status by default.
    with tracer.start_as_current_span("retrieve") as span:
        if conversation_id:
            span.set_attribute("kestrel.conversation_id", conversation_id)
        span.set_attribute("kestrel.retrieval.top_k", top_k)
        span.set_attribute("kestrel.retrieval.threshold", threshold)
        span.set_attribute("kestrel.embedding.model", EMBEDDING_MODEL)

        vec = embed([question])[0]
        # <=> is cosine distance; embeddings are normalized, so 1 - distance is cosine similarity.
        rows = conn.execute(
            """
            SELECT chunk_id, text, 1 - (embedding <=> %(v)s) AS score
            FROM chunks
            WHERE indexed
            ORDER BY embedding <=> %(v)s
            LIMIT %(k)s
            """,
            {"v": vec, "k": top_k},
        ).fetchall()
        chunks = [Chunk(cid, text, float(score)) for cid, text, score in rows if score >= threshold]

        span.set_attribute("kestrel.retrieval.result_count", len(chunks))
        span.set_attribute("kestrel.retrieval.chunk_ids", [c.chunk_id for c in chunks])
        span.set_attribute("kestrel.retrieval.scores", [round(c.score, 4) for c in chunks])
        return chunks
