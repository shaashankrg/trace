"""Shared helpers: baseline change history, config/index/prompt writes, reset_healthy()."""
import difflib
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

from assistant.config import reset_config
from assistant.ingest import DOCS_DIR, latest_versions, parse
from assistant.prompts import PROMPT_V1, SEED_PROMPTS
from assistant.store_seed import generate

DECOY_DOCS_DIR = Path(__file__).parent / "decoy_docs"
NL = chr(10)
AUTHORS = ["alice", "ben", "priya", "marco", "ops-bot"]

# Matches ingest.latest_versions: newest published_at wins, version breaks ties.
HEALTHY_INDEX_SQL = """
UPDATE chunks c SET indexed = (
  c.version = (SELECT d.version FROM documents d WHERE d.doc_id = c.doc_id
               ORDER BY d.published_at DESC, d.version DESC LIMIT 1)
)
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def change_slug(kind: str) -> str:
    return kind.replace("_", "-")


def unified(old: str, new: str, name: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{name}", tofile=f"b/{name}", n=1))


def change_record(change_id, kind, target, description, diff, author, created_at) -> dict:
    return {"change_id": change_id, "kind": kind, "target": target, "description": description,
            "diff": diff, "author": author, "created_at": created_at}


def insert_change(conn: psycopg.Connection, rec: dict) -> None:
    conn.execute(
        """
        INSERT INTO changes (change_id, kind, target, description, diff, author, created_at)
        VALUES (%(change_id)s, %(kind)s, %(target)s, %(description)s, %(diff)s, %(author)s, %(created_at)s)
        ON CONFLICT (change_id) DO UPDATE SET kind = EXCLUDED.kind, target = EXCLUDED.target,
          description = EXCLUDED.description, diff = EXCLUDED.diff, author = EXCLUDED.author,
          created_at = EXCLUDED.created_at
        """,
        rec,
    )


def baseline_changes(docs_dir: Path = DOCS_DIR) -> list[dict]:
    """One doc_update per published doc version, dated by its published_at. Pure: reads
    only the doc files, so ids are stable and the injector can name them as a cause."""
    parsed = [parse(p) for p in sorted(docs_dir.glob("*.md"))]
    by_doc: dict[str, list[tuple[dict, str]]] = {}
    for meta, body in parsed:
        by_doc.setdefault(meta["doc_id"], []).append((meta, body))

    ordered = sorted(parsed, key=lambda mb: (mb[0]["published_at"], mb[0]["doc_id"], mb[0]["version"]))
    rng = random.Random(7)
    out = []
    for i, (meta, body) in enumerate(ordered):
        earlier = sorted((m["published_at"], b) for m, b in by_doc[meta["doc_id"]]
                         if m["published_at"] < meta["published_at"])
        if earlier:
            diff = unified(earlier[-1][1] + NL, body + NL, meta["doc_id"])
        else:
            diff = NL.join(f"+{line}" for line in body.splitlines()) + NL
        out.append(change_record(
            f"change:doc-update-{1000 + i}", "doc_update", f"doc:{meta['doc_id']}",
            f"Publish {meta['doc_id']}@{meta['version']}", diff, rng.choice(AUTHORS),
            datetime.fromisoformat(meta["published_at"]).replace(hour=9, tzinfo=timezone.utc),
        ))
    return out


def baseline_change_id(doc_id: str, version: str) -> str:
    for rec in baseline_changes():
        if rec["target"] == f"doc:{doc_id}" and rec["description"].endswith(f"@{version}"):
            return rec["change_id"]
    raise KeyError(f"{doc_id}@{version}")


def set_config(conn: psycopg.Connection, key: str, value: str, at: datetime | None = None) -> None:
    """Upsert one app_config key. `at` backdates updated_at; None leaves it untouched on update."""
    if at is None:
        conn.execute(
            """INSERT INTO app_config (key, value) VALUES (%s, %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
            (key, value),
        )
    else:
        conn.execute(
            """INSERT INTO app_config (key, value, updated_at) VALUES (%s, %s, %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at""",
            (key, value, at),
        )


def get_config(conn: psycopg.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_config WHERE key = %s", (key,)).fetchone()
    return row[0] if row else None


def restore_index_flags(conn: psycopg.Connection, doc_id: str | None = None) -> None:
    if doc_id is None:
        conn.execute(HEALTHY_INDEX_SQL)
    else:
        conn.execute(HEALTHY_INDEX_SQL + " WHERE c.doc_id = %s", (doc_id,))


def next_prompt_version(conn: psycopg.Connection) -> str:
    versions = [r[0] for r in conn.execute("SELECT version FROM prompt_versions").fetchall()]
    nums = [int(v[1:]) for v in versions if v[1:].isdigit()]
    return f"v{max(nums, default=0) + 1}"


def add_prompt_version(conn, template: str, author: str, note: str, at: datetime) -> str:
    version = next_prompt_version(conn)
    conn.execute(
        "INSERT INTO prompt_versions (version, template, created_at, author, note) VALUES (%s,%s,%s,%s,%s)",
        (version, template, at, author, note),
    )
    return version


def drop_prompt_version(conn: psycopg.Connection, version: str, restore_to: str) -> None:
    """Idempotent: re-point the active prompt only if it still is `version`, then delete it."""
    conn.execute("UPDATE app_config SET value = %s WHERE key = 'prompt_version' AND value = %s",
                 (restore_to, version))
    if version not in {v for v, *_ in SEED_PROMPTS}:
        conn.execute("DELETE FROM prompt_versions WHERE version = %s", (version,))


def reset_inventory(conn: psycopg.Connection, product_id: str | None = None) -> None:
    for pid, qty in generate()["inventory"]:
        if product_id and pid != product_id:
            continue
        conn.execute(
            """INSERT INTO inventory_source (product_id, qty, updated_at) VALUES (%s, %s, now())
               ON CONFLICT (product_id) DO UPDATE SET qty = EXCLUDED.qty, updated_at = now()""",
            (pid, qty))
        conn.execute(
            """INSERT INTO inventory_cache (product_id, qty, refreshed_at) VALUES (%s, %s, now())
               ON CONFLICT (product_id) DO UPDATE SET qty = EXCLUDED.qty, refreshed_at = now()""",
            (pid, qty))


def decoy_doc_ids() -> list[str]:
    return [p.name.split("@")[0] for p in sorted(DECOY_DOCS_DIR.glob("*.md"))]


def reset_healthy(conn: psycopg.Connection) -> None:
    """Put config, prompts, index flags, inventory and change history back to the healthy state."""
    reset_config(conn)

    conn.execute("DELETE FROM prompt_versions WHERE version <> ALL(%s)", ([v for v, *_ in SEED_PROMPTS],))
    conn.execute("UPDATE prompt_versions SET template = %s, created_at = now() WHERE version = 'v1'",
                 (PROMPT_V1,))

    ids = decoy_doc_ids()
    conn.execute("DELETE FROM chunks WHERE doc_id = ANY(%s)", (ids,))
    conn.execute("DELETE FROM documents WHERE doc_id = ANY(%s)", (ids,))
    restore_index_flags(conn)

    reset_inventory(conn)

    conn.execute("DELETE FROM changes")
    for rec in baseline_changes():
        insert_change(conn, rec)
