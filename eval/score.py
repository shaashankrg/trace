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
            # Top-3 over the ranked candidate list. A method that ranks nothing (B1, B2) has only
            # its one answer, so for those top-3 degenerates to top-1 rather than being blank.
            "top3_correct": ok and t["root_component"] in (
                (d.get("ranked_components") or ([d["root_component"]] if d.get("root_component") else []))[:3]),
            "cost_usd": d.get("cost_usd"),
            "wall_s": d.get("wall_s"),
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


def mean(rows: list[dict], key: str) -> float:
    """The average of a numeric field over the rows that carry it. B1 has no cost or wall time
    (it is a heuristic over a recorded trace), so a missing value is absent, not zero."""
    values = [r[key] for r in rows if r.get(key) is not None]
    return sum(values) / len(values) if values else float("nan")


def headline(rows: list[dict]) -> dict:
    """One row per method for the comparison table: accuracy next to the three things that make
    the investigator different from a model that just answers (verified evidence, a proven fix,
    and not being fooled by a decoy)."""
    proof = proof_summary(rows)
    with_decoy = [r for r in rows if r["has_decoy"]]
    without = [r for r in rows if not r["has_decoy"]]
    return {
        "n": len(rows),
        "top1": rate(rows, "component_correct"),
        "top3": rate(rows, "top3_correct"),
        "category": rate(rows, "category_correct"),
        "hallucinated_rate": proof["hallucinated_rate"],
        "fix_success": proof["fix_success"],
        "decoy_with": rate(with_decoy, "component_correct"),
        "decoy_without": rate(without, "component_correct"),
        # Decoy resistance: how much accuracy survives a red herring. 100% means the decoy cost
        # nothing; below 100% is the share of accuracy it took away.
        "decoy_resistance": (rate(with_decoy, "component_correct") / rate(without, "component_correct")
                             if without and rate(without, "component_correct") else float("nan")),
        "abstain_rate": proof["abstain_rate"],
        "cost_usd": mean(rows, "cost_usd"),
        "wall_s": mean(rows, "wall_s"),
    }


def comparison(by_method: dict[str, list[dict]]) -> str:
    """The results table: one line per method, investigator first."""
    head = ("| method | n | top-1 | top-3 | category | hallucinated evidence | fix success "
            "(replay) | decoy resistance | abstained | $/case | s/case |")
    lines = [head, "|" + "---|" * 11]
    for name, rows in by_method.items():
        if not rows:
            continue
        h = headline(rows)
        lines.append(
            f"| {name} | {h['n']} | {pct(h['top1'])} | {pct(h['top3'])} | {pct(h['category'])} "
            f"| {pct(h['hallucinated_rate'])} | {pct(h['fix_success'])} "
            f"| {pct(h['decoy_resistance'])} | {pct(h['abstain_rate'])} "
            f"| {num(h['cost_usd'], '$', 4)} | {num(h['wall_s'], '', 1)} |")
    return "\n".join(lines)


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


def num(x: float, prefix: str = "", places: int = 2) -> str:
    """A number, or n/a when the method does not have one (B1 costs nothing and calls no model)."""
    return "n/a" if x != x else f"{prefix}{x:.{places}f}"


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
             "### Headline", "", comparison({run: rows}), "",
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


def compare_methods(specs: list[str], include_invalid: bool = False,
                    restrict_to: list[str] | None = None) -> tuple[dict[str, list[dict]], str]:
    """Score several methods against the same truth and build the comparison table.

    `specs` are `name=path/to/diagnoses.jsonl`, investigator first. Every method is scored over the
    same case set, so a method that is missing a case scores it as unanswered rather than having it
    quietly dropped from its denominator - otherwise a baseline that crashed on the hard cases
    would look better than one that tried them.
    """
    truth = load_truth(include_invalid)
    if restrict_to is not None:
        keep = set(restrict_to)
        truth = [t for t in truth if t["case_id"] in keep]
    by_method = {}
    for spec in specs:
        name, _, path = spec.partition("=")
        if not path:
            raise SystemExit(f"--compare takes name=path, got {spec!r}")
        by_method[name] = score_cases(truth, load_diagnoses(Path(path)))
    return by_method, comparison(by_method)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("diagnoses", type=Path, nargs="?")
    parser.add_argument("--run", help="result name (default: the diagnoses file name)")
    parser.add_argument("--include-invalid", action="store_true",
                        help="also score cases recorded with run_case --force")
    parser.add_argument("--compare", nargs="+", metavar="NAME=PATH",
                        help="score several methods over the same cases into one table")
    parser.add_argument("--split", help="restrict to one split's cases (e.g. test)")
    parser.add_argument("--out", type=Path, help="write the comparison table here")
    args = parser.parse_args()

    if args.compare:
        restrict = None
        if args.split:
            from eval import splits
            restrict = (splits.lockable_test_ids() if args.split == "test"
                        else splits.split_ids(args.split))
        by_method, table = compare_methods(args.compare, args.include_invalid, restrict)
        print(table)
        for name, rows in by_method.items():
            if rows:
                write_results(name, summarize(rows), rows)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(table + "\n", encoding="utf-8")
            print(f"\nwrote {args.out}")
        return

    if args.diagnoses is None:
        raise SystemExit("pass a diagnoses file, or --compare name=path ...")
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
