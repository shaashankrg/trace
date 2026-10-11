"""Build assistant/canary.json.

Doc answers come from facts.yaml; tool and multistep answers come from the
deterministic store seed. Patterns are case-insensitive regexes. Each
must_contain entry is a regex, or a list of regexes where any one may match.

Usage: python -m eval.make_canary
"""
import json
import re
from pathlib import Path

from assistant.store_seed import REF_DATE, generate
from eval.render_docs import load_facts

OUT = Path(__file__).resolve().parent.parent / "assistant" / "canary.json"
TODAY = REF_DATE.date().isoformat()
WORDS = {1: "one", 2: "two", 5: "five", 7: "seven", 14: "fourteen", 21: "twenty-one",
         30: "thirty", 60: "sixty", 90: "ninety"}
# The prompt asks yes/no answers to end with "Answer: Yes" or "Answer: No".
YES = r"answer:\W*yes\b"
NO = r"answer:\W*no\b"


def lit(s) -> str:
    return re.escape(str(s))


def n(num: int) -> str:
    return f"(?:{num}|{WORDS[num]})" if num in WORDS else str(num)


def days(num: int) -> str:
    return rf"\b{n(num)}[- ](?:calendar |business )?days?\b"


def q(category, question, must, must_not=(), smoke=False) -> dict:
    return {"category": category, "question": question, "must_contain": list(must),
            "must_not_contain": list(must_not), "smoke": smoke}


def doc_questions(f: dict) -> list[dict]:
    return [
        q("docs", "How long do I have to return boots for a refund?",
          [days(f["refund_window_days"])], [days(f["refund_window_days_v1"])], smoke=True),
        q("docs", "Once you receive my return, how long until the refund reaches my card?",
          [days(f["refund_processing_days"])]),
        q("docs", "Do I have to pay for return shipping?",
          [r"\bfree\b"], [lit(f["return_label_cost_v1"])], smoke=True),
        q("docs", "How long do I have to exchange a jacket for a different size?",
          [days(f["exchange_window_days"])], [days(f["exchange_window_days_v1"])]),
        q("docs", "What's the minimum order for free standard shipping?",
          [lit(f["free_shipping_threshold"])], [lit(f["free_shipping_threshold_v1"])], smoke=True),
        q("docs", "How much does standard shipping cost on a small order?",
          [lit(f["standard_shipping_fee"])]),
        q("docs", "How much is express shipping?",
          [lit(f["express_shipping_price"])], [lit(f["express_shipping_price_v1"])], smoke=True),
        q("docs", "How much does overnight shipping cost?", [lit(f["overnight_shipping_price"])]),
        q("docs", "Do you ship to the United Kingdom, and how much does it cost?",
          [lit(f["international_flat_rate"])]),
        q("docs", "How long is the warranty on Kestrel-brand gear?",
          [rf"\b{n(f['warranty_years'])}[- ]years?\b"],
          [rf"\b{n(f['warranty_years_v1'])}[- ]year\b"], smoke=True),
        q("docs", "How long is a password reset link valid?",
          [rf"\b{n(f['password_reset_expiry_minutes'])}[- ]minutes?\b"],
          [rf"\b{n(f['password_reset_expiry_minutes_v1'])}[- ]minutes?\b"], smoke=True),
        q("docs", "How long after delivery can I ask for a price match?",
          [days(f["price_match_window_days"])], [days(f["price_match_window_days_v1"])], smoke=True),
        q("docs", "How many Trail Rewards points do I earn per dollar?",
          [rf"\b{n(f['loyalty_points_per_dollar'])} point"]),
        q("docs", "How many points do I need for a reward, and what is it worth?",
          [rf"\b{f['loyalty_reward_points']}\b", lit(f["loyalty_reward_value"])]),
        q("docs", "When do Trail Rewards rewards expire?", [days(f["loyalty_reward_expiry_days"])]),
        q("docs", "How much do I need to spend to reach Gold tier?", [lit(f["gold_tier_spend"])]),
        q("docs", "What extra benefits do Gold tier members get?",
          [lit(f["gold_points_multiplier"]), r"express"]),
        q("docs", "Do Kestrel gift cards expire?",
          [r"never expire|do not expire|don't expire|no expiration"], smoke=True),
        q("docs", "What's the largest amount I can put on a gift card?", [lit(f["gift_card_max"])]),
        q("docs", "My order arrived damaged. How long do I have to report it?",
          [days(f["damaged_report_days"])]),
        q("docs", "How much is the deposit to rent a tent?", [lit(f["rental_deposit"])]),
        q("docs", "What is the late fee for returning rental gear late?", [lit(f["rental_late_fee"])]),
        q("docs", "How much do you charge to assess a repair on gear that's out of warranty?",
          [lit(f["repair_assessment_fee"])]),
        q("docs", "How long do I have to cancel an order after placing it?",
          [rf"\b{n(f['cancellation_window_hours'])}[- ]hours?\b"]),
        q("docs", "Can I pay for my order in installments?", [r"affirm", lit(f["affirm_minimum"])]),
        q("docs", "How many promo codes can I use on one order?",
          [rf"\b{n(f['promo_codes_per_order'])}\b"]),
        q("docs", "Do you have a student discount?", [rf"\b{f['student_discount_pct']}\s?(?:%|percent)"]),
        q("docs", "Is there a military discount?", [rf"\b{f['military_discount_pct']}\s?(?:%|percent)"]),
        q("docs", "What's the phone number for customer support?", [lit(f["support_phone"])]),
        q("docs", "How long will you hold my store pickup order?", [days(f["pickup_hold_days"])]),
        q("docs", "Should I order my usual size in Kestrel boots?",
          [r"half (?:a )?size (?:up|larger|bigger)"]),
        q("docs", "How much store credit can I get for trading in used gear?",
          [rf"\b{f['trade_in_max_pct']}\s?(?:%|percent)"]),
        q("docs", "I bought a gift on December 10. Until when can I return it?",
          [lit(f["holiday_return_deadline"])]),
        q("docs", "Can I return a jacket I bought at 60% off?", [r"final[- ]sale", NO]),
        q("docs", "Which camp stove has been recalled?", [r"RS-200"]),
    ]


def tool_questions(data: dict) -> list[dict]:
    names = {pid: name for pid, name, _ in data["products"]}
    items_by_order: dict[str, list[str]] = {}
    for oid, pid, _ in data["order_items"]:
        items_by_order.setdefault(oid, []).append(pid)
    refunded = {r[1]: r for r in data["refunds"]}
    orders = data["orders"]

    out = []
    # Order status: one order per status, plus one extra delivered.
    for status in ["delivered", "shipped", "processing", "cancelled", "delivered"]:
        order = next(o for o in orders if o[4] == status and o[0] not in {x["_order"] for x in out})
        out.append({**q("tools", f"I'm customer {order[1]}. What's the status of order {order[0]}?",
                        [rf"\b{status}\b"], smoke=len(out) == 0), "_order": order[0]})

    # Refund status: one per status.
    for status in ["requested", "approved", "processed", "denied"]:
        refund = next(r for r in data["refunds"] if r[2] == status)
        order = next(o for o in orders if o[0] == refund[1])
        out.append({**q("tools", f"I'm customer {order[1]}. What's the status of my refund for order {order[0]}?",
                        [rf"\b{status}\b"], smoke=status == "approved"), "_order": order[0]})

    # Order with no refund on file.
    order = next(o for o in orders if o[4] == "delivered" and o[0] not in refunded
                 and o[0] not in {x["_order"] for x in out})
    out.append({**q("tools", f"I'm customer {order[1]}. Has a refund been started for order {order[0]}?",
                    [NO]), "_order": order[0]})

    # Inventory: two in stock, two out of stock.
    in_stock = [(pid, qty) for pid, qty in data["inventory"] if qty > 0][:2]
    out_of_stock = [(pid, qty) for pid, qty in data["inventory"] if qty == 0][:2]
    for pid, qty in in_stock:
        out.append({**q("tools", f"Is the {names[pid]} (product {pid}) in stock?",
                        [YES, rf"\b{qty}\b"], [r"out of stock"], smoke=pid == in_stock[0][0]), "_order": None})
    for pid, _ in out_of_stock:
        out.append({**q("tools", f"Is the {names[pid]} (product {pid}) in stock?",
                        [NO], [YES]), "_order": None})

    # Order count for a customer.
    cid = "C-0005"
    count = sum(1 for o in orders if o[1] == cid)
    out.append({**q("tools", f"I'm customer {cid}. How many orders have I placed with you?",
                    [rf"\b{n(count)}\b"]), "_order": None})

    for x in out:
        x.pop("_order")
    return out


def multistep_questions(data: dict, f: dict) -> list[dict]:
    names = {pid: name for pid, name, _ in data["products"]}
    first_item = {}
    for oid, pid, _ in data["order_items"]:
        first_item.setdefault(oid, names[pid])
    delivered = [o for o in data["orders"] if o[4] == "delivered"]

    def age_days(order) -> int:
        return (REF_DATE - order[3]).days

    def pick(lo: int, hi: int, k: int, used: set) -> list:
        found = [o for o in delivered if lo <= age_days(o) <= hi and o[0] not in used][:k]
        assert len(found) == k, f"need {k} orders delivered {lo}-{hi} days ago, found {len(found)}"
        used.update(o[0] for o in found)
        return found

    used: set = set()
    refund_window = f["refund_window_days"]
    out = []

    def ask(order, text: str) -> str:
        return f"I'm customer {order[1]}. Today is {TODAY}. {text}"

    # Refund: clearly inside the window, and outside v2's window but inside v1's.
    for o in pick(1, refund_window - 4, 3, used):
        out.append(q("multistep", ask(o, f"Can I still return the {first_item[o[0]]} from order {o[0]} for a refund?"),
                     [YES], smoke=len(out) == 0))
    for o in pick(refund_window + 4, f["refund_window_days_v1"] - 1, 2, used):
        out.append(q("multistep", ask(o, f"Can I still return the {first_item[o[0]]} from order {o[0]} for a refund?"),
                     [NO], [YES], smoke=len(out) == 3))
    for o in pick(f["exchange_window_days"] + 10, 90, 1, used):
        out.append(q("multistep", ask(o, f"Can I still return the {first_item[o[0]]} from order {o[0]} for a refund?"),
                     [NO], [YES]))

    # Warranty: inside v2's 2 years but outside v1's 1 year, and outside both.
    years = 365
    for o in pick(f["warranty_years_v1"] * years + 10, f["warranty_years"] * years - 30, 1, used):
        out.append(q("multistep", ask(o, f"A seam failed on the {first_item[o[0]]} from order {o[0]}. Is it still under warranty?"),
                     [YES], [NO], smoke=True))
    for o in pick(f["warranty_years"] * years + 30, 5000, 1, used):
        out.append(q("multistep", ask(o, f"A seam failed on the {first_item[o[0]]} from order {o[0]}. Is it still under warranty?"),
                     [NO], [YES]))

    # Exchange: past the refund window but inside the exchange window, and past both.
    for o in pick(refund_window + 4, f["exchange_window_days"] - 2, 1, used):
        out.append(q("multistep", ask(o, f"Can I exchange the {first_item[o[0]]} from order {o[0]} for a different size?"),
                     [YES], [NO]))
    for o in pick(f["exchange_window_days"] + 10, 300, 1, used):
        out.append(q("multistep", ask(o, f"Can I exchange the {first_item[o[0]]} from order {o[0]} for a different size?"),
                     [NO], [YES]))
    return out


def main() -> None:
    facts = load_facts()
    data = generate()
    questions = doc_questions(facts) + tool_questions(data) + multistep_questions(data, facts)
    for i, item in enumerate(questions, start=1):
        item["id"] = f"q{i:03d}"
    questions = [{"id": x.pop("id"), **x} for x in questions]
    OUT.write_text(json.dumps(questions, indent=2) + "\n", encoding="utf-8")
    by_cat = {c: sum(1 for x in questions if x["category"] == c) for c in ("docs", "tools", "multistep")}
    print(f"wrote {len(questions)} questions {by_cat}, {sum(x['smoke'] for x in questions)} smoke")


if __name__ == "__main__":
    main()
