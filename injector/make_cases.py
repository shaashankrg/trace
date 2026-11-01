"""Generate the injection cases from templates, write them to injector/cases/, load eval.cases.

11 fault types; the ones that can actually break an answer carry 2-5 variants each, about half
with a decoy. The decoy's position relative to the real fault is randomized so "ignore the latest
change" is not a winning rule. Policy numbers come from facts.yaml, store numbers from the
deterministic store seed.

Day 8 adds a train/dev/test split (SPLITS) and quality-drop alerts. `embedding_mismatch` and
`guardrail_misfire` are held out: every one of their cases is in the test split, so the test run
measures generalization to failure modes the prompt was never tuned on.

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
               "silent_tool_failure", "stale_tool_data", "prompt_regression",
               "chunking_regression", "embedding_mismatch", "model_downgrade", "guardrail_misfire"]

# Held out of train and dev entirely: the test run is the first time the prompt meets them.
HELD_OUT_TYPES = ("embedding_mismatch", "guardrail_misfire")

# Faults that degrade many questions at once, so the realistic alert is a metric drop rather
# than one flagged conversation.
BROAD_FAULTS = ("prompt_regression", "threshold_too_strict", "embedding_mismatch",
                "model_downgrade", "context_overflow", "guardrail_misfire")
QUALITY_DROP_METRIC = "kestrel_canary_accuracy"
# Each quality-drop case runs the full 60-question canary, so keep the count small.
QUALITY_DROP_TARGET = 10
# New-on-Day-8 types that are not held out still need train coverage.
TRAIN_NEW_PER_TYPE = 2

# The embedding model the mismatch switches to: same 384 dimensions as the indexed vectors, so
# nothing raises and the only symptom is worse ranking (probe: eval/probes/probe_retrieval_breakage.py).
MISMATCH_MODEL = "sentence-transformers/all-MiniLM-L12-v2"

DESCRIPTIONS = {
    "missing_document": ["Index cleanup", "Clean up index entries", "Remove outdated entries from the index"],
    "chunking_regression": ["Tune chunk size for retrieval", "Reduce chunk size", "Re-chunk the knowledge base"],
    "embedding_mismatch": ["Upgrade the embedding model", "Switch query embeddings to L12"],
    "model_downgrade": ["Cut inference cost", "Move to a cheaper model"],
    "guardrail_misfire": ["Expand the blocked topic list", "Add a blocked topic"],
    "threshold_too_strict": ["Tune retrieval relevance cutoff", "Reduce low-relevance retrievals"],
    "context_overflow": ["Increase retrieval recall", "Give the model more context"],
    "prompt_regression": ["Simplify system prompt", "Trim redundant prompt instructions"],
    "typo_prompt_edit": ["Fix typo in system prompt", "Prompt typo fix"],
    "unrelated_config_change": ["Raise response length limit", "Allow longer replies"],
}
DECOY_DOCS = ["gear-donation", "trail-volunteer-days", "camp-skills-workshops"]


def alert(question: str, correction: str, must: list, must_not: list,
          alert_type: str = "flagged_conversation") -> dict:
    """One case's alert and the reviewer's correction written as patterns.

    `alert_type` is "flagged_conversation" (a human flagged this one answer) or "quality_drop"
    (a metric fell; run_case fills in the before/after numbers and the failing samples from the
    canary run it does while the fault is active)."""
    return {
        "alert": {"type": alert_type, "question": question, "reviewer_correction": correction},
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
        "chunking_regression": CHUNKING_REGRESSION(f),
        "embedding_mismatch": EMBEDDING_MISMATCH(f),
        "model_downgrade": MODEL_DOWNGRADE(f),
        "guardrail_misfire": GUARDRAIL_MISFIRE(f),
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
    # A refusal in any of the ways the assistant phrases one (Day 7: the first version missed
    # "I don't know", "contacting support" and "there is no mention of", so correct refusals failed).
    unknown = (r"(?:don'?t|do not) (?:have (?:any |specific )?information|know)|not covered|"
               r"no (?:specific )?(?:information|mention|instructions|guidance|details)|"
               r"(?:doesn'?t|does not|don'?t|do not) (?:mention|cover|address|specify|say)|"
               r"not (?:mentioned|specified|addressed)|contact(?:ing)? (?:our )?(?:customer )?(?:support|team)|"
               r"support team|help@kestrel")
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



def CHUNKING_REGRESSION(f: dict) -> list[dict]:
    """Re-chunk a doc small enough that the key sentence splits and no one chunk answers the
    question. Sizes are not guesses: eval/probes/probe_chunking_sizes.py swept 40-2000 against
    every policy question and these (doc, size) pairs are the ones that drop the answer out of
    the retrieved set. Size 80 was mild (only price-matching broke); 40-60 breaks reliably."""
    return [
        {"params": {"doc_id": "refund-policy", "chunk_size": 40},
         **alert("What's the refund window for returning an item?",
                 f"The refund window is {f['refund_window_days']} days.",
                 [days(f["refund_window_days"])], [])},
        {"params": {"doc_id": "military-discount", "chunk_size": 60},
         **alert("Is there a military discount?",
                 f"Yes, military members, veterans and families get {f['military_discount_pct']}% off.",
                 [rf"\b{f['military_discount_pct']}\s?(?:%|percent)"], [])},
        {"params": {"doc_id": "rentals", "chunk_size": 40},
         **alert("How much is the deposit to rent a tent?",
                 f"The rental deposit is {f['rental_deposit']}.",
                 [lit(f["rental_deposit"])], [])},
        {"params": {"doc_id": "price-matching", "chunk_size": 80},
         **alert("What's the price match window?",
                 f"The price match window is {f['price_match_window_days']} days.",
                 [days(f["price_match_window_days"])], [])},
        {"params": {"doc_id": "store-pickup", "chunk_size": 40},
         **alert("How long will you hold my store pickup order?",
                 f"Store pickup orders are held for {f['pickup_hold_days']} days.",
                 [days(f["pickup_hold_days"])], [])},
    ]


def EMBEDDING_MISMATCH(f: dict) -> list[dict]:
    """The query embedding model changes and nothing is re-indexed. Same dimension (384), so no
    error anywhere: queries and chunks just live in different spaces and ranking degrades. Held
    out of train/dev. Questions are the ones the probe showed losing their answer chunk."""
    m = MISMATCH_MODEL
    return [
        {"params": {"embedding_model": m},
         **alert("How long is a password reset link valid?",
                 f"Password reset links are valid for {f['password_reset_expiry_minutes']} minutes.",
                 [rf"\b{n(f['password_reset_expiry_minutes'])}[- ]minutes?\b"], [])},
        {"params": {"embedding_model": m},
         **alert("What are the loyalty tier thresholds?",
                 "The loyalty tiers and their spend thresholds are listed in the loyalty-tiers policy.",
                 [r"\$\d+"], [])},
        {"params": {"embedding_model": m},
         **alert("What's the price match window?",
                 f"The price match window is {f['price_match_window_days']} days.",
                 [days(f["price_match_window_days"])], [])},
    ]


def MODEL_DOWNGRADE(f: dict) -> list[dict]:
    """No cases. Every available smaller model is unusable as a *silent* downgrade:

      gemini-2.5-flash / -flash-lite   404: retired, "no longer available to new users"
      gemma-4-26b / gemma-4-31b        assistant.llm routes any non-`gemini-` name to the
                                       Anthropic SDK, so they raise instead of answering badly
      gemini-3.1-flash-lite            too capable: right 3/3 on all 13 two-hop questions and
                                       7 of 8 three-hop ones; the single break did not reproduce
                                       on a second run, so it is sampling noise, not a fault

    A fault that only shows up as flakiness cannot be scored: the replay rule needs a patch to
    flip the answer repeatably. Same call as context_overflow on Day 3 - the fault class and its
    apply/revert tests stay, the cases do not. Probe: eval/probes/probe_model_downgrade.py.
    """
    return []


def GUARDRAIL_MISFIRE(f: dict) -> list[dict]:
    """An everyday topic joins the blocked list, so ordinary questions get the refusal text.
    The guardrail matches on word boundaries, so the topic has to appear as a whole word in the
    question: "return" does not fire on "returning". Held out of train/dev.

    must_not_contain carries the refusal itself, so "blocked" fails the check even when the
    answer pattern is loose."""
    refused = r"can'?t help with that topic|cannot help with that topic"
    return [
        {"params": {"topic": "refund"},
         **alert("What's the refund window for returning an item?",
                 f"The refund window is {f['refund_window_days']} days. The assistant refused a "
                 f"question it should have answered.",
                 [days(f["refund_window_days"])], [refused])},
        {"params": {"topic": "shipping"},
         **alert("What's the minimum order for free standard shipping?",
                 f"Free standard shipping starts at {f['free_shipping_threshold']}. The assistant "
                 f"refused a question it should have answered.",
                 [lit(f["free_shipping_threshold"])], [refused])},
        {"params": {"topic": "warranty"},
         **alert("How long is the warranty on Kestrel-brand gear?",
                 f"The warranty is {f['warranty_years']} years. The assistant refused a question "
                 f"it should have answered.",
                 [rf"\b{n(f['warranty_years'])}[- ]years?\b"], [refused])},
        {"params": {"topic": "exchange"},
         **alert("Can I exchange an item for a different size?",
                 "Yes, unused items can be exchanged for a different size or color. The assistant "
                 "refused a question it should have answered.",
                 [r"\byes\b|can exchange|may exchange"], [refused])},
        {"params": {"topic": "deposit"},
         **alert("How much is the deposit to rent a tent?",
                 f"The rental deposit is {f['rental_deposit']}. The assistant refused a question "
                 f"it should have answered.",
                 [lit(f["rental_deposit"])], [refused])},
    ]



def TEST_EXTRA(f: dict, data: dict) -> dict[str, list[dict]]:
    """Extra questions for the legacy fault types, so the test split is not limited to the
    questions the Day 3 train/dev cases already own.

    Every entry here was confirmed end-to-end by eval/probes/probe_new_questions.py: the healthy
    assistant answers it correctly, and the fault makes the answer fail the check. Candidates that
    did not break (most `stale_document` doc pairs, several `missing_document` docs the assistant
    answers from a neighbouring doc) are left out rather than recorded as invalid cases.
    """
    names = {pid: name for pid, name, _ in data["products"]}
    inventory = dict(data["inventory"])
    orders = data["orders"]
    first_item = {}
    for oid, pid, _ in data["order_items"]:
        first_item.setdefault(oid, names[pid])
    today = REF_DATE.date().isoformat()

    def count(cid: str) -> int:
        return sum(1 for o in orders if o[1] == cid)

    def stock(pid: str, new_qty: int) -> dict:
        old = inventory[pid]
        if new_qty:
            return {"params": {"product_id": pid, "new_qty": new_qty},
                    **alert(f"Is the {names[pid]} (product {pid}) in stock, and how many do you have?",
                            f"The {names[pid]} is in stock: {new_qty} units available.",
                            [rf"\b{new_qty}\b"], [r"out of stock|sold out|unavailable"])}
        return {"params": {"product_id": pid, "new_qty": 0},
                **alert(f"Is the {names[pid]} (product {pid}) in stock, and how many do you have?",
                        f"The {names[pid]} is out of stock. There are none left.",
                        [r"out of stock|not in stock|sold out|unavailable|none left|\b0\b"],
                        [rf"\b{old}\b"])}

    def order_of(oid: str):
        return next(o for o in orders if o[0] == oid)

    def refund_yes(oid: str) -> dict:
        o = order_of(oid)
        age = (REF_DATE - o[3]).days
        return {"params": {"tool": "lookup_order"},
                **alert(f"I'm customer {o[1]}. Today is {today}. Can I still return the "
                        f"{first_item[oid]} from order {oid} for a refund?",
                        f"Order {oid} was delivered {age} days ago, inside the "
                        f"{f['refund_window_days']}-day window. The answer is yes.",
                        [YES], [NO])}

    def status(oid: str) -> dict:
        o = order_of(oid)
        return {"params": {"tool": "lookup_order"},
                **alert(f"I'm customer {o[1]}. What's the status of order {oid}?",
                        f"Order {oid} is shipped.", [r"\bshipped\b"], [])}

    def how_many(cid: str) -> dict:
        return {"params": {"tool": "lookup_order"},
                **alert(f"I'm customer {cid}. How many orders have I placed with you?",
                        f"Customer {cid} has {count(cid)} orders on file. The assistant said it found none.",
                        [rf"\b{n(count(cid))}\b"], [])}

    unknown = (r"(?:don'?t|do not) (?:have (?:any |specific )?information|know)|not covered|"
               r"no (?:specific )?(?:information|mention|instructions|guidance|details)|"
               r"(?:doesn'?t|does not|don'?t|do not) (?:mention|cover|address|specify|say)|"
               r"not (?:mentioned|specified|addressed)|contact(?:ing)? (?:our )?(?:customer )?(?:support|team)|"
               r"support team|help@kestrel")

    return {
        "missing_document": [
            {"params": {"doc_id": "student-discount"},
             **alert("What discount do students get?",
                     f"Students get {f['student_discount_pct']}% off with verification."
                     if "student_discount_pct" in f else "Students get a verified student discount.",
                     [r"\b\d+\s?(?:%|percent)"], [])},
        ],
        "threshold_too_strict": [
            {"params": {"value": 0.85},
             **alert("How long is the warranty on Kestrel-brand gear?",
                     f"The warranty is {f['warranty_years']} years.",
                     [rf"\b{n(f['warranty_years'])}[- ]years?\b"], [])},
            {"params": {"value": 0.9},
             **alert("How long will you hold my store pickup order?",
                     f"Store pickup orders are held for {f['pickup_hold_days']} days.",
                     [days(f["pickup_hold_days"])], [])},
            {"params": {"value": 0.8},
             **alert("Is there a military discount?",
                     f"Yes, military members, veterans and families get {f['military_discount_pct']}% off.",
                     [rf"\b{f['military_discount_pct']}\s?(?:%|percent)"], [])},
            {"params": {"value": 0.88},
             **alert("Do Kestrel gift cards expire?",
                     "Gift cards never expire.",
                     [r"never expire|do not expire|don'?t expire"], [])},
            {"params": {"value": 0.85},
             **alert("What's the price match window?",
                     f"The price match window is {f['price_match_window_days']} days.",
                     [days(f["price_match_window_days"])], [])},
        ],
        "silent_tool_failure": [
            how_many("C-0022"), how_many("C-0012"),
            status("O-1023"), status("O-1027"),
            refund_yes("O-1003"), refund_yes("O-1008"),
        ],
        "stale_tool_data": [
            stock("P-020", 17), stock("P-025", 17), stock("P-032", 17),
            stock("P-002", 0), stock("P-004", 0), stock("P-006", 0),
        ],
        "prompt_regression": [
            {"params": {},
             **alert("How often should I replace my hiking socks?",
                     f"The documents have no sock-replacement guidance. The assistant should have "
                     f"said it didn't know and pointed the customer to support ({f['support_email']}), "
                     f"not given generic tips.",
                     [unknown],
                     [r"every \d+|general(?:ly)? recommend|rule of thumb|signs of wear"])},
        ],
    }


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
              "context_overflow": "config_change", "prompt_regression": "prompt_edit",
              "chunking_regression": "config_change", "embedding_mismatch": "config_change",
              "model_downgrade": "model_change", "guardrail_misfire": "config_change"}
DECOY_KIND = {"typo_prompt_edit": "prompt_edit", "unrelated_doc_added": "doc_update",
              "unrelated_config_change": "config_change"}


# The 20 Day 3 cases, by case_id, split 15 train / 5 dev. They were generated before Day 8 and
# their snapshots are already recorded, so their ids and specs are frozen here: regenerating must
# not renumber them. Dev holds the five looked at most during Day 5-7 development.
DEV_CASES = ("case-003", "case-007", "case-011", "case-014", "case-019")
LEGACY_COUNT = 20


def split_of(case_id: str) -> str:
    k = int(case_id.split("-")[1])
    if k > LEGACY_COUNT:
        return "test"
    return "dev" if case_id in DEV_CASES else "train"


def decoy_component(kind: str) -> str:
    """The component a decoy points at, so it can be kept away from the true root component."""
    return {"typo_prompt_edit": "prompt", "unrelated_doc_added": "knowledge_base",
            "unrelated_config_change": "config"}[kind]


def compatible_decoys(root_component: str) -> list[str]:
    """Decoy kinds whose component differs from the fault's root component. Otherwise "blamed
    the decoy" and "got it right" are the same answer and the case cannot score either way."""
    return [k for k in ("unrelated_config_change", "unrelated_doc_added", "typo_prompt_edit")
            if decoy_component(k) != root_component]


def build_cases() -> list[dict]:
    """Every case: the 20 frozen Day 3 cases, then today's test cases.

    Day 3 cases are read back from injector/cases/ so their ids, questions, decoys and ages are
    exactly what was recorded. Everything new is numbered from LEGACY_COUNT + 1 and lands in test.
    """
    f, data = load_facts(), generate()
    legacy = legacy_cases()
    cases = [{**c, "split": split_of(c["case_id"])} for c in legacy]

    rng = random.Random(SEED + 8)  # a separate stream, so the Day 3 cases are untouched
    all_variants = variants(f, data)
    for ftype, extra in TEST_EXTRA(f, data).items():
        all_variants[ftype] = all_variants[ftype] + extra
    used_ids = {r["change_id"] for r in state.baseline_changes()}
    used_ids |= {c["fault"].get("change_id") for c in legacy if c["fault"].get("change_id")}
    used_ids |= {c["decoy"]["change_id"] for c in legacy if c["decoy"]}
    used_ids.discard(None)

    # Which (type, variant) pairs the Day 3 cases already used, so test cases are new questions.
    taken = {(c["fault"]["type"], c["alert"]["question"]) for c in legacy}

    # chunking_regression is new on Day 8 and is not a held-out type, so the train split has to
    # cover it or the prompt is tuned without ever meeting a retrieval-shape failure. Its first
    # TRAIN_NEW_PER_TYPE variants go to train; the rest stay in test.
    train_quota = {"chunking_regression": TRAIN_NEW_PER_TYPE}

    raw = []
    for ftype in FAULT_ORDER:
        root = faults.FAULTS[ftype].root_component
        for v in all_variants[ftype]:
            if (ftype, v["alert"]["question"]) in taken:
                continue  # the Day 3 split owns this question
            fault_age = rng.randint(20, 120)
            fault = {"type": ftype, "params": v["params"], "age_min": fault_age,
                     "author": rng.choice(state.AUTHORS),
                     "description": rng.choice(DESCRIPTIONS[ftype]) if ftype in DESCRIPTIONS else ""}
            if ftype in FAULT_KIND:
                fault["change_id"] = change_id(FAULT_KIND[ftype], rng, used_ids)

            # about half the cases get a decoy, and never one on the true root component
            decoy = None
            options = compatible_decoys(root)
            if options and rng.random() < 0.5:
                kind = rng.choice(options)
                if ftype == "prompt_regression" and kind == "typo_prompt_edit":
                    kind = "unrelated_config_change"  # the typo would be built on the broken prompt
                position = rng.choice(["before", "after"])
                gap = rng.randint(10, 45)
                age = fault_age + gap if position == "before" else max(5, fault_age - gap)
                decoy = {"type": kind, "params": decoy_params(kind, rng), "age_min": age,
                         "position": position, "author": rng.choice(state.AUTHORS),
                         "change_id": change_id(DECOY_KIND[kind], rng, used_ids),
                         "description": (f"Publish {{doc}}@v1" if kind == "unrelated_doc_added"
                                         else rng.choice(DESCRIPTIONS[kind]))}
                if kind == "unrelated_doc_added":
                    decoy["description"] = decoy["description"].format(doc=decoy["params"]["doc_id"])

            split = "test"
            if train_quota.get(ftype, 0) > 0 and ftype not in HELD_OUT_TYPES:
                split = "train"
                train_quota[ftype] -= 1
            raw.append({"fault": fault, "decoy": decoy, "alert": dict(v["alert"]),
                        "answer_check": v["answer_check"], "split": split,
                        "_broad": ftype in BROAD_FAULTS})

    rng.shuffle(raw)  # so case numbers do not reveal the fault type

    # A broad fault shows up as a metric drop rather than one flagged conversation. Each of these
    # runs the full 60-question canary, so they are capped at QUALITY_DROP_TARGET to control cost;
    # the rest of the broad-fault cases keep the flagged-conversation alert.
    broad = [c for c in raw if c["_broad"] and c["split"] == "test"]
    rng.shuffle(broad)
    for c in broad[:QUALITY_DROP_TARGET]:
        c["alert"]["type"] = "quality_drop"
        c["alert"]["metric"] = QUALITY_DROP_METRIC
    for c in raw:
        c.pop("_broad", None)

    for k, c in enumerate(raw, start=LEGACY_COUNT + 1):
        case_id = f"case-{k:03d}"
        conv = "conv-" + hashlib.sha1(case_id.encode()).hexdigest()[:8]
        cases.append({"case_id": case_id, "conversation_id": conv, **c})
    return cases


def legacy_cases() -> list[dict]:
    """The Day 3 cases as recorded on disk, without any `split` key they may already carry."""
    out = []
    for path in sorted(CASES_DIR.glob("case-*.json")):
        c = json.loads(path.read_text(encoding="utf-8"))
        if int(c["case_id"].split("-")[1]) <= LEGACY_COUNT:
            c.pop("split", None)
            out.append(c)
    if len(out) != LEGACY_COUNT:
        raise RuntimeError(
            f"expected {LEGACY_COUNT} Day 3 cases in {CASES_DIR}, found {len(out)}; "
            "they are frozen inputs and must not be regenerated")
    return out


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
    by_split = {s: sum(1 for c in cases if c["split"] == s) for s in ("train", "dev", "test")}
    drops = sum(1 for c in cases if c["alert"]["type"] == "quality_drop")
    with_decoy = sum(1 for c in cases if c["decoy"])
    print(f"wrote {len(cases)} cases {by_type}, {with_decoy} with a decoy")
    print(f"  splits {by_split}; {drops} quality-drop alerts; held out in test only: "
          f"{', '.join(HELD_OUT_TYPES)}")
    if not args.no_load:
        load_into_eval(cases)
        print("loaded into eval.cases")


if __name__ == "__main__":
    main()
