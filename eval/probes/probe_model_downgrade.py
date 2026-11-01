"""model_downgrade, take 4: widen the warranty+repair+cost question shape to find ~6 variants.

The shape that works: in-warranty order, a defect, and "is it covered AND what does it cost me".
The small model gets the warranty window right but drops the cost half of the policy.
"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, r"C:\Users\shash\trace")
from dotenv import load_dotenv

load_dotenv(r"C:\Users\shash\trace\.env")

from assistant import db
from assistant.store_seed import REF_DATE, generate
from eval import canary
from eval.make_canary import YES
from eval.render_docs import load_facts
from injector import faults, state

NOW = datetime.now(timezone.utc)
SMALL = "gemini-3.1-flash-lite"
DECOY = "gemini-3.5-flash"
RUNS = 3

f = load_facts()
data = generate()
names = {pid: name for pid, name, _ in data["products"]}
orders = data["orders"]
items_by_order = {}
for oid, pid, qty in data["order_items"]:
    items_by_order.setdefault(oid, []).append(names[pid])
TODAY = REF_DATE.date().isoformat()

DEFECTS = ["The zipper broke", "A seam split", "The buckle snapped", "The fabric tore",
           "The stitching came apart", "A pole cracked"]
MUST = [YES, r"free|no (?:cost|charge)|covered|at no"]


def build():
    """Every in-warranty delivered order, paired with a defect phrasing."""
    in_w = [o for o in orders if o[4] == "delivered" and 370 <= (REF_DATE - o[3]).days <= 700]
    qs = []
    for i, o in enumerate(in_w[:14]):
        item = items_by_order.get(o[0], ["item"])[0]
        defect = DEFECTS[i % len(DEFECTS)]
        age = (REF_DATE - o[3]).days
        qs.append((f"wr-{o[0]}",
                   f"I'm customer {o[1]}. Today is {TODAY}. {defect} on the {item} from order "
                   f"{o[0]}. Is this covered under warranty, and what does it cost me?",
                   MUST, [], f"{age}d old, inside {f['warranty_years']}y"))
    return qs


def trial(q, check, tag, n):
    p = e = 0
    for i in range(n):
        try:
            r = canary.ask(q, f"{tag}-{i}")
            if not canary.score(check, r["answer"]):
                p += 1
        except Exception:
            e += 1
    return p, e


def main():
    qs = build()
    print(f"{len(qs)} candidates of the warranty+cost shape\n")
    with db.connect() as conn:
        state.reset_healthy(conn)

        reliable = []
        for label, q, must, must_not, why in qs:
            check = {"must_contain": must, "must_not_contain": must_not}
            p, e = trial(q, check, f"d4h-{label}", RUNS)
            print(f"[healthy] {label} ({why}): {p}/{RUNS}, {e} err")
            if p == RUNS and e == 0:
                reliable.append((label, q, must, must_not, why))
        print(f"\n{len(reliable)} reliable for the healthy model\n")

        keepers = []
        fault = faults.build(conn, {"type": "model_downgrade", "params": {"model": SMALL},
                                    "age_min": 30, "change_id": "c", "description": "p"}, NOW)
        fault.apply()
        try:
            for label, q, must, must_not, why in reliable:
                check = {"must_contain": must, "must_not_contain": must_not}
                p, e = trial(q, check, f"d4s-{label}", RUNS)
                mark = "  <- BREAKS 3/3" if (p == 0 and e == 0) else ""
                print(f"[{SMALL}] {label}: {p}/{RUNS}, {e} err{mark}")
                if p == 0 and e == 0:
                    keepers.append((label, q, must, must_not, why))
        finally:
            fault.revert()
            state.reset_healthy(conn)

        # the decoy model bump must answer these correctly
        print(f"\n--- decoy check: {DECOY} must still be right ---")
        clean = []
        d = faults.build(conn, {"type": "model_downgrade", "params": {"model": DECOY},
                                "age_min": 30, "change_id": "c", "description": "p"}, NOW)
        d.apply()
        try:
            for label, q, must, must_not, why in keepers:
                check = {"must_contain": must, "must_not_contain": must_not}
                p, e = trial(q, check, f"d4d-{label}", RUNS)
                ok = p == RUNS and e == 0
                print(f"[{DECOY}] {label}: {p}/{RUNS}, {e} err  {'clean' if ok else 'NOT clean'}")
                if ok:
                    clean.append((label, q, must, must_not, why))
        finally:
            d.revert()
            state.reset_healthy(conn)

        print(f"\n===== {len(clean)} usable model_downgrade questions =====")
        for label, q, must, must_not, why in clean:
            print(f"  {label} ({why})")
            print(f"    {q}")


if __name__ == "__main__":
    main()
