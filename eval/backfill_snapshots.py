"""Add the two Day 6 snapshot fields to cases recorded before Day 6, without re-recording them.

  alert.json   gains `answer_check`, copied from the case spec (the reviewer's correction as patterns)
  store.json   the store tables as they were while the fault was active

The store is rebuilt, not guessed: in one transaction that is rolled back, the live DB is reset
to healthy, the case's decoy and fault are applied with the original run's timestamps, and the
store tables are dumped. The store seed is deterministic and only stale_tool_data touches the
store, so this is what run_case would have written. Each rebuilt store is checked against the
tool outputs recorded in tool_io.json.

Usage: python -m eval.backfill_snapshots [--runs-dir eval/runs] [case-001 ...]
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv()

from assistant import db  # noqa: E402
from eval.run_case import RUNS_DIR, dump, jsonable, load_case, store_rows  # noqa: E402
from injector import decoys, faults, state  # noqa: E402
from tools.replay import Store  # noqa: E402


def rebuild_store(case: dict, fault_time: datetime) -> dict:
    with db.connect() as conn:
        result = {}
        try:
            with conn.transaction():
                state.reset_healthy(conn)
                items = [faults.build(conn, case["fault"], fault_time)]
                if case["decoy"]:
                    items.append(decoys.build(conn, case["decoy"], fault_time))
                for item in sorted(items, key=lambda x: x.created_at):
                    item.apply()
                result = json.loads(json.dumps(store_rows(conn), default=jsonable))
                raise psycopg.Rollback()
        except psycopg.Rollback:
            pass
    return result


def check_against_recording(case_dir: Path, store: dict) -> list[str]:
    """Every non-timed-out tool call recorded for the alert's trace must come out the same."""
    alert = json.loads((case_dir / "alert.json").read_text(encoding="utf-8"))
    calls = json.loads((case_dir / "tool_io.json").read_text(encoding="utf-8"))
    s = Store(store, now=alert["fired_at"])
    problems = []
    for c in calls:
        if c["trace_id"] != alert["trace_id"] or c["timed_out"]:
            continue
        got = s.run(c["tool_name"], c["input"])
        if c["tool_name"] == "check_inventory":  # refreshed_at moves with the rebuild time; qty must not
            got = [{k: v for k, v in r.items() if k != "refreshed_at"} for r in got]
            want = [{k: v for k, v in r.items() if k != "refreshed_at"} for r in c["output"]]
        else:
            want = c["output"]
        if json.loads(json.dumps(got)) != want:
            problems.append(f"{c['tool_name']}({c['input']}): store gives {got!r:.200}, recorded {want!r:.200}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("case_ids", nargs="*")
    parser.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    args = parser.parse_args()
    ids = args.case_ids or sorted(p.name for p in args.runs_dir.iterdir() if (p / "alert.json").exists())

    bad = 0
    for case_id in ids:
        case_dir = args.runs_dir / case_id
        case = load_case(case_id)
        alert_path = case_dir / "alert.json"
        alert = json.loads(alert_path.read_text(encoding="utf-8"))
        alert["answer_check"] = case["answer_check"]
        dump(alert_path, alert)

        fault_time = datetime.fromisoformat(alert["window"]["start"])
        store = rebuild_store(case, fault_time)
        dump(case_dir / "store.json", store)
        problems = check_against_recording(case_dir, store)
        bad += bool(problems)
        print(f"{case_id}: store.json {sum(len(v) for v in store.values())} rows"
              + ("" if not problems else "\n  " + "\n  ".join(problems)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
