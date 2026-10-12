"""Inject one case, record everything an investigator could see, put the system back.

  1. reset_healthy(), smoke canary (abort under 90%), and check the alert question is answered
     correctly while healthy
  2. write the decoy (if any)
  3. apply the fault, write its change record (if any); the label stays in the eval schema
  4. ask the alert question; if the answer is not actually wrong the case is invalid
  5. snapshot trace, logs, metrics and DB state to eval/runs/<case_id>/
  6. revert, remove the decoy, smoke canary again and confirm recovery

Usage: python -m eval.run_case case-001 [case-002 ...]
       python -m eval.run_case --all
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from opensearchpy import OpenSearch

load_dotenv()

from assistant import db  # noqa: E402
from assistant.logs import INDEX_PREFIX, OPENSEARCH_URL  # noqa: E402
from eval import canary  # noqa: E402
from eval.setup_eval import connect_eval, connect_tools  # noqa: E402
from injector import decoys, faults, state  # noqa: E402

JAEGER_URL = os.environ.get("JAEGER_URL", "http://localhost:16686")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9092")
RUNS_DIR = Path(__file__).resolve().parent / "runs"
SMOKE_MIN_ACCURACY = 0.9
SCRAPE_WAIT_S = 60


class CaseAborted(Exception):
    """The system was not healthy enough to run the case; nothing was injected."""


def jsonable(value):
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


def dump(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, default=jsonable, ensure_ascii=False) + "\n", encoding="utf-8")


def http_json(url: str, timeout: int = 20):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def load_case(case_id: str) -> dict:
    with connect_eval() as conn:
        row = conn.execute("SELECT spec FROM eval.cases WHERE case_id = %s", (case_id,)).fetchone()
    if row is None:
        raise KeyError(f"no such case: {case_id} (run python -m injector.make_cases)")
    return row[0]


def healthy_state(conn, label: str) -> None:
    """Reset to healthy and prove it with the smoke canary. One retry: a single flaky answer
    drops a 14-question run to 92%, two to 86%."""
    for attempt in (1, 2):
        state.reset_healthy(conn)
        _, acc = canary.run_canary(smoke=True, verbose=False)
        print(f"  [{label}] smoke accuracy {acc:.3f}")
        if acc >= SMOKE_MIN_ACCURACY:
            return
    raise CaseAborted(f"smoke accuracy {acc:.3f} < {SMOKE_MIN_ACCURACY} after reset ({label})")


def ask_alert(case: dict) -> tuple[dict, list[str]]:
    resp = canary.ask(case["alert"]["question"], case["conversation_id"])
    return resp, canary.score(case["answer_check"], resp["answer"])


def snapshot(case: dict, resp: dict, window: tuple[datetime, datetime], case_start: datetime,
             out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    start, end = window

    # Jaeger: spans are exported in batches, so poll until the trace shows up.
    trace_id = resp["trace_id"]
    trace_json = None
    for _ in range(30):
        try:
            trace_json = http_json(f"{JAEGER_URL}/api/traces/{trace_id}")
            if trace_json.get("data"):
                break
        except Exception:
            pass
        time.sleep(1)
    dump(out / "trace.json", trace_json or {"data": []})

    # OpenSearch: logs in the alert window (the handler flushes every second).
    client = OpenSearch(OPENSEARCH_URL)
    try:
        client.indices.refresh(index=f"{INDEX_PREFIX}-*")
        hits = client.search(index=f"{INDEX_PREFIX}-*", body={
            "size": 10000, "sort": [{"timestamp": "asc"}],
            "query": {"range": {"timestamp": {"gte": start.isoformat(), "lte": end.isoformat()}}},
        })["hits"]["hits"]
        logs = [h["_source"] for h in hits]
    except Exception as e:
        print(f"  logs unavailable: {e}")
        logs = []
    dump(out / "logs.json", logs)

    # Prometheus: canary accuracy from just before this case began.
    def query_range(q: str) -> list:
        params = urllib.parse.urlencode({
            "query": q, "start": (case_start - timedelta(minutes=10)).timestamp(),
            "end": end.timestamp(), "step": 15})
        return http_json(f"{PROMETHEUS_URL}/api/v1/query_range?{params}")["data"]["result"]

    try:
        metrics = {"kestrel_canary_accuracy": query_range("kestrel_canary_accuracy"),
                   "kestrel_canary_run_timestamp": query_range("kestrel_canary_run_timestamp")}
    except Exception as e:
        print(f"  metrics unavailable: {e}")
        metrics = {}
    dump(out / "metrics.json", metrics)

    # Database state, read as the `tools` role: exactly what the investigator's tools can see.
    with connect_tools() as conn:
        def rows(sql: str, params: tuple = ()) -> list[dict]:
            cur = conn.execute(sql, params)
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

        dump(out / "changes.json", rows("SELECT * FROM changes ORDER BY created_at DESC, change_id"))
        dump(out / "documents.json", rows("""
            SELECT d.doc_id, d.version, d.title, d.published_at, d.body,
                   count(c.chunk_id) AS chunk_count,
                   count(c.chunk_id) FILTER (WHERE c.indexed) AS indexed_chunks
            FROM documents d LEFT JOIN chunks c USING (doc_id, version)
            GROUP BY d.doc_id, d.version, d.title, d.published_at, d.body
            ORDER BY d.doc_id, d.version"""))
        dump(out / "config.json", rows("SELECT key, value, updated_at FROM app_config ORDER BY key"))
        dump(out / "prompt_versions.json", rows("""
            SELECT p.version, p.template, p.created_at, p.author, p.note,
                   (p.version = (SELECT value FROM app_config WHERE key = 'prompt_version')) AS active
            FROM prompt_versions p ORDER BY p.created_at"""))
        dump(out / "tool_io.json", rows(
            """SELECT conversation_id, trace_id, span_id, tool_name, input, output, timed_out,
                      latency_ms, created_at
               FROM tool_calls WHERE conversation_id = %s ORDER BY id""", (case["conversation_id"],)))

    # No ground truth in here: only what the alert would carry in production.
    dump(out / "alert.json", {
        "alert_id": f"alert-{case['case_id']}",
        "type": case["alert"]["type"],
        "fired_at": window[1].isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "conversation_id": case["conversation_id"],
        "trace_id": trace_id,
        "question": case["alert"]["question"],
        "answer": resp["answer"],
        "reviewer_correction": case["alert"]["reviewer_correction"],
    })


def wait_for_scrape(after: float) -> None:
    """Block until Prometheus has scraped a canary push made after `after` (unix time)."""
    deadline = time.time() + SCRAPE_WAIT_S
    q = urllib.parse.quote('max(kestrel_canary_run_timestamp{suite="smoke"})')
    while time.time() < deadline:
        try:
            result = http_json(f"{PROMETHEUS_URL}/api/v1/query?query={q}")["data"]["result"]
            if result and float(result[0]["value"][1]) >= after:
                return
        except Exception:
            pass
        time.sleep(3)
    print("  warning: Prometheus did not scrape the fault-time canary in time")


def record_run(case_id: str, conversation_id: str, valid: bool, note: str, resp: dict | None) -> None:
    with connect_eval() as conn:
        conn.execute(
            """INSERT INTO eval.case_runs (case_id, valid, note, conversation_id, trace_id, answer)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (case_id, valid, note, conversation_id, (resp or {}).get("trace_id"),
             (resp or {}).get("answer")))


def run_case(case_id: str, fault_canary: bool = True) -> dict:
    case = load_case(case_id)
    case_start = datetime.now(timezone.utc)
    print(f"{case_id}: {case['fault']['type']}" + (f" + {case['decoy']['type']}" if case["decoy"] else ""))
    result = {"case_id": case_id, "valid": False, "recovered": False, "note": ""}

    with db.connect() as conn:
        # 1. healthy baseline
        healthy_state(conn, "reset")
        healthy_resp, healthy_failures = ask_alert(case)
        if healthy_failures:
            result["note"] = f"invalid: the alert question already fails while healthy: {healthy_failures}"
            print("  " + result["note"])
            record_run(case_id, case["conversation_id"], False, result["note"], healthy_resp)
            return result

        now = datetime.now(timezone.utc)
        decoy = decoys.build(conn, case["decoy"], now) if case["decoy"] else None
        fault = faults.build(conn, case["fault"], now)
        resp = None
        try:
            # 2-3. inject. Oldest change first, so the history reads in the order it happened.
            window_start = datetime.now(timezone.utc)
            for item in sorted(filter(None, [decoy, fault]), key=lambda x: x.created_at):
                item.apply()
                record = item.change_record()
                if record:
                    state.insert_change(conn, record)

            # 4. the alert
            resp, failures = ask_alert(case)
            result["answer"] = resp["answer"]
            if not failures:
                result["note"] = "invalid: the fault did not make the answer wrong"
                print(f"  {result['note']}\n    answer: {resp['answer'][:200]!r}")
                record_run(case_id, case["conversation_id"], False, result["note"], resp)
                return result
            result["valid"] = True
            print(f"  wrong answer confirmed: {failures}")

            # 5. snapshot, with a fault-time canary so the metrics show the drop
            if fault_canary:
                _, acc = canary.run_canary(smoke=True, verbose=False)
                print(f"  [fault] smoke accuracy {acc:.3f}")
                wait_for_scrape(time.time() - 5)
            time.sleep(3)  # let the log handler flush
            window = (window_start, datetime.now(timezone.utc))
            snapshot(case, resp, window, case_start, RUNS_DIR / case_id)
            print(f"  snapshot saved to eval/runs/{case_id}")
        finally:
            # 6. put it back
            fault.revert()
            if decoy:
                decoy.revert()

        healthy_state(conn, "recovery")
        result["recovered"] = True
        record_run(case_id, case["conversation_id"], True, "ok", resp)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("case_ids", nargs="*")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--no-fault-canary", action="store_true",
                        help="skip the canary run while the fault is active (metrics.json will show no drop)")
    args = parser.parse_args()

    if args.all:
        with connect_eval() as conn:
            ids = [r[0] for r in conn.execute("SELECT case_id FROM eval.cases ORDER BY case_id")]
    else:
        ids = args.case_ids
    if not ids:
        parser.error("give case ids or --all")

    results = []
    for case_id in ids:
        try:
            results.append(run_case(case_id, not args.no_fault_canary))
        except CaseAborted as e:
            print(f"  aborted: {e}")
            results.append({"case_id": case_id, "valid": False, "recovered": False, "note": f"aborted: {e}"})
        except Exception as e:  # keep going; the next case starts with a reset anyway
            print(f"  error: {type(e).__name__}: {e}")
            results.append({"case_id": case_id, "valid": False, "recovered": False, "note": f"error: {e}"})

    ok = [r for r in results if r["valid"] and r["recovered"]]
    print(f"\n{len(ok)}/{len(results)} cases valid and recovered")
    for r in results:
        if r not in ok:
            print(f"  {r['case_id']}: {r['note']}")
    return 0 if len(ok) == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
