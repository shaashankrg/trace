"""Chunk, embed and load assistant/docs/ into documents + chunks.

Every version is stored; only the newest version of each doc is indexed.
Chunk size and embedding model come from app_config.

Usage: python -m assistant.ingest
"""
from datetime import date
from pathlib import Path

import psycopg

from assistant import db
from assistant.config import load_config
from assistant.retrieve import embed

DOCS_DIR = Path(__file__).parent / "docs"


def parse(path: Path) -> tuple[dict, str]:
    """Split simple `key: value` front matter from the body."""
    text = path.read_text(encoding="utf-8")
    _, front, body = text.split("---", 2)
    meta = {}
    for line in front.strip().splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta, body.strip()


def chunk(text: str, size: int) -> list[str]:
    """Fixed-size chunks of at most `size` chars, broken on word boundaries."""
    chunks, current = [], ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > size:
            chunks.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        chunks.append(current)
    return chunks


def latest_versions(docs: list[tuple[dict, str]]) -> set[tuple[str, str]]:
    newest: dict[str, tuple[date, str]] = {}
    for meta, _ in docs:
        key = (date.fromisoformat(meta["published_at"]), meta["version"])
        if meta["doc_id"] not in newest or key > newest[meta["doc_id"]]:
            newest[meta["doc_id"]] = key
    return {(doc_id, version) for doc_id, (_, version) in newest.items()}


def ingest_all(conn: psycopg.Connection, docs_dir: Path = DOCS_DIR) -> int:
    cfg = load_config(conn)
    docs = [parse(p) for p in sorted(docs_dir.glob("*.md"))]
    indexed = latest_versions(docs)

    rows = []
    for meta, body in docs:
        pieces = chunk(body, cfg.chunk_size)
        vectors = embed(pieces, cfg.embedding_model)
        is_indexed = (meta["doc_id"], meta["version"]) in indexed
        for i, (piece, vec) in enumerate(zip(pieces, vectors)):
            rows.append((db.chunk_id(meta["doc_id"], meta["version"], i),
                         meta["doc_id"], meta["version"], i, piece, vec, is_indexed))

    # Full rebuild, so renamed or removed docs don't linger.
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM chunks")
        cur.execute("DELETE FROM documents")
        cur.executemany(
            "INSERT INTO documents (doc_id, version, title, body, published_at) VALUES (%s, %s, %s, %s, %s)",
            [(m["doc_id"], m["version"], m.get("title"), body, m["published_at"]) for m, body in docs],
        )
        cur.executemany(
            """
            INSERT INTO chunks (chunk_id, doc_id, version, chunk_index, text, embedding, indexed)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )
    return len(docs)


def main() -> None:
    with db.connect() as conn:
        n = ingest_all(conn)
        n_chunks, n_indexed = conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE indexed) FROM chunks"
        ).fetchone()
        n_docs = conn.execute("SELECT count(DISTINCT doc_id) FROM documents").fetchone()[0]
        print(f"{n} doc versions ({n_docs} docs), {n_chunks} chunks, {n_indexed} indexed")


if __name__ == "__main__":
    main()
