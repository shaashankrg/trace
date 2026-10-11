"""Chunk, embed and upsert the docs in assistant/docs/.

Usage: python -m assistant.ingest
"""
import os
from pathlib import Path

from assistant import db
from assistant.retrieve import embed

DOCS_DIR = Path(__file__).parent / "docs"
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "300"))


def parse(path: Path) -> tuple[dict, str]:
    """Split simple `key: value` front matter from the body."""
    text = path.read_text(encoding="utf-8")
    _, front, body = text.split("---", 2)
    meta = {}
    for line in front.strip().splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta, body.strip()


def chunk(text: str, size: int = CHUNK_SIZE) -> list[str]:
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


def ingest(conn, path: Path) -> list[str]:
    meta, body = parse(path)
    doc_id, version = meta["doc_id"], meta["version"]
    pieces = chunk(body)
    vectors = embed(pieces)

    with conn.transaction():
        conn.execute(
            """
            INSERT INTO documents (doc_id, version, title, body, published_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (doc_id, version) DO UPDATE
              SET title = EXCLUDED.title, body = EXCLUDED.body,
                  published_at = EXCLUDED.published_at
            """,
            (doc_id, version, meta.get("title"), body, meta.get("published_at")),
        )
        # Re-ingest replaces this version's chunks, since the count changes with CHUNK_SIZE.
        conn.execute(
            "DELETE FROM chunks WHERE doc_id = %s AND version = %s", (doc_id, version)
        )
        ids = []
        for i, (piece, vec) in enumerate(zip(pieces, vectors)):
            cid = db.chunk_id(doc_id, version, i)
            conn.execute(
                """
                INSERT INTO chunks (chunk_id, doc_id, version, chunk_index, text, embedding)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (cid, doc_id, version, i, piece, vec),
            )
            ids.append(cid)
    return ids


def main() -> None:
    with db.connect() as conn:
        db.init_schema(conn)
        for path in sorted(DOCS_DIR.glob("*.md")):
            ids = ingest(conn, path)
            print(f"{path.name}: {len(ids)} chunks ({ids[0]} .. {ids[-1]})")


if __name__ == "__main__":
    main()
