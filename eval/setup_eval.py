"""Create the `eval` schema and the `eval` / `tools` roles.

Ground truth lives in eval.*, owned by the `eval` role. The `tools` role (what the
investigator's MCP tools connect as) can read the assistant's tables and nothing in eval,
so the anti-cheating rule is enforced by Postgres instead of by good intentions.

Usage: python -m eval.setup_eval
"""
import os

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from assistant import db

EVAL_PASSWORD = os.environ.get("EVAL_DB_PASSWORD", "eval")
TOOLS_PASSWORD = os.environ.get("TOOLS_DB_PASSWORD", "tools")

EVAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval.cases (
  case_id      TEXT PRIMARY KEY,
  fault_type   TEXT,
  has_decoy    BOOLEAN,
  spec         JSONB,          -- the case as the injector reads it
  ground_truth JSONB           -- root_component, category, cause_ref
);

-- One row per run_case execution; `valid` is false when the fault did not break the answer.
CREATE TABLE IF NOT EXISTS eval.case_runs (
  id              BIGSERIAL PRIMARY KEY,
  case_id         TEXT REFERENCES eval.cases,
  valid           BOOLEAN,
  note            TEXT,
  conversation_id TEXT,
  trace_id        TEXT,
  answer          TEXT,
  ran_at          TIMESTAMPTZ DEFAULT now()
);
"""


def role_url(user: str, password: str) -> str:
    parts = conninfo_to_dict(db.DATABASE_URL)
    parts.update(user=user, password=password)
    return make_conninfo(**parts)


def connect_as(user: str, password: str) -> psycopg.Connection:
    return psycopg.connect(role_url(user, password), autocommit=True)


def connect_eval() -> psycopg.Connection:
    return connect_as("eval", EVAL_PASSWORD)


def connect_tools() -> psycopg.Connection:
    return connect_as("tools", TOOLS_PASSWORD)


def _ensure_role(conn: psycopg.Connection, name: str, password: str) -> None:
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone()
    verb = "ALTER" if exists else "CREATE"
    conn.execute(sql.SQL(f"{verb} ROLE {{}} LOGIN PASSWORD {{}}").format(
        sql.Identifier(name), sql.Literal(password)))


def setup(conn: psycopg.Connection) -> None:
    """Run as the database owner (the app's own connection)."""
    dbname = conn.execute("SELECT current_database()").fetchone()[0]
    _ensure_role(conn, "eval", EVAL_PASSWORD)
    _ensure_role(conn, "tools", TOOLS_PASSWORD)
    conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO eval, tools").format(sql.Identifier(dbname)))

    conn.execute("CREATE SCHEMA IF NOT EXISTS eval AUTHORIZATION eval")
    conn.execute("ALTER SCHEMA eval OWNER TO eval")
    conn.execute("REVOKE ALL ON SCHEMA eval FROM PUBLIC")
    conn.execute(EVAL_SCHEMA)
    for table in ("cases", "case_runs"):
        conn.execute(f"ALTER TABLE eval.{table} OWNER TO eval")
    conn.execute("ALTER SEQUENCE eval.case_runs_id_seq OWNER TO eval")
    # tools gets no grant on eval at all, and the public schema is read-only for it.
    conn.execute("REVOKE ALL ON SCHEMA eval FROM tools")
    conn.execute("REVOKE ALL ON ALL TABLES IN SCHEMA eval FROM tools")

    conn.execute("GRANT USAGE ON SCHEMA public TO tools, eval")
    conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO tools")
    # Needed so the eval role can look at the assistant's tables when taking snapshots.
    conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO eval")


def main() -> None:
    with db.connect() as conn:
        db.init_schema(conn)
        setup(conn)
    print("eval schema and roles ready")


if __name__ == "__main__":
    main()
