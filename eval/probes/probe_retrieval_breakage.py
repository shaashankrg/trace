"""Find questions that chunking_regression / embedding_mismatch actually break.

Strategy: retrieval-only, no LLM. For every canary-style question, compare the retrieved
chunk set healthy vs faulted, and report the ones where the chunk holding the answer fact
drops OUT of the retrieved set under the fault. Those are the candidates worth an LLM probe.
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
L12 = "sentence-transformers/all-MiniLM-L12-v2"
MQA = "sentence-transformers/multi-qa-MiniLM-L6-cos-v1"


def retrieved(conn, question):
    cfg = load_config(conn)
    return [c.chunk_id for c in retrieve(conn, question, cfg.top_k, cfg.similarity_threshold,
                                         cfg.embedding_model)]


def answer_present(conn, chunk_ids, pattern):
    """Is the answer fact textually inside any retrieved chunk?"""
    if not chunk_ids:
        return False
    rows = conn.execute("SELECT text FROM chunks WHERE chunk_id = ANY(%s)", (list(chunk_ids),)).fetchall()
    return any(re.search(pattern, t, re.IGNORECASE) for (t,) in rows)


# (question, regex for the answer fact, doc that holds it)
QUESTIONS = [
    ("What's the refund window for returning an item?", r"14 days", "refund-policy"),
    ("How much does express shipping cost?", r"\$24\.95|\$19\.95", "shipping-expedited"),
    ("How long is the warranty on Kestrel-brand gear?", r"2[- ]year", "warranty"),
    ("How long will you hold my store pickup order?", r"7 days", "store-pickup"),
    ("Is there a military discount?", r"15\s?%|15 percent", "military-discount"),
    ("Do Kestrel gift cards expire?", r"never expire|do not expire", "gift-cards"),
    ("How much is the deposit to rent a tent?", r"\$100", "rentals"),
    ("How long is a password reset link valid?", r"30[- ]min", "password-reset"),
    ("What's the minimum order for free standard shipping?", r"\$75", "shipping-domestic"),
    ("How many days do I have to report a damaged item?", r"\d+ days", "damaged-items"),
    ("What is the restocking fee on a returned tent?", r"\d+\s?%|\$\d+", "returns-process"),
    ("How long does an international shipment take?", r"\d+", "shipping-international"),
    ("What discount do students get?", r"\d+\s?%", "student-discount"),
    ("How long do I have to cancel an order?", r"\d+ ", "order-cancellation"),
    ("What are the loyalty tier thresholds?", r"\$\d+", "loyalty-tiers"),
    ("How do I trade in old gear?", r"\w+", "trade-in"),
    ("What's the price match window?", r"\d+ days", "price-matching"),
    ("How long are backordered items held?", r"\d+", "backorders"),
    ("Can I rent gear for a weekend trip?", r"\$100|\d+ days", "rentals"),
    ("What happens if my package is lost?", r"\d+", "lost-packages"),
    ("How do I exchange an item for a different size?", r"\d+ days|exchange", "exchanges"),
    ("Are final sale items returnable?", r"cannot be returned|final sale", "final-sale"),
    ("What is the tax exempt process?", r"\w+", "tax-exempt"),
    ("How long do repairs take?", r"\d+", "repairs"),
]


def probe(conn, label, apply_fault, revert_fault):
    out = []
    for question, pattern, doc in QUESTIONS:
        base = retrieved(conn, question)
        base_ok = answer_present(conn, base, pattern)
        apply_fault()
        bad = retrieved(conn, question)
        bad_ok = answer_present(conn, bad, pattern)
        revert_fault()
        if base_ok and not bad_ok:
            out.append((question, doc, base, bad))
            print(f"  BREAKS  {question}")
            print(f"          healthy={base}")
            print(f"          faulted={bad}")
    print(f"[{label}] {len(out)} breaking questions of {len(QUESTIONS)}")
    return out


def main():
    with db.connect() as conn:
        state.reset_healthy(conn)

        for model in (L12, MQA):
            print(f"\n===== embedding_mismatch {model} =====")
            f = faults.build(conn, {"type": "embedding_mismatch",
                                    "params": {"embedding_model": model},
                                    "age_min": 30, "change_id": "change:p", "description": "p"}, NOW)
            probe(conn, f"embedding/{model.split('/')[-1]}", f.apply, f.revert)
            state.reset_healthy(conn)

        print("\n===== chunking_regression (per doc, size 80) =====")
        docs = sorted({d for _, _, d in QUESTIONS})
        for doc in docs:
            f = faults.build(conn, {"type": "chunking_regression",
                                    "params": {"doc_id": doc, "chunk_size": 80},
                                    "age_min": 30, "change_id": "change:p", "description": "p"}, NOW)
            qs = [(q, p, d) for q, p, d in QUESTIONS if d == doc]
            for question, pattern, _ in qs:
                base = retrieved(conn, question)
                base_ok = answer_present(conn, base, pattern)
                f.apply()
                bad = retrieved(conn, question)
                bad_ok = answer_present(conn, bad, pattern)
                f.revert()
                flag = "BREAKS" if (base_ok and not bad_ok) else "      "
                print(f"  {flag}  [{doc}] {question}")
                if base_ok and not bad_ok:
                    print(f"          healthy={base}")
                    print(f"          faulted={bad}")
            state.reset_healthy(conn)


if __name__ == "__main__":
    main()
