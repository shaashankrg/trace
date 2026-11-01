"""Run the investigator on recorded cases, check its evidence, test its fix, and score it.

For each case, a fresh MCP tool server starts with TRACEWISE_BACKEND=snapshot:<case_id>, the
investigator gets that case's alert.json, and then, in the evaluator:

  verifier  every citation is checked against the raw snapshot and the replay log
            (eval/verifier.py); a diagnosis with a bad one is `hallucinated` and scores as wrong
  fix       the diagnosis's fix_patch is applied in a FRESH replay (not the agent's own), with a
            fresh unpatched control; fix success = it matches the correction and the patch targets
            the ground-truth root component (F5)

Each case is saved to eval/results/<run_id>/<case_id>.json (diagnosis, verification, fix replay,
transcript, tokens, cost, wall time); replays go to eval/results/<run_id>/replays/.

Then the investigator is scored next to B1 and an earlier run (default: the Day 5 run), on the
valid cases and on every recorded case.

Usage: python -m eval.replay_case --agent investigator --cases all [--run-id ID] [--concurrency 2]
       python -m eval.replay_case --cases case-002 case-007
       python -m eval.replay_case --split train              # tune here
       python -m eval.replay_case --split test               # needs the lock and the frozen prompt
       python -m eval.replay_case --rescore --run-id ID      # rebuild summary.md from saved cases

Splits (Day 8). `--split train|dev|test` selects the cases of one split. The test split is
held out: `--split test` refuses to run unless eval/test_split.lock still matches the cases on
disk and the investigator prompt is the frozen one, and every test run appends a line to
eval/results/test_runs.log. Report dev separately and never as a held-out result.
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
from agent.llm import SPEND_CAP_USD, _record_spend, model_name, spent  # noqa: E402
from agent.schema import abstain  # noqa: E402
from eval import splits, verifier  # noqa: E402
from eval.baselines import run_b1  # noqa: E402
from eval.score import (RESULTS_DIR, load_truth, pct, proof_summary, score_cases, summarize,  # noqa: E402
                        write_results)
from tools import replay  # noqa: E402

RUNS_DIR = Path(__file__).resolve().parent / "runs"
CASE_TIMEOUT_S = 900  # the slowest clean Day 5 case took about 10 minutes
COMPARE_WITH = "investigator-day5-v2"


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


def replay_logs(out_dir: Path, case_id: str) -> list[Path]:
    return [out_dir / "replays" / f"{case_id}.jsonl", out_dir / "replays" / f"{case_id}.fix.jsonl"]


def fix_replay(case_dir: Path, diagnosis: dict, log: Path) -> dict | None:
    """Apply the diagnosis's fix_patch in a fresh replay. None when there is nothing to test."""
    patch = diagnosis.get("fix_patch")
    if diagnosis.get("abstained") or not patch:
        return None
    try:
        rec = replay.run_replay(replay.Snapshot(case_dir), patch, log, purpose="fix")
    except replay.PatchError as e:
        return {"error": f"patch rejected: {e}", "patch": patch, "matches_correction": False,
                "target_component": None, "cost_usd": 0.0}
    return {k: rec.get(k) for k in ("ref", "patch", "target_component", "matches_correction", "votes", "judge",
                                    "control_ref", "control_matches_correction", "proves_fix", "cost_usd")}


async def run_one(case_id: str, runs_dir: Path, out_dir: Path, model: str,
                  timeout_s: float = CASE_TIMEOUT_S) -> dict:
    case_dir = runs_dir / case_id
    alert = json.loads((case_dir / "alert.json").read_text(encoding="utf-8"))
    agent_log, fix_log = replay_logs(out_dir, case_id)
    env = {"TRACEWISE_BACKEND": f"snapshot:{case_id}", "TRACEWISE_RUNS_DIR": str(runs_dir),
           "TRACEWISE_REPLAY_LOG": str(agent_log), "TRACEWISE_REPLAY": "1"}
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
        state["diagnosis"] = abstain(f"investigation crashed: {error}", state.get("candidates"))
    wall = round(time.time() - started, 1)
    diagnosis = state.get("diagnosis") or abstain("no diagnosis", state.get("candidates"))

    check = verifier.verify(diagnosis, case_dir, [agent_log])
    fix = await asyncio.to_thread(fix_replay, case_dir, diagnosis, fix_log)
    agent_replays = replay.read_log(agent_log)
    replay_cost = sum(r["cost_usd"] for r in agent_replays) + ((fix or {}).get("cost_usd") or 0)
    replay_cost += sum(r["cost_usd"] for r in replay.read_log(fix_log) if r.get("purpose") == "control")
    _record_spend(replay_cost)

    pool = {e["ref"] for e in state.get("evidence", [])}
    cited = [e["ref"] for e in diagnosis.get("evidence", [])]
    usage = usage_totals(state.get("transcript", []))
    record = {
        "case_id": case_id, "model": model, "wall_s": wall, "error": error,
        "diagnosis": diagnosis, "verification": check, "fix": fix,
        "gate": state.get("gate"), "rounds": state.get("rounds", 0),
        "candidates": state.get("candidates", []),
        "tool_calls_used": state.get("tool_calls_used", 0),
        "replays_used": sum(1 for r in agent_replays if r.get("purpose") == "investigation"),
        "replay_judges": [r["judge"] for r in agent_replays if r.get("purpose") == "investigation"],
        "evidence_pool_size": len(pool), "refs_not_in_pool": [r for r in cited if r not in pool],
        **usage, "investigator_cost_usd": usage["cost_usd"], "replay_cost_usd": round(replay_cost, 6),
        "transcript": state.get("transcript", []),
    }
    record["cost_usd"] = round(usage["cost_usd"] + replay_cost, 6)
    (out_dir / f"{case_id}.json").write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")
    d = diagnosis
    tag = " (abstained)" if d.get("abstained") else " HALLUCINATED" if check["hallucinated"] else ""
    fixed = "" if fix is None else f" fix={'ok' if fix.get('matches_correction') else 'no'}"
    print(f"  {case_id}: {d.get('root_component') or (d.get('top_candidate') or {}).get('root_component')} / "
          f"{d.get('category') or (d.get('top_candidate') or {}).get('category')}{tag}{fixed}  "
          f"tools={record['tool_calls_used']} replays={record['replays_used']} rounds={record['rounds']} "
          f"${record['cost_usd']:.4f} {wall}s" + (f"  ERROR {error}" if error else ""), flush=True)
    return record


async def run_all(case_ids: list[str], runs_dir: Path, out_dir: Path, model: str, concurrency: int,
                  timeout_s: float = CASE_TIMEOUT_S) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)

    async def guarded(cid):
        async with sem:
            return await run_one(cid, runs_dir, out_dir, model, timeout_s)

    return await asyncio.gather(*(guarded(c) for c in case_ids))


def as_scorable(record: dict, truth: dict | None = None) -> dict:
    """One diagnosis for the scorer. Abstentions name nothing; hallucinated diagnoses keep their
    answer but `hallucinated` makes the scorer count them wrong."""
    d = record["diagnosis"]
    check = record.get("verification") or {}
    fix = record.get("fix") or {}
    out = {"case_id": record["case_id"], "abstained": bool(d.get("abstained")),
           "hallucinated": bool(check.get("hallucinated")),
           "root_component": None if d.get("abstained") else d.get("root_component"),
           "category": None if d.get("abstained") else d.get("category"),
           "cause_ref": None if d.get("abstained") else d.get("cause_ref"),
           "top_candidate": (d.get("top_candidate") or {}).get("root_component") if d.get("abstained") else None,
           "fix_tested": bool(fix), "fix_matches": bool(fix.get("matches_correction")),
           "fix_target": fix.get("target_component"), "fix_proves": bool(fix.get("proves_fix")),
           "replay_judges": record.get("replay_judges", []) + ([fix["judge"]] if fix.get("judge") else [])}
    return out


def load_records(out_dir: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(out_dir.glob("case-*.json"))]


def verify_earlier(records: list[dict], runs_dir: Path, out_dir: Path) -> list[dict]:
    """An earlier run's records, with the verifier applied now (Day 5 runs had none)."""
    for r in records:
        if "verification" not in r:
            r["verification"] = verifier.verify(r["diagnosis"], runs_dir / r["case_id"],
                                                replay_logs(out_dir, r["case_id"])[:1])
    return records


def comparison(run_id: str, records: list[dict], b1: dict[str, dict], earlier: tuple[str, list[dict]] | None,
               out_dir: Path) -> str:
    truth_all = [t for t in load_truth(include_invalid=True) if t["case_id"] in {r["case_id"] for r in records}]
    truth_valid = [t for t in truth_all if t["valid"]]
    cost = sum(r["cost_usd"] for r in records)
    wall = sum(r["wall_s"] for r in records) / max(len(records), 1)
    ours = {r["case_id"]: as_scorable(r) for r in records}
    methods = [("investigator", ours, cost)]
    if earlier:
        name, recs = earlier
        methods.append((name, {r["case_id"]: as_scorable(r) for r in recs}, sum(r["cost_usd"] for r in recs)))
    methods.append(("B1", {k: {**v, "abstained": False, "hallucinated": False} for k, v in b1.items()}, 0.0))

    lines = [f"# {run_id}: investigator vs. {earlier[0] + ' vs. ' if earlier else ''}B1", "",
             f"Investigator model `{records[0]['model'] if records else model_name()}`, {len(records)} cases, "
             f"total cost ${cost:.4f} (${cost / max(len(records), 1):.4f} per case, replays included), "
             f"mean wall time {wall:.0f}s. B1 makes no model calls.", "",
             "Top-1, category and cause count an abstention as no answer and a hallucinated diagnosis "
             "(any citation the verifier rejects) as wrong.", "",
             "| method | cases | n | top-1 | category | cause | precision when answered | abstain rate | "
             "abstention value | hallucinated evidence | fix success | judge fallback | cost |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for subset, truth in (("valid", truth_valid), ("all recorded", truth_all)):
        for method, diags, method_cost in methods:
            rows = score_cases(truth, diags)
            if not rows:
                continue
            s = {x["key"]: x for x in summarize(rows)}
            p = proof_summary(rows)
            lines.append(
                f"| {method} | {subset} | {len(rows)} | {pct(s['all']['top1'])} | {pct(s['all']['category'])} | "
                f"{pct(s['all']['cause'])} | {pct(p['precision'])} | {pct(p['abstain_rate'])} | "
                f"{pct(p['abstention_value'])} | {pct(p['hallucinated_rate'])} | {pct(p['fix_success'])} | "
                f"{pct(p['judge_fallback'])} | ${method_cost:.4f} |")
            tag = {"investigator": "investigator", "B1": "b1"}.get(method, "earlier")
            write_results(f"{run_id}_{tag}_{subset.split()[0]}", summarize(rows), rows, out_dir)
    lines += ["", "valid = the fault made the answer wrong when it was recorded; all recorded adds the cases "
              "recorded with `run_case --force` (fault applied, answer not shown to be wrong).",
              "precision when answered = correct / non-abstained. abstention value = of the abstained cases, "
              "how many had a wrong top candidate. hallucinated evidence = diagnoses with a citation the verifier "
              "rejects / non-abstained diagnoses. fix success = fresh replays of the fix_patch that match the "
              "correction with a patch targeting the true root component / non-abstained diagnoses. judge "
              "fallback = replays checked by the LLM judge / all replays.", "",
              "## Per case", "",
              "| case | fault | valid | decoy | truth | investigator | gate | verifier | fix replay | "
              "tools | replays | cost |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    by_case = {r["case_id"]: r for r in records}
    for t in truth_all:
        r = by_case[t["case_id"]]
        d, fix = r["diagnosis"], r.get("fix") or {}
        if d.get("abstained"):
            top = d.get("top_candidate") or {}
            said = f"abstained (top: {top.get('root_component')})"
        else:
            said = f"{d.get('root_component')} / {d.get('category')}"
        gate_txt = "passed" if (r.get("gate") or {}).get("passed") else f"failed x{r.get('rounds', 0)}"
        bad = r["verification"]["failures"]
        ver = "ok" if not bad else "; ".join(f"{f['ref']}: {f['problem']}" for f in bad)[:120]
        if not fix:
            fx = "none"
        elif fix.get("error"):
            fx = fix["error"][:60]
        else:
            fx = (f"{'fixed' if fix['matches_correction'] else 'not fixed'} via {fix['target_component']}"
                  + ("" if fix.get("proves_fix") or not fix["matches_correction"] else " (control also right)"))
        lines.append(f"| {t['case_id']} | {t['fault_type']} | {'yes' if t['valid'] else 'forced'} | "
                     f"{'yes' if t['has_decoy'] else ''} | {t['root_component']} / {t['category']} | {said} | "
                     f"{gate_txt} | {ver} | {fx} | {r['tool_calls_used']} | {r.get('replays_used', 0)} | "
                     f"${r['cost_usd']:.4f} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", choices=["investigator"], default="investigator")
    parser.add_argument("--cases", nargs="+", default=["all"])
    parser.add_argument("--split", choices=splits.SPLITS, default=None,
                        help="run one split; 'test' requires the lock and the frozen prompt")
    parser.add_argument("--note", default="", help="a note for the test-run log")
    parser.add_argument("--allow-unfrozen-prompt", action="store_true",
                        help="test split only: run without the frozen-prompt check (recorded in the log)")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    parser.add_argument("--model", default=None, help="default: INVESTIGATOR_MODEL or gemini-3.8-flash")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=CASE_TIMEOUT_S, help="seconds per case")
    parser.add_argument("--compare-with", default=COMPARE_WITH, help="an earlier run id ('' for none)")
    parser.add_argument("--rescore", action="store_true", help="only rebuild summary.md from saved cases")
    args = parser.parse_args()

    runs_dir = args.runs_dir.resolve()
    recorded = sorted(p.name for p in runs_dir.iterdir() if (p / "alert.json").exists())
    if args.split:
        if args.cases != ["all"]:
            parser.error("--split and --cases are mutually exclusive")
        wanted = splits.split_ids(args.split)
        case_ids = [c for c in wanted if c in recorded]
        missing = [c for c in wanted if c not in recorded]
        if missing:
            print(f"warning: {len(missing)} {args.split} cases have no recording yet: "
                  f"{', '.join(missing[:8])}{' ...' if len(missing) > 8 else ''}")
    elif args.cases == ["all"]:
        case_ids = recorded
    else:
        case_ids = args.cases
    if not case_ids:
        parser.error(f"no recorded cases in {runs_dir}")

    # The test split is held out: check the lock and the frozen prompt before spending anything.
    if args.split == "test" and not args.rescore:
        try:
            lock = splits.guard_test_run(case_ids, require_frozen_prompt=not args.allow_unfrozen_prompt)
        except splits.LockError as e:
            print(f"refusing to run the test split: {e}")
            return 2
        print(f"test-split lock ok ({lock['case_count']} cases, hash {lock['split_hash'][:12]}); "
              f"this will be test-split run #{splits.test_run_count() + 1}")
    model = args.model or model_name()
    if args.rescore and not args.run_id:
        parser.error("--rescore needs --run-id")
    run_id = args.run_id or f"investigator-{datetime.now():%Y%m%d-%H%M}"
    out_dir = RESULTS_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.rescore:
        records = [r for r in load_records(out_dir) if r["case_id"] in case_ids]
    else:
        print(f"{run_id}: {len(case_ids)} cases with {model}")
        records = asyncio.run(run_all(case_ids, runs_dir, out_dir, model, args.concurrency, args.timeout))
        print(f"spend so far: ${spent():.4f} of ${SPEND_CAP_USD:.2f}")
    (out_dir / "diagnoses.jsonl").write_text("".join(json.dumps(as_scorable(r)) + "\n" for r in records),
                                             encoding="utf-8")

    earlier = None
    if args.compare_with and (RESULTS_DIR / args.compare_with).is_dir():
        recs = [r for r in load_records(RESULTS_DIR / args.compare_with) if r["case_id"] in case_ids]
        earlier = (args.compare_with, verify_earlier(recs, runs_dir, RESULTS_DIR / args.compare_with))
    if args.split == "test" and not args.rescore:
        note = args.note or ("unfrozen prompt" if args.allow_unfrozen_prompt else "")
        print("test run logged: " + splits.log_test_run(run_id, case_ids, note))

    b1 = {d["case_id"]: d for d in run_b1(runs_dir) if d["case_id"] in case_ids}
    (out_dir / "summary.md").write_text(comparison(run_id, records, b1, earlier, out_dir), encoding="utf-8")
    print(f"\nwrote {out_dir / 'summary.md'}")
    print((out_dir / "summary.md").read_text(encoding="utf-8").split("## Per case")[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
