"""Deterministic store data: 30 customers, 50 products, 80 orders.

generate() is pure (fixed RNG seed), so eval/make_canary.py can derive the
expected tool answers without querying the database.
"""
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import psycopg

# Date the canary questions say "today" is; order dates are laid out around it.
REF_DATE = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)

PRODUCT_NAMES = [
    "Ridgeline Hiking Boot", "Summit GTX Boot", "Trailrunner Low Shoe", "Alpine Mountaineering Boot",
    "Canyon Sandal", "Stormshell Rain Jacket", "Cirrus Down Jacket", "Talus Fleece Pullover",
    "Basecamp Insulated Parka", "Switchback Softshell", "Merino Base Layer Top", "Merino Base Layer Bottom",
    "Trek Hiking Pants", "Scree Convertible Pants", "Wool Hiking Socks 3-Pack", "Sunrise 2-Person Tent",
    "Highland 4-Person Tent", "Ultralight 1-Person Tent", "Aspen 20F Sleeping Bag", "Glacier 0F Sleeping Bag",
    "Featherlite Sleeping Pad", "Comfort Plus Sleeping Pad", "Daybreak 28L Daypack", "Traverse 55L Backpack",
    "Expedition 70L Backpack", "Hydration Reservoir 2L", "Trekking Poles Pair", "Headlamp 400 Lumen",
    "Camp Lantern", "Titanium Cook Pot", "Backpacking Stove", "Water Filter Pump",
    "Insulated Water Bottle", "Camp Chair", "Folding Camp Table", "Bear Canister",
    "Trail First Aid Kit", "Rain Fly Tarp", "Compression Stuff Sack", "Gaiters",
    "Snowshoes", "Microspikes Traction", "Climbing Harness", "Chalk Bag",
    "Packable Sun Hat", "Insulated Gloves", "Beanie", "Duffel Bag 60L",
    "Kayak Paddle", "Dry Bag 20L",
]
FIRST_NAMES = [
    "Avery", "Jordan", "Riley", "Casey", "Morgan", "Quinn", "Rowan", "Sage", "Skyler", "Emerson",
    "Harper", "Reese", "Dakota", "Finley", "Hayden", "Parker", "Kendall", "Logan", "Peyton", "Cameron",
    "Elliot", "Jamie", "Taylor", "Alexis", "Blake", "Drew", "Jesse", "Kai", "Marley", "Remy",
]
LAST_NAMES = [
    "Alvarez", "Brooks", "Chen", "Diaz", "Evans", "Foster", "Garcia", "Hughes", "Ito", "Johnson",
    "Kim", "Lopez", "Martin", "Nguyen", "Okafor", "Patel", "Quinn", "Reyes", "Singh", "Turner",
    "Usman", "Vega", "Walsh", "Xu", "Young", "Zhang", "Bennett", "Carter", "Dunn", "Ellis",
]
# Days before REF_DATE that delivered orders arrived: a mix of inside and outside policy windows.
DELIVERY_OFFSETS = [2, 4, 6, 9, 11, 13, 20, 26, 35, 45, 60, 120, 200, 380, 500, 800]
REFUND_STATUSES = ["requested", "approved", "processed", "denied"]


def generate() -> dict[str, list[tuple]]:
    rng = random.Random(42)

    products = [
        (f"P-{i:03d}", name, Decimal(rng.randrange(1500, 45000, 500)) / 100)
        for i, name in enumerate(PRODUCT_NAMES, start=1)
    ]
    price = {pid: p for pid, _, p in products}

    customers = []
    for i in range(1, 31):
        first, last = FIRST_NAMES[i - 1], LAST_NAMES[i - 1]
        customers.append((f"C-{i:04d}", f"{first} {last}", f"{first.lower()}.{last.lower()}@example.com"))

    statuses = ["delivered"] * 60 + ["shipped"] * 10 + ["processing"] * 6 + ["cancelled"] * 4
    rng.shuffle(statuses)

    orders, order_items = [], []
    for n, status in enumerate(statuses):
        order_id = f"O-{1001 + n}"
        customer_id = customers[n % 30][0] if n < 30 else rng.choice(customers)[0]
        delivered_at = None
        if status == "delivered":
            delivered_at = REF_DATE - timedelta(days=rng.choice(DELIVERY_OFFSETS), hours=rng.randint(0, 8))
            placed_at = delivered_at - timedelta(days=rng.randint(3, 6))
        elif status == "shipped":
            placed_at = REF_DATE - timedelta(days=rng.randint(2, 4))
        elif status == "processing":
            placed_at = REF_DATE - timedelta(hours=rng.randint(26, 40))
        else:
            placed_at = REF_DATE - timedelta(days=rng.randint(10, 90))

        picks = rng.sample(products, rng.randint(1, 3))
        items = [(order_id, pid, rng.randint(1, 2)) for pid, _, _ in picks]
        total = sum(price[pid] * qty for _, pid, qty in items)
        orders.append((order_id, customer_id, placed_at, delivered_at, status, total))
        order_items.extend(items)

    delivered = [o for o in orders if o[4] == "delivered"]
    refunds = []
    for k, order in enumerate(rng.sample(delivered, 16)):
        refunds.append((
            f"R-{5001 + k}", order[0], REFUND_STATUSES[k % 4],
            order[3] + timedelta(days=rng.randint(1, 10)),
        ))

    inventory = []
    for pid, _, _ in products:
        qty = 0 if rng.random() < 0.16 else rng.randint(1, 40)
        inventory.append((pid, qty))

    return {
        "customers": customers,
        "products": products,
        "orders": orders,
        "order_items": order_items,
        "refunds": refunds,
        "inventory": inventory,
    }


def seed_store(conn: psycopg.Connection) -> None:
    data = generate()
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "TRUNCATE refunds, order_items, orders, inventory_cache, inventory_source, products, customers"
        )
        cur.executemany("INSERT INTO customers VALUES (%s, %s, %s)", data["customers"])
        cur.executemany("INSERT INTO products VALUES (%s, %s, %s)", data["products"])
        cur.executemany("INSERT INTO orders VALUES (%s, %s, %s, %s, %s, %s)", data["orders"])
        cur.executemany("INSERT INTO order_items VALUES (%s, %s, %s)", data["order_items"])
        cur.executemany("INSERT INTO refunds VALUES (%s, %s, %s, %s)", data["refunds"])
        cur.executemany(
            "INSERT INTO inventory_source (product_id, qty) VALUES (%s, %s)", data["inventory"]
        )
        cur.executemany(
            "INSERT INTO inventory_cache (product_id, qty) VALUES (%s, %s)", data["inventory"]
        )
