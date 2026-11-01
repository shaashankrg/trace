"""Sweep chunk sizes to find where chunking_regression actually breaks retrieval.

A tiny chunk size dilutes each chunk's signal; a large one buries the key sentence in a blob
that matches the question less well than some other doc's focused chunk. Sweep both ways.
"""
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, r"C:\Users\shash\trace")
from dotenv import load_dotenv

load_dotenv(r"C:\Users\shash\trace\.env")

from assistant import db
from assistant.config import load_config
from assistant.retrieve import retrieve
from injector import faults, state

NOW = datetime.now(timezone.utc)
SIZES = [40, 60, 80, 120, 1000, 2000]

QUESTIONS = [
    ("What's the refund window for returning an item?", r"14 days", "refund-policy"),
    ("How much does express shipping cost?", r"\$24\.95", "shipping-expedited"),
    ("How long is the warranty on Kestrel-brand gear?", r"2[- ]year", "warranty"),
    ("How long will you hold my store pickup order?", r"7 days", "store-pickup"),
    ("Is there a military discount?", r"15\s?%|15 percent", "military-discount"),
    ("Do Kestrel gift cards expire?", r"never expire|do not expire", "gift-cards"),
    ("How much is the deposit to rent a tent?", r"\$100", "rentals"),
    ("How long is a password reset link valid?", r"30[- ]min", "password-reset"),
    ("What's the minimum order for free standard shipping?", r"\$75", "shipping-domestic"),
    ("What's the price match window?", r"\d+ days", "price-matching"),
    ("What discount do students get?", r"\d+\s?%", "student-discount"),
    ("What are the loyalty tier thresholds?", r"\$\d+", "loyalty-tiers"),
]


def retrieved(conn, question):
    cfg = load_config(conn)
    return [c.chunk_id for c in retrieve(conn, question, cfg.top_k, cfg.similarity_threshold,
                                         cfg.embedding_model)]


def answer_present(conn, chunk_ids, pattern):
    if not chunk_ids:
        return False
    rows = conn.execute("SELECT text FROM chunks WHERE chunk_id = ANY(%s)", (list(chunk_ids),)).fetchall()
    return any(re.search(pattern, t, re.IGNORECASE) for (t,) in rows)


def main():
    with db.connect() as conn:
        state.reset_healthy(conn)
        base = {}
        for q, pat, doc in QUESTIONS:
            r = retrieved(conn, q)
            base[q] = (r, answer_present(conn, r, pat))

        hits = []
        for size in SIZES:
            print(f"\n===== chunk_size {size} =====")
            for q, pat, doc in QUESTIONS:
                if not base[q][1]:
                    continue
                f = faults.build(conn, {"type": "chunking_regression",
                                        "params": {"doc_id": doc, "chunk_size": size},
                                        "age_min": 30, "change_id": "change:p", "description": "p"}, NOW)
                f.apply()
                bad = retrieved(conn, q)
                ok = answer_present(conn, bad, pat)
                f.revert()
                if not ok:
                    hits.append((size, doc, q))
                    print(f"  BREAKS [{doc}] {q}")
                    print(f"         healthy={base[q][0]}")
                    print(f"         faulted={bad}")
            state.reset_healthy(conn)

        print("\n\n===== SUMMARY: breaking (size, doc, question) =====")
        for h in hits:
            print(" ", h)


if __name__ == "__main__":
    main()
