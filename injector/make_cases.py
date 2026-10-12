"""Generate the injection cases from templates, write them to injector/cases/, load eval.cases.

7 fault types x 2-3 variants = 20 cases, about half with a decoy. The decoy's position
relative to the real fault is randomized so "ignore the latest change" is not a winning rule.
Policy numbers come from facts.yaml, store numbers from the deterministic store seed.

Usage: python -m injector.make_cases [--no-load]
"""
import argparse
import hashlib
import json
import random
from datetime import datetime, timezone
from pathlib import Path

from assistant.store_seed import REF_DATE, generate
from eval.make_canary import NO, YES, days, lit, n
from eval.render_docs import load_facts
from injector import decoys, faults, state

CASES_DIR = Path(__file__).parent / "cases"
SEED = 2026
FAULT_ORDER = ["stale_document", "missing_document", "threshold_too_strict", "context_overflow",
               "silent_tool_failure", "stale_tool_data", "prompt_regression"]

DESCRIPTIONS = {
    "missing_document": ["Index cleanup", "Clean up index entries", "Remove outdated entries from the index"],
    "threshold_too_strict": ["Tune retrieval relevance cutoff", "Reduce low-relevance retrievals"],
    "context_overflow": ["Increase retrieval recall", "Give the model more context"],
    "prompt_regression": ["Simplify system prompt", "Trim redundant prompt instructions"],
    "typo_prompt_edit": ["Fix typo in system prompt", "Prompt typo fix"],
    "unrelated_config_change": ["Raise response length limit", "Allow longer replies"],
}
DECOY_DOCS = ["gear-donation", "trail-volunteer-days", "camp-skills-workshops"]


def alert(question: str, correction: str, must: list, must_not: list) -> dict:
    return {
        "alert": {"type": "flagged_conversation", "question": question, "reviewer_correction": correction},
        "answer_check": {"must_contain": must, "must_not_contain": must_not},
    }


def variants(f: dict, data: dict) -> dict[str, list[dict]]:
    names = {pid: name for pid, name, _ in data["products"]}
    inventory = dict(data["inventory"])
    orders = data["orders"]
    first_item = {}
    for oid, pid, _ in data["order_items"]:
        first_item.setdefault(oid, names[pid])

    def count(cid: str) -> int:
        return sum(1 for o in orders if o[1] == cid)

    def restocked(pid: str, new_qty: int) -> dict:
        assert inventory[pid] == 0
        return {"params": {"product_id": pid, "new_qty": new_qty},
                **alert(f"Is the {names[pid]} (product {pid}) in stock, and how many do you have?",
                        f"The {names[pid]} is in stock: {new_qty} units available.",
                        [rf"\b{new_qty}\b"], [r"out of stock|sold out|unavailable"])}

    def sold_out(pid: str) -> dict:
        old = inventory[pid]
        assert old > 0
        return {"params": {"product_id": pid, "new_qty": 0},
                **alert(f"Is the {names[pid]} (product {pid}) in stock, and how many do you have?",
                        f"The {names[pid]} is out of stock. There are none left.",
                        [r"out of stock|not in stock|sold out|unavailable|none left|\b0\b"],
                        [rf"\b{old}\b"])}

    delivered_recent = next(o for o in orders if o[4] == "delivered" and 1 <= (REF_DATE - o[3]).days <= 8)
    shipped = next(o for o in orders if o[4] == "shipped")
    in_warranty = next(o for o in orders if o[4] == "delivered" and 400 <= (REF_DATE - o[3]).days <= 600)
    cust_many = "C-0011"

    return {
        "stale_document": [
            {"params": {"doc_id": "refund-policy", "old_version": "v1", "new_version": "v2"},
             **alert("What's the refund window for returning an item?",
                     f"The refund window is {f['refund_window_days']} days, not {f['refund_window_days_v1']}.",
                     [days(f["refund_window_days"])], [days(f["refund_window_days_v1"])])},
            {"params": {"doc_id": "shipping-expedited", "old_version": "v1", "new_version": "v2"},
             **alert("How much does express shipping cost?",
                     f"Express shipping is {f['express_shipping_price']}, not {f['express_shipping_price_v1']}.",
                     [lit(f["express_shipping_price"])], [lit(f["express_shipping_price_v1"])])},
            {"params": {"doc_id": "warranty", "old_version": "v1", "new_version": "v2"},
             **alert("How long is the warranty on Kestrel-brand gear?",
                     f"The warranty is {f['warranty_years']} years, not {f['warranty_years_v1']}.",
                     [rf"\b{n(f['warranty_years'])}[- ]years?\b"], [rf"\b{n(f['warranty_years_v1'])}[- ]year\b"])},
        ],
        "missing_document": [
            {"params": {"doc_id": "gift-cards"},
             **alert("Do Kestrel gift cards expire?",
                     "Gift cards never expire. The assistant should have answered, not said it didn't know.",
                     [r"never expire|do not expire|don't expire|no expiration"], [])},
            {"params": {"doc_id": "military-discount"},
             **alert("Is there a military discount?",
                     f"Yes, military members, veterans and families get {f['military_discount_pct']}% off.",
                     [rf"\b{f['military_discount_pct']}\s?(?:%|percent)"], [])},
            {"params": {"doc_id": "store-pickup"},
             **alert("How long will you hold my store pickup order?",
                     f"Store pickup orders are held for {f['pickup_hold_days']} days.",
                     [days(f["pickup_hold_days"])], [])},
        ],
        "threshold_too_strict": [
            {"params": {"value": 0.85},
             **alert("What's the minimum order for free standard shipping?",
                     f"Free standard shipping starts at {f['free_shipping_threshold']}.",
                     [lit(f["free_shipping_threshold"])], [lit(f["free_shipping_threshold_v1"])])},
            {"params": {"value": 0.9},
             **alert("How long is a password reset link valid?",
                     f"Password reset links are valid for {f['password_reset_expiry_minutes']} minutes.",
                     [rf"\b{n(f['password_reset_expiry_minutes'])}[- ]minutes?\b"], [])},
            {"params": {"value": 0.8},
             **alert("How much is the deposit to rent a tent?",
                     f"The rental deposit is {f['rental_deposit']}.",
                     [lit(f["rental_deposit"])], [])},
        ],
        "context_overflow": CONTEXT_OVERFLOW(f),
        "silent_tool_failure": [
            {"params": {"tool": "lookup_order"},
             **alert(f"I'm customer {cust_many}. How many orders have I placed with you?",
                     f"Customer {cust_many} has {count(cust_many)} orders on file. The assistant said it found none.",
                     [rf"\b{n(count(cust_many))}\b"], [])},
            {"params": {"tool": "lookup_order"},
             **alert(f"I'm customer {shipped[1]}. What's the status of order {shipped[0]}?",
                     f"Order {shipped[0]} is shipped.",
                     [r"\bshipped\b"], [])},
            {"params": {"tool": "lookup_order"},
             **alert(f"I'm customer {delivered_recent[1]}. Today is {REF_DATE.date().isoformat()}. "
                     f"Can I still return the {first_item[delivered_recent[0]]} from order {delivered_recent[0]} for a refund?",
                     f"Order {delivered_recent[0]} was delivered {(REF_DATE - delivered_recent[3]).days} days ago, "
                     f"inside the {f['refund_window_days']}-day window. The answer is yes.",
                     [YES], [NO])},
            {"params": {"tool": "lookup_order"},
             **alert(f"I'm customer {in_warranty[1]}. Today is {REF_DATE.date().isoformat()}. A seam failed on the "
                     f"{first_item[in_warranty[0]]} from order {in_warranty[0]}. Is it still under warranty?",
                     f"Order {in_warranty[0]} was delivered {(REF_DATE - in_warranty[3]).days} days ago, inside the "
                     f"{f['warranty_years']}-year warranty. The answer is yes.",
                     [YES], [NO])},
        ],
        "stale_tool_data": [restocked("P-003", 23), restocked("P-005", 18), sold_out("P-001"),
                            restocked("P-019", 12)],
        "prompt_regression": PROMPT_REGRESSION(f),
    }


def CONTEXT_OVERFLOW(f: dict) -> list[dict]:
    """No cases. Probed top_k 20, 40 and 200 against all 60 canary questions and 18 extra
    ones: the model answered every one correctly, and a bigger top_k only ever added recall
    (a Gold-tier question the healthy top_k=4 got wrong, top_k=20 got right). The fault class
    stays in faults.py and its tests, but a case that cannot break the answer is invalid."""
    return []


def PROMPT_REGRESSION(f: dict) -> list[dict]:
    # Each question is one the docs do not answer. With the grounding line the assistant says
    # it does not know; without it, it invents a policy or falls back on general advice.
    unknown = (r"(?:don'?t|do not) have (?:any |specific )?information|not covered|"
               r"no information|contact (?:our )?(?:support|team)|support team|help@kestrel")
    support = f"point the customer to support ({f['support_email']})"
    return [
        {"params": {},
         **alert("Can I change the shipping address after I place an order?",
                 f"The documents say nothing about changing an address. The assistant should have said it "
                 f"didn't know and {support}, not told me it isn't supported.",
                 [unknown],
                 [r"not supported|cannot be changed|can'?t be changed|unable to change|not possible|isn'?t possible"])},
        {"params": {},
         **alert("Can I return an item without a receipt?",
                 f"The documents don't say whether a receipt can be skipped. The assistant should have said it "
                 f"didn't know and {support}, not told me no receipt is needed.",
                 [unknown],
                 [r"(?:don'?t|do not) need (?:a |your )?(?:physical )?receipt|no receipt (?:is )?(?:needed|required)|receipt (?:is )?not (?:needed|required)"])},
        {"params": {},
         **alert("How should I break in my new hiking boots?",
                 f"The documents have no break-in guidance. The assistant should have said it didn't know and "
                 f"{support}, not given generic tips.",
                 [unknown],
                 [r"general best practices|start slow|around the house|moleskin|thick socks|short (?:hikes|walks)"])},
    ]


def decoy_plan() -> dict[tuple[str, int], str]:
    """Which (fault type, variant index) gets which decoy: 10 of 20, spread across types."""
    cycle = ["unrelated_config_change", "unrelated_doc_added", "typo_prompt_edit"]
    types = [t for t in FAULT_ORDER if t != "context_overflow"]
    slots = [(t, 1) for t in types] + [("stale_document", 2), ("missing_document", 2),
                                       ("silent_tool_failure", 3), ("stale_tool_data", 3)]
    plan = {}
    for k, slot in enumerate(slots):
        kind = cycle[k % 3]
        if slot[0] == "prompt_regression" and kind == "typo_prompt_edit":
            kind = cycle[(k + 1) % 3]  # the typo decoy would be built on the broken prompt
        plan[slot] = kind
    return plan


def decoy_params(kind: str, rng: random.Random) -> dict:
    return {"unrelated_config_change": {"value": 700},
            "unrelated_doc_added": {"doc_id": rng.choice(DECOY_DOCS)},
            "typo_prompt_edit": {}}[kind]


def change_id(kind: str, rng: random.Random, used: set) -> str:
    while True:
        cid = f"change:{state.change_slug(kind)}-{rng.randint(2000, 9999)}"
        if cid not in used:
            used.add(cid)
            return cid


FAULT_KIND = {"missing_document": "doc_delete", "threshold_too_strict": "config_change",
              "context_overflow": "config_change", "prompt_regression": "prompt_edit"}
DECOY_KIND = {"typo_prompt_edit": "prompt_edit", "unrelated_doc_added": "doc_update",
              "unrelated_config_change": "config_change"}


def build_cases() -> list[dict]:
    f, data = load_facts(), generate()
    rng = random.Random(SEED)
    plan = decoy_plan()
    all_variants = variants(f, data)
    used_ids = {r["change_id"] for r in state.baseline_changes()}

    positions = ["before"] * 5 + ["after"] * 5
    rng.shuffle(positions)
    raw = []
    for ftype in FAULT_ORDER:
        for i, v in enumerate(all_variants[ftype]):
            fault_age = rng.randint(20, 120)
            fault = {"type": ftype, "params": v["params"], "age_min": fault_age,
                     "author": rng.choice(state.AUTHORS),
                     "description": rng.choice(DESCRIPTIONS[ftype]) if ftype in DESCRIPTIONS else ""}
            if ftype in FAULT_KIND:
                fault["change_id"] = change_id(FAULT_KIND[ftype], rng, used_ids)
            decoy = None
            if (ftype, i) in plan:
                kind, position = plan[(ftype, i)], positions.pop()
                gap = rng.randint(10, 45)
                age = fault_age + gap if position == "before" else max(5, fault_age - gap)
                decoy = {"type": kind, "params": decoy_params(kind, rng), "age_min": age,
                         "position": position, "author": rng.choice(state.AUTHORS),
                         "change_id": change_id(DECOY_KIND[kind], rng, used_ids),
                         "description": (f"Publish {{doc}}@v1" if kind == "unrelated_doc_added"
                                         else rng.choice(DESCRIPTIONS[kind]))}
                if kind == "unrelated_doc_added":
                    decoy["description"] = decoy["description"].format(doc=decoy["params"]["doc_id"])
            raw.append({"fault": fault, "decoy": decoy, "alert": v["alert"], "answer_check": v["answer_check"]})

    rng.shuffle(raw)  # so case numbers do not reveal the fault type
    cases = []
    for k, c in enumerate(raw, start=1):
        case_id = f"case-{k:03d}"
        conv = "conv-" + hashlib.sha1(case_id.encode()).hexdigest()[:8]
        cases.append({"case_id": case_id, "conversation_id": conv, **c})
    return cases


def ground_truth(case: dict) -> dict:
    now = datetime.now(timezone.utc)
    return faults.build(None, case["fault"], now).ground_truth()


def write_files(cases: list[dict]) -> None:
    CASES_DIR.mkdir(exist_ok=True)
    for old in CASES_DIR.glob("case-*.json"):
        old.unlink()
    for c in cases:
        (CASES_DIR / f"{c['case_id']}.json").write_text(json.dumps(c, indent=2) + "\n", encoding="utf-8")


def load_cases() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES_DIR.glob("case-*.json"))]


def load_into_eval(cases: list[dict]) -> None:
    from psycopg.types.json import Jsonb

    from eval.setup_eval import connect_eval
    with connect_eval() as conn:
        for c in cases:
            conn.execute(
                """INSERT INTO eval.cases (case_id, fault_type, has_decoy, spec, ground_truth)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (case_id) DO UPDATE SET fault_type = EXCLUDED.fault_type,
                     has_decoy = EXCLUDED.has_decoy, spec = EXCLUDED.spec,
                     ground_truth = EXCLUDED.ground_truth""",
                (c["case_id"], c["fault"]["type"], c["decoy"] is not None, Jsonb(c), Jsonb(ground_truth(c))))
        conn.execute("DELETE FROM eval.cases WHERE case_id <> ALL(%s)", ([c["case_id"] for c in cases],))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-load", action="store_true", help="only write the JSON files")
    args = parser.parse_args()
    cases = build_cases()
    write_files(cases)
    by_type = {t: sum(1 for c in cases if c["fault"]["type"] == t) for t in FAULT_ORDER}
    with_decoy = sum(1 for c in cases if c["decoy"])
    print(f"wrote {len(cases)} cases {by_type}, {with_decoy} with a decoy")
    if not args.no_load:
        load_into_eval(cases)
        print("loaded into eval.cases")


if __name__ == "__main__":
    main()
