"""Score diagnoses against the ground truth in the eval schema.

A diagnosis is one JSON object per line: {case_id, root_component, category, cause_ref?}, plus
optionally the Day 6 fields: abstained, hallucinated (from eval/verifier.py; scored as wrong even when
the component is right), top_candidate (what an abstention would have said), and the fresh fix
replay (fix_tested, fix_matches, fix_target, fix_proves).
Only cases whose latest run was valid (the fault really broke the answer) are scored; a
valid case with no diagnosis counts as wrong. --include-invalid also scores cases recorded with
`run_case --force`: the fault was applied, but the answer it produced was not shown to be wrong.

Writes eval/results/<run>.md (tables), <run>.csv (the summary) and <run>_cases.csv (one row per case).

Usage: python -m eval.score eval/diagnoses/b1.jsonl [--run b1]
"""
import argparse
import csv
import json
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def load_diagnoses(path: Path) -> dict[str, dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {r["case_id"]: r for r in rows}


def load_truth(include_invalid: bool = False) -> list[dict]:
    from eval.setup_eval import connect_eval
    with connect_eval() as conn:
        rows = conn.execute("""
            SELECT c.case_id, c.fault_type, c.has_decoy, c.ground_truth, last.valid
            FROM eval.cases c
            JOIN LATERAL (SELECT r.valid FROM eval.case_runs r WHERE r.case_id = c.case_id
                          ORDER BY r.id DESC LIMIT 1) last ON TRUE
            WHERE last.valid OR %s
            ORDER BY c.case_id""", (include_invalid,)).fetchall()
    return [{"case_id": cid, "fault_type": ft, "has_decoy": decoy, "valid": valid, **gt}
            for cid, ft, decoy, gt, valid in rows]


def score_cases(truth: list[dict], diagnoses: dict[str, dict]) -> list[dict]:
    rows = []
    for t in truth:
        d = diagnoses.get(t["case_id"], {})
        ok = not d.get("hallucinated")  # a diagnosis citing evidence that is not there is wrong
        component = d.get("root_component") == t["root_component"]
        rows.append({
            "case_id": t["case_id"], "fault_type": t["fault_type"], "has_decoy": t["has_decoy"],
            "truth_component": t["root_component"], "pred_component": d.get("root_component"),
            "component_correct": ok and component,
            "truth_category": t["category"], "pred_category": d.get("category"),
            "category_correct": ok and d.get("category") == t["category"],
            "truth_cause": t["cause_ref"], "pred_cause": d.get("cause_ref"),
            "cause_correct": ok and bool(d.get("cause_ref")) and d.get("cause_ref") == t["cause_ref"],
            "answered": bool(d) and not d.get("abstained") and d.get("root_component") is not None,
            "abstained": bool(d.get("abstained")),
            "hallucinated": bool(d.get("hallucinated")),
            "component_correct_unverified": component,
            "top_candidate_correct": d.get("top_candidate") == t["root_component"],
            "fix_success": bool(d.get("fix_matches")) and d.get("fix_target") == t["root_component"],
            "fix_proven": bool(d.get("fix_proves")) and d.get("fix_target") == t["root_component"],
            "replay_judges": "|".join(d.get("replay_judges") or []),
        })
    return rows


def proof_summary(rows: list[dict]) -> dict:
    """The Day 6 metrics. Rates over an empty group are NaN (printed n/a)."""
    answered = [r for r in rows if r["answered"]]
    abstained = [r for r in rows if r["abstained"]]
    judges = [j for r in rows for j in r["replay_judges"].split("|") if j]
    div = lambda a, b: a / b if b else float("nan")  # noqa: E731
    return {
        "answered": len(answered), "abstained": len(abstained), "replays": len(judges),
        "hallucinated_rate": div(sum(r["hallucinated"] for r in answered), len(answered)),
        "fix_success": div(sum(r["fix_success"] for r in answered), len(answered)),
        "fix_proven": div(sum(r["fix_proven"] for r in answered), len(answered)),
        "abstain_rate": div(len(abstained), len(rows)),
        "precision": div(sum(r["component_correct"] for r in answered), len(answered)),
        "abstention_value": div(sum(not r["top_candidate_correct"] for r in abstained), len(abstained)),
        "judge_fallback": div(sum(j == "llm" for j in judges), len(judges)),
    }


def rate(rows: list[dict], key: str) -> float:
    return sum(r[key] for r in rows) / len(rows) if rows else float("nan")


def summarize(rows: list[dict]) -> list[dict]:
    """Summary rows: group, key, n, top1 (root component), category, cause."""
    def line(group: str, key: str, subset: list[dict]) -> dict:
        return {"group": group, "key": key, "n": len(subset), "top1": rate(subset, "component_correct"),
                "category": rate(subset, "category_correct"), "cause": rate(subset, "cause_correct")}

    out = [line("overall", "all", rows)]
    out += [line("decoy", "with decoy" if flag else "without decoy", [r for r in rows if r["has_decoy"] == flag])
            for flag in (True, False)]
    out += [line("fault_type", ft, [r for r in rows if r["fault_type"] == ft])
            for ft in sorted({r["fault_type"] for r in rows})]
    return out


def pct(x: float) -> str:
    return "n/a" if x != x else f"{x:.0%}"


def markdown(run: str, summary: list[dict], rows: list[dict]) -> str:
    def table(group: str, title: str) -> str:
        lines = [f"### {title}", "", "| | n | top-1 | category | cause |", "|---|---|---|---|---|"]
        lines += [f"| {s['key']} | {s['n']} | {pct(s['top1'])} | {pct(s['category'])} | {pct(s['cause'])} |"
                  for s in summary if s["group"] == group]
        return "\n".join(lines)

    wrong = [r for r in rows if not r["component_correct"]]
    lines = [f"# Results: {run}", "",
             "top-1 = root component named correctly; category = kind of cause named correctly; "
             "cause = the exact change or outage named (n/a when the method does not name one).", "",
             table("overall", "Overall"), "", table("decoy", "With vs. without a decoy"), "",
             table("fault_type", "Per fault type"), "",
             "### Misses", "", "| case | fault | truth | predicted |", "|---|---|---|---|"]
    lines += [f"| {r['case_id']} | {r['fault_type']} | {r['truth_component']} | {r['pred_component']} |"
              for r in wrong]
    return "\n".join(lines) + "\n"


def write_results(run: str, summary: list[dict], rows: list[dict], out_dir: Path = RESULTS_DIR) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{run}.md").write_text(markdown(run, summary, rows), encoding="utf-8")
    for name, data in ((run, summary), (f"{run}_cases", rows)):
        with (out_dir / f"{name}.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(data[0]))
            writer.writeheader()
            writer.writerows(data)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("diagnoses", type=Path)
    parser.add_argument("--run", help="result name (default: the diagnoses file name)")
    parser.add_argument("--include-invalid", action="store_true",
                        help="also score cases recorded with run_case --force")
    args = parser.parse_args()

    run = args.run or args.diagnoses.stem
    rows = score_cases(load_truth(args.include_invalid), load_diagnoses(args.diagnoses))
    if not rows:
        raise SystemExit("no valid cases to score; run python -m eval.run_case --all first")
    summary = summarize(rows)
    write_results(run, summary, rows)
    top = summary[0]
    print(f"{run}: {top['n']} cases, top-1 {pct(top['top1'])}, category {pct(top['category'])}")
    print(f"wrote {RESULTS_DIR / (run + '.md')}")


if __name__ == "__main__":
    main()
