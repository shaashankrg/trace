"""Baseline B1: blame whatever span has an ERROR status, otherwise blame the model.

It reads only the recorded trace, no change history, config, logs or tool output. Most of the
faults leave no error span (a swallowed tool timeout is an OK span; a stale document retrieves
fine), so B1 should score low. If it scores high, the faults are too loud.

Usage: python -m eval.baselines b1 [--out eval/diagnoses/b1.jsonl]
"""
import argparse
import json
from pathlib import Path

RUNS_DIR = Path(__file__).resolve().parent / "runs"
DIAGNOSES_DIR = Path(__file__).resolve().parent / "diagnoses"

# What the category of a blamed component would be, since B1 has nothing better to go on.
CATEGORY = {"retriever": "config_change", "guardrail": "config_change", "model": "model_change"}


def component_for(operation: str) -> str | None:
    if operation == "retrieve":
        return "retriever"
    if operation.startswith("tool."):
        return f"tool:{operation[len('tool.'):]}"
    if operation == "guardrail":
        return "guardrail"
    if operation == "llm.chat":
        return "model"
    return None  # the FastAPI root span and anything else says nothing about a component


def is_error(span: dict) -> bool:
    tags = {t["key"]: t["value"] for t in span.get("tags", [])}
    return tags.get("error") in (True, "true") or tags.get("otel.status_code") == "ERROR"


def b1_diagnose(trace: dict) -> dict:
    spans = sorted((s for t in trace.get("data", []) for s in t["spans"]), key=lambda s: s["startTime"])
    for span in spans:
        component = component_for(span["operationName"])
        if component and is_error(span):
            category = "tool_outage" if component.startswith("tool:") else CATEGORY[component]
            return {"root_component": component, "category": category, "cause_ref": None,
                    "reason": f"span {span['operationName']} has status ERROR"}
    # No error anywhere: the only thing left to blame is the model hallucinating.
    return {"root_component": "model", "category": "model_change", "cause_ref": None,
            "reason": "no ERROR span"}


def run_b1(runs_dir: Path = RUNS_DIR) -> list[dict]:
    out = []
    for case_dir in sorted(runs_dir.glob("case-*")):
        trace_path = case_dir / "trace.json"
        if not trace_path.exists():
            continue
        diagnosis = b1_diagnose(json.loads(trace_path.read_text(encoding="utf-8")))
        out.append({"case_id": case_dir.name, "method": "b1", **diagnosis})
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", choices=["b1"])
    parser.add_argument("--out", type=Path, default=DIAGNOSES_DIR / "b1.jsonl")
    args = parser.parse_args()

    diagnoses = run_b1()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(json.dumps(d) + "\n" for d in diagnoses), encoding="utf-8")
    print(f"wrote {len(diagnoses)} diagnoses to {args.out}")


if __name__ == "__main__":
    main()
