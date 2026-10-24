"""Run the investigator on recorded cases and score it next to B1.

For each case, a fresh MCP tool server starts with TRACEWISE_BACKEND=snapshot:<case_id>, the
investigator gets that case's alert.json, and the run is saved to
eval/results/<run_id>/<case_id>.json (diagnosis, candidates, transcript, tokens, cost, wall time).

Then both methods are scored twice: on the valid cases only (the fault really broke the answer)
and on every recorded case, including those recorded with `run_case --force`.

Usage: python -m eval.replay_case --agent investigator --cases all [--run-id ID] [--concurrency 2]
       python -m eval.replay_case --agent investigator --cases case-002 case-007
"""
import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from agent.graph import investigate_alert  # noqa: E402
from agent.llm import SPEND_CAP_USD, model_name, spent  # noqa: E402
from agent.schema import abstain  # noqa: E402
from eval.baselines import run_b1  # noqa: E402
from eval.score import RESULTS_DIR, load_truth, pct, score_cases, summarize, write_results  # noqa: E402

RUNS_DIR = Path(__file__).resolve().parent / "runs"
CASE_TIMEOUT_S = 900  # the slowest clean Day 5 case took about 10 minutes


def describe(e: BaseException) -> str:
    """The innermost real error: anyio wraps the MCP client's failures in ExceptionGroups."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    if isinstance(e, BaseExceptionGroup):
        return "; ".join(describe(x) for x in e.exceptions)
    return f"{type(e).__name__}: {e}"


def usage_totals(transcript: list[dict]) -> dict:
    llm = [e for e in transcript if e.get("kind") == "llm"]
    return {"llm_calls": len(llm),
            "input_tokens": sum(e["input_tokens"] for e in llm),
            "output_tokens": sum(e["output_tokens"] for e in llm),
            "thinking_tokens": sum(e["thinking_tokens"] for e in llm),
            "cost_usd": round(sum(e["cost_usd"] for e in llm), 6)}


async def run_one(case_id: str, runs_dir: Path, out_dir: Path, model: str,
                  timeout_s: float = CASE_TIMEOUT_S) -> dict:
    alert = json.loads((runs_dir / case_id / "alert.json").read_text(encoding="utf-8"))
    env = {"TRACEWISE_BACKEND": f"snapshot:{case_id}", "TRACEWISE_RUNS_DIR": str(runs_dir)}
    progress: dict = {}
    started = time.time()
    try:
        state = await asyncio.wait_for(
            investigate_alert(alert, env, model, progress=progress, errlog=out_dir / f"{case_id}.server.log"),
            timeout_s)
        error = None
    except Exception as e:  # a crashed investigation is a miss, not a crashed run; keep what it got to
        error = (f"TimeoutError: no diagnosis after {timeout_s:.0f}s" if isinstance(e, TimeoutError)
                 else describe(e))
        state = {"transcript": [], "evidence": [], "candidates": [], "tool_calls_used": 0, **progress}
        state["diagnosis"] = abstain(f"investigation crashed: {error}")
    wall = round(time.time() - started, 1)
    diagnosis = state.get("diagnosis") or abstain("no diagnosis")
    pool = {e["ref"] for e in state.get("evidence", [])}
    cited = [e["ref"] for e in diagnosis.get("evidence", [])]
    record = {
        "case_id": case_id, "model": model, "wall_s": wall, "error": error,
        "diagnosis": diagnosis, "candidates": state.get("candidates", []),
        "tool_calls_used": state.get("tool_calls_used", 0), "evidence_pool_size": len(pool),
        "refs_not_in_pool": [r for r in cited if r not in pool],
        **usage_totals(state.get("transcript", [])),
        "transcript": state.get("transcript", []),
    }
    (out_dir / f"{case_id}.json").write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")
    d = diagnosis
    print(f"  {case_id}: {d.get('root_component')} / {d.get('category')}"
          f"{' (abstained)' if d.get('abstained') else ''}  tools={record['tool_calls_used']} "
          f"${record['cost_usd']:.4f} {wall}s" + (f"  ERROR {error}" if error else ""), flush=True)
    return record


async def run_all(case_ids: list[str], runs_dir: Path, out_dir: Path, model: str, concurrency: int,
                  timeout_s: float = CASE_TIMEOUT_S) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)

    async def guarded(cid):
        async with sem:
            return await run_one(cid, runs_dir, out_dir, model, timeout_s)

    return await asyncio.gather(*(guarded(c) for c in case_ids))


def as_scorable(record: dict) -> dict:
    d = record["diagnosis"]
    if d.get("abstained"):
        return {"case_id": record["case_id"], "root_component": None, "category": None, "cause_ref": None}
    return {"case_id": record["case_id"], "root_component": d.get("root_component"),
            "category": d.get("category"), "cause_ref": d.get("cause_ref")}


def comparison(run_id: str, records: list[dict], b1: dict[str, dict], out_dir: Path) -> str:
    ours = {r["case_id"]: as_scorable(r) for r in records}
    truth_all = [t for t in load_truth(include_invalid=True) if t["case_id"] in ours]
    truth_valid = [t for t in truth_all if t["valid"]]
    cost = sum(r["cost_usd"] for r in records)
    wall = sum(r["wall_s"] for r in records) / max(len(records), 1)

    lines = [f"# {run_id}: investigator vs. B1", "",
             f"Investigator model `{records[0]['model'] if records else model_name()}`, {len(records)} cases, "
             f"total cost ${cost:.4f} (${cost / max(len(records), 1):.4f} per case), mean wall time {wall:.0f}s. "
             "B1 makes no model calls.", "",
             "| method | cases | n | top-1 | category | cause | top-1 with decoy | top-1 without decoy | cost |",
             "|---|---|---|---|---|---|---|---|---|"]
    for subset, truth in (("valid", truth_valid), ("all recorded", truth_all)):
        for method, diags, method_cost in (("investigator", ours, f"${cost:.4f}"), ("B1", b1, "$0")):
            rows = score_cases(truth, diags)
            s = {x["key"]: x for x in summarize(rows)} if rows else {}
            get = lambda k, m: pct(s[k][m]) if k in s and s[k]["n"] else "n/a"  # noqa: E731
            lines.append(f"| {method} | {subset} | {len(rows)} | {get('all', 'top1')} | {get('all', 'category')} | "
                         f"{get('all', 'cause')} | {get('with decoy', 'top1')} | {get('without decoy', 'top1')} | "
                         f"{method_cost} |")
            if rows:
                tag = f"{'investigator' if method == 'investigator' else 'b1'}_{subset.split()[0]}"
                write_results(f"{run_id}_{tag}", summarize(rows), rows, out_dir)
    lines += ["", "valid = the fault made the answer wrong when it was recorded; all recorded adds the cases "
              "recorded with `run_case --force` (fault applied, answer not shown to be wrong).", "",
              "## Per case", "",
              "| case | fault | valid | decoy | truth | investigator | B1 | tools | cost | refs not in pool |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    by_case = {r["case_id"]: r for r in records}
    for t in truth_all:
        r, o, b = by_case[t["case_id"]], ours[t["case_id"]], b1.get(t["case_id"], {})
        mark = lambda d: f"{d.get('root_component')} / {d.get('category')}"  # noqa: E731
        lines.append(f"| {t['case_id']} | {t['fault_type']} | {'yes' if t['valid'] else 'forced'} | "
                     f"{'yes' if t['has_decoy'] else ''} | {t['root_component']} / {t['category']} | {mark(o)} | "
                     f"{mark(b)} | {r['tool_calls_used']} | ${r['cost_usd']:.4f} | {len(r['refs_not_in_pool'])} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", choices=["investigator"], default="investigator")
    parser.add_argument("--cases", nargs="+", default=["all"])
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    parser.add_argument("--model", default=None, help="default: INVESTIGATOR_MODEL or gemini-3.8-flash")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=CASE_TIMEOUT_S, help="seconds per case")
    args = parser.parse_args()

    runs_dir = args.runs_dir.resolve()
    if args.cases == ["all"]:
        case_ids = sorted(p.name for p in runs_dir.iterdir() if (p / "alert.json").exists())
    else:
        case_ids = args.cases
    if not case_ids:
        parser.error(f"no recorded cases in {runs_dir}")
    model = args.model or model_name()
    run_id = args.run_id or f"investigator-{datetime.now():%Y%m%d-%H%M}"
    out_dir = RESULTS_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"{run_id}: {len(case_ids)} cases with {model}")
    records = asyncio.run(run_all(case_ids, runs_dir, out_dir, model, args.concurrency, args.timeout))
    print(f"spend so far: ${spent():.4f} of ${SPEND_CAP_USD:.2f}")
    (out_dir / "diagnoses.jsonl").write_text("".join(json.dumps(as_scorable(r)) + "\n" for r in records),
                                             encoding="utf-8")

    b1 = {d["case_id"]: d for d in run_b1(runs_dir) if d["case_id"] in case_ids}
    (out_dir / "summary.md").write_text(comparison(run_id, records, b1, out_dir), encoding="utf-8")
    print(f"\nwrote {out_dir / 'summary.md'}")
    print((out_dir / "summary.md").read_text(encoding="utf-8").split("## Per case")[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
