"""Day 7 re-record sanity check: do snapshots hold everything the live system shows?

For each case: inject it again (eval.run_case, writing to eval/results/live-check/runs/, never to
eval/runs/, and leaving eval.case_runs alone), and while the fault is still applied run the
investigator against the LIVE stack (Jaeger, OpenSearch, Postgres, Prometheus). After the revert,
run it on the fresh snapshot of that same recording. If the snapshot recorder misses something,
the two verdicts part ways.

Replay needs a recording, so the live investigation cannot replay; its gate has to pass on two
evidence types. The snapshot run can.

Usage: python -m eval.live_check case-002 case-007 case-009
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from agent.graph import investigate_alert  # noqa: E402
from agent.llm import model_name  # noqa: E402
from eval.replay_case import run_one  # noqa: E402
from eval import spend  # noqa: E402
from eval.run_case import run_case  # noqa: E402
from eval.score import RESULTS_DIR  # noqa: E402

OUT = RESULTS_DIR / "live-check"


def verdict(diagnosis: dict) -> str:
    if diagnosis.get("abstained"):
        return f"abstained (top: {(diagnosis.get('top_candidate') or {}).get('root_component')})"
    return f"{diagnosis.get('root_component')} / {diagnosis.get('category')}"


def investigate_live(case: dict, case_dir: Path) -> dict:
    alert = json.loads((case_dir / "alert.json").read_text(encoding="utf-8"))
    state = asyncio.run(investigate_alert(alert, {"TRACEWISE_BACKEND": "live"}, errlog=OUT / f"{case['case_id']}.live.server.log"))
    record = {"diagnosis": state.get("diagnosis"), "gate": state.get("gate"),
              "tool_calls_used": state.get("tool_calls_used"), "transcript": state.get("transcript", [])}
    (OUT / f"{case['case_id']}.live.json").write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")
    return record


def earlier_verdicts(case_id: str, runs: list[str]) -> list[str]:
    out = []
    for run in runs:
        path = RESULTS_DIR / run / f"{case_id}.json"
        if path.exists():
            out.append(verdict(json.loads(path.read_text(encoding="utf-8"))["diagnosis"]))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("case_ids", nargs="+")
    parser.add_argument("--compare-runs", nargs="*", default=["investigator-day7-r1", "investigator-day7-r2",
                                                              "investigator-day7-r3"])
    args = parser.parse_args()
    spend.install()
    runs_dir = OUT / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    model = model_name()

    rows = []
    for case_id in args.case_ids:
        result = run_case(case_id, runs_dir=runs_dir, while_faulted=investigate_live, record=False, force=True)
        live = (result.get("while_faulted") or {}).get("diagnosis") or {}
        snap = asyncio.run(run_one(case_id, runs_dir, OUT, model))
        rows.append({"case_id": case_id, "valid": result["valid"], "note": result.get("note", ""),
                     "live": verdict(live), "snapshot": verdict(snap["diagnosis"]),
                     "eval_snapshot_runs": earlier_verdicts(case_id, args.compare_runs)})
        print(f"{case_id}: live {rows[-1]['live']} | snapshot {rows[-1]['snapshot']} | "
              f"eval runs {rows[-1]['eval_snapshot_runs']}", flush=True)

    lines = ["# Live re-record check", "",
             "Each case injected again; the investigator ran on the live stack while the fault was active, then on "
             "the snapshot of that same recording, next to its verdicts on the evaluation snapshot (eval/runs).", "",
             "| case | answer broken this time | live | fresh snapshot | eval snapshot (3 runs) | match |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        same = r["live"] == r["snapshot"]
        lines.append(f"| {r['case_id']} | {'yes' if r['valid'] else 'no: ' + r['note'][:50]} | {r['live']} | "
                     f"{r['snapshot']} | {', '.join(r['eval_snapshot_runs']) or 'n/a'} | {'yes' if same else '**no**'} |")
    (OUT / "live_check.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "live_check.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
