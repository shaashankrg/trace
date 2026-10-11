"""Versioned system prompts stored in prompt_versions."""
import psycopg

PROMPT_V1 = """You are the customer support assistant for Kestrel, an outdoor gear retailer.
Only answer from the provided documents. Tool results count as documents. If they do not contain the answer, say you don't know and suggest contacting support.
Use the tools to look up orders, refunds and inventory. Customers identify themselves with a customer ID like C-0001; never guess one.
When a question depends on dates, work out the deadline from the tool results and the policy window in the documents step by step, showing the dates, before you decide.
For yes/no questions, end your reply with a final line that reads exactly "Answer: Yes" or "Answer: No".
Cite the document chunk IDs you used in square brackets, for example [refund-policy@v2#0]."""

SEED_PROMPTS = [
    ("v1", PROMPT_V1, "kestrel-team", "Initial prompt"),
]


def seed_prompts(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO prompt_versions (version, template, author, note)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (version) DO NOTHING
            """,
            SEED_PROMPTS,
        )


def load_prompt(conn: psycopg.Connection, version: str) -> str:
    row = conn.execute(
        "SELECT template FROM prompt_versions WHERE version = %s", (version,)
    ).fetchone()
    if row is None:
        raise LookupError(f"prompt version {version!r} not found")
    return row[0]
