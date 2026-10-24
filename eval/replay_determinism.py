"""Replay every recorded case with an empty patch and check it reproduces the original.

A replay can only prove a fix if, with nothing changed, it gives back the answer that was
flagged. Per case this compares, against the recording:

  verdict    the answer check's failures (eval.canary.score) are the same as the original answer's
  retrieval  the same chunk ids, in the same order, as the recorded `retrieve` span
  tools      the same tool calls, in order, as the recorded trace
  text       the exact answer text (reported, not required: the check is what replay relies on)

Each replay takes several samples of the assistant (tools.replay.SAMPLES). A case reproduces when
the majority of samples gives the original verdict and every sample retrieves and calls the same
as the recording; `samples_reproducing` shows how many of the samples did.

Usage: python -m eval.replay_determinism [case-001 ...] [--out eval/results/determinism] [--workers 4]
"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from eval.canary import score  # noqa: E402
from eval.score import RESULTS_DIR  # noqa: E402
from tools import replay  # noqa: E402

RUNS_DIR = Path(__file__).resolve().parent / "runs"


def recorded(snap: replay.Snapshot) -> tuple[list[str], list[str]]:
    """(chunk ids of the retrieve span, tool names in call order) of the alert's trace."""
    trace = next(t for t in snap.trace["data"] if t["traceID"] == snap.alert["trace_id"])
    spans = sorted(trace["spans"], key=lambda s: s["startTime"])
    chunk_ids: list[str] = []
    for s in spans:
        if s["operationName"] == "retrieve":
            tag = next((t["value"] for t in s["tags"] if t["key"] == "kestrel.retrieval.chunk_ids"), "[]")
            chunk_ids = json.loads(tag) if isinstance(tag, str) else list(tag)
    tools = [s["operationName"].removeprefix("tool.") for s in spans if s["operationName"].startswith("tool.")]
    return chunk_ids, tools


def check_case(case_dir: Path, out: Path) -> dict:
    snap = replay.Snapshot(case_dir)
    rec = replay.run_replay(snap, {}, out / f"{snap.case_id}.jsonl", judge=None, purpose="determinism")
    chunk_ids, tools = recorded(snap)
    check = snap.alert["answer_check"]
    original = score(check, snap.alert["answer"])
    same_verdict = [score(check, s["answer"]) == original for s in rec["samples"]]
    same_retrieval = [s["retrieved"] == chunk_ids for s in rec["samples"]]
    same_tools = [[c["tool"] for c in s["tools"]] == tools for s in rec["samples"]]
    row = {
        "case_id": snap.case_id,
        "verdict": sum(same_verdict) * 2 > len(same_verdict),
        "retrieval": all(same_retrieval),
        "tools": all(same_tools),
        "text": any(s["answer"].strip() == snap.alert["answer"].strip() for s in rec["samples"]),
        "samples_reproducing": sum(v and r and t for v, r, t in zip(same_verdict, same_retrieval, same_tools)),
        "samples": len(rec["samples"]),
        "original_failures": original,
        "replay_failures": [score(check, s["answer"]) for s in rec["samples"]],
        "replay_ref": rec["ref"], "cost_usd": rec["cost_usd"],
    }
    row["reproduces"] = row["verdict"] and row["retrieval"] and row["tools"]
    if not row["retrieval"]:
        row["retrieval_detail"] = {"recorded": chunk_ids, "replay": [s["retrieved"] for s in rec["samples"]]}
    if not row["tools"]:
        row["tools_detail"] = {"recorded": tools, "replay": [[c["tool"] for c in s["tools"]] for s in rec["samples"]]}
    if not row["verdict"]:
        row["answers"] = {"recorded": snap.alert["answer"][:400], "replay": rec["answer"][:400]}
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("case_ids", nargs="*")
    parser.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    parser.add_argument("--out", type=Path, default=RESULTS_DIR / "determinism")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    ids = args.case_ids or sorted(p.name for p in args.runs_dir.iterdir() if (p / "alert.json").exists())
    args.out.mkdir(parents=True, exist_ok=True)

    with ThreadPoolExecutor(args.workers) as pool:
        rows = list(pool.map(lambda c: check_case(args.runs_dir / c, args.out), ids))

    (args.out / "determinism.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    lines = ["| case | reproduces | samples | verdict | retrieval | tools | exact text |",
             "|---|---|---|---|---|---|---|"]
    mark = lambda b: "yes" if b else "**no**"  # noqa: E731
    for r in rows:
        lines.append(f"| {r['case_id']} | {mark(r['reproduces'])} | {r['samples_reproducing']}/{r['samples']} | "
                     f"{mark(r['verdict'])} | {mark(r['retrieval'])} | "
                     f"{mark(r['tools'])} | {'yes' if r['text'] else 'no'} |")
    ok = sum(r["reproduces"] for r in rows)
    lines += ["", f"{ok}/{len(rows)} reproduce (majority of samples); "
              f"{sum(r['samples_reproducing'] for r in rows)}/{sum(r['samples'] for r in rows)} samples reproduce; "
              f"{sum(r['text'] for r in rows)}/{len(rows)} cases with a word-for-word sample; "
              f"cost ${sum(r['cost_usd'] for r in rows):.4f}"]
    (args.out / "determinism.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    for r in rows:
        if not r["reproduces"]:
            print(f"\n{r['case_id']}: " + json.dumps({k: r[k] for k in r if k.endswith("detail") or k == "answers"}))
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
