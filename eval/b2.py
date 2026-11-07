"""Baseline B2: the same model as the investigator, one shot, no tools.

Why this baseline exists. B1 (blame the ERROR span) only shows the agent beats a heuristic that
barely tries. B2 is the honest comparison: it holds the model fixed and removes *the
investigation*. It gets, in a single prompt, the three things a developer would paste into a chat
window when an answer looks wrong:

  - the conversation: question, the wrong answer, the reviewer's correction
  - the text of the chunks retrieval actually returned
  - the recent change list, decoys included, exactly as `recent_changes` returns it

What it cannot do is look anything up: no document history to see that an older version is still
indexed, no logs to catch a tool that timed out and returned nothing, no config on the root span,
no replay to prove a fix. So the gap between B2 and the investigator is the value of the tools and
the verification gate, not of the model. If B2 scores close to the investigator, the tools are not
earning their place and that is worth knowing.

Fairness rules, so the comparison means something:
  - same model and max_tokens as the investigator (`INVESTIGATOR_MODEL`), temperature left alone
  - same component/category vocabulary, and the same wording for it, from agent/schema.py
  - decoys are present and unlabelled, as they are for the investigator
  - it sees the question and correction, which the investigator also sees in its alert
  - no ground truth, no fault labels: the snapshot files it reads carry none

Usage: python -m eval.b2 [--out eval/diagnoses/b2.jsonl] [--split test] [--cases case-021,...]
"""
import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

# Before importing agent.llm: it reads the model, the spend cap and the API key at import time, so
# a late load would leave B2 on different settings than the investigator it is compared against.
load_dotenv()

from agent import llm  # noqa: E402
from agent.schema import CATEGORIES, COMPONENTS  # noqa: E402

RUNS_DIR = Path(__file__).resolve().parent / "runs"
DIAGNOSES_DIR = Path(__file__).resolve().parent / "diagnoses"

MAX_CHUNK_CHARS = 700          # a retrieved chunk, truncated the way the real tool truncates
MAX_CHANGES = 30               # what recent_changes returns by default
MAX_SAMPLES = 3                # failing samples shown for a quality_drop alert

SYSTEM = """You are an incident investigator for Kestrel's customer support assistant, a \
retrieval-augmented chatbot. One of its answers was wrong. Name the component that caused it.

## How the assistant works

One question goes through: `guardrail` (topic filter) -> `retrieve` (top-k document chunks above a
similarity threshold) -> `llm.chat` (the model, which may call store tools) -> `tool.<name>` (store
lookups) -> `llm.chat` again with the tool results. Documents have versions; only indexed chunks
can be retrieved.

## What you are given

The conversation, the text of the chunks retrieval returned, and the list of changes made shortly
before the failure. You have no tools and cannot look anything else up. Some of the changes listed
are innocent and unrelated to the failure; nothing marks which.

## How to judge

- The final answer is usually the victim, not the cause. Consider what was retrieved and what the
  changes did before blaming the model.
- If the model was given correct, sufficient inputs and still answered wrongly, blame the model.
- A change shortly before the failure is a suspect, not a culprit.

## Vocabulary

root_component, one of:
{components}

category, one of:
{categories}

## Output

Reply with one JSON object and nothing else:

{{"root_component": "...", "category": "...", "cause_ref": "<change_id or chunk_id that is the \
cause, or null>", "reason": "<one or two sentences>"}}

For a tool, write `tool:<name>` using the tool's name. `cause_ref` must be copied exactly from \
what you were given, or be null; never invent one."""


def _vocabulary() -> str:
    components = "\n".join(f"- {k}: {v}" for k, v in COMPONENTS.items())
    categories = "\n".join(f"- {k}: {v}" for k, v in CATEGORIES.items())
    return SYSTEM.format(components=components, categories=categories)


def retrieved_chunk_ids(trace: dict) -> list[str]:
    """The chunk ids the retrieve span recorded, in the order retrieval ranked them."""
    for data in trace.get("data", []):
        for span in data.get("spans", []):
            if span.get("operationName") != "retrieve":
                continue
            for tag in span.get("tags", []):
                if tag["key"] == "kestrel.retrieval.chunk_ids":
                    value = tag["value"]
                    if isinstance(value, str):
                        try:
                            value = json.loads(value)
                        except ValueError:
                            return []
                    return list(value or [])
    return []  # the guardrail can block before retrieval ever runs


def _read(case_dir: Path, name: str, default):
    path = case_dir / name
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _truncate(text: str, limit: int = MAX_CHUNK_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "..."


def build_prompt(case_dir: Path) -> str:
    """The one user turn B2 gets. Reads only files the investigator's tools also read from."""
    alert = _read(case_dir, "alert.json", {})
    trace = _read(case_dir, "trace.json", {})
    chunks = {c["chunk_id"]: c for c in _read(case_dir, "chunks.json", [])}
    changes = _read(case_dir, "changes.json", [])

    parts = [f"## Alert ({alert.get('type', 'unknown')})", ""]
    if alert.get("type") == "quality_drop":
        parts += [f"A quality metric fell: {alert.get('metric', 'canary accuracy')} "
                  f"{alert.get('before')} -> {alert.get('after')}.", ""]
        for i, s in enumerate(alert.get("failing_samples", [])[:MAX_SAMPLES], 1):
            parts += [f"Failing sample {i}:",
                      f"  question: {s.get('question', '')}",
                      f"  answer: {s.get('answer', '')}",
                      f"  correction: {s.get('reviewer_correction', '')}", ""]
    if alert.get("question"):
        parts += [f"Question: {alert['question']}",
                  f"Answer given: {alert.get('answer', '')}",
                  f"Reviewer's correction: {alert.get('reviewer_correction', '')}", ""]

    ids = retrieved_chunk_ids(trace)
    parts += [f"## Chunks retrieval returned ({len(ids)})", ""]
    if not ids:
        parts += ["(none: retrieval returned nothing, or never ran)", ""]
    for cid in ids:
        chunk = chunks.get(cid)
        if chunk is None:
            parts += [f"- {cid}: (text not recorded)"]
        else:
            parts += [f"- {cid} (doc {chunk.get('doc_id')} {chunk.get('version')}): "
                      f"{_truncate(chunk.get('text', ''))}"]
    parts += [""]

    shown = changes[:MAX_CHANGES]
    parts += [f"## Changes shortly before the failure ({len(shown)} of {len(changes)})", ""]
    for ch in shown:
        line = (f"- {ch.get('change_id')} [{ch.get('kind')}] target={ch.get('target')} "
                f"by {ch.get('author')} at {ch.get('created_at')}: {ch.get('description', '')}")
        parts += [line]
        if ch.get("diff"):
            parts += [f"    diff: {_truncate(ch['diff'], 400)}"]
    parts += ["", "Name the root component and category as JSON."]
    return "\n".join(parts)


def b2_diagnose(case_dir: Path, model: str | None = None) -> dict:
    """One model call, no tools. A reply that is not usable JSON is a wrong answer, not a crash:
    a baseline that cannot answer in the required shape has failed that case on its own terms."""
    chat = llm.Chat(system=_vocabulary(), json_mode=True, model=model)
    reply = chat.send(build_prompt(case_dir))
    usage = {"cost_usd": round(reply.usage.cost(chat.model), 6),
             "input_tokens": reply.usage.input_tokens,
             "output_tokens": reply.usage.output_tokens}
    try:
        out = llm.parse_json(reply.text)
        if not isinstance(out, dict):
            raise ValueError("reply was not a JSON object")
    except ValueError as e:
        return {"root_component": None, "category": None, "cause_ref": None,
                "reason": f"unparseable reply: {e}", "model": chat.model, **usage}
    return {"root_component": out.get("root_component"),
            "category": out.get("category"),
            "cause_ref": out.get("cause_ref") or None,
            "reason": (out.get("reason") or "")[:500],
            "model": chat.model, **usage}


def run_b2(case_ids: list[str] | None = None, runs_dir: Path = RUNS_DIR,
           model: str | None = None, verbose: bool = True) -> list[dict]:
    dirs = [runs_dir / c for c in case_ids] if case_ids else sorted(runs_dir.glob("case-*"))
    out = []
    for case_dir in dirs:
        if not (case_dir / "alert.json").exists():
            continue
        diagnosis = b2_diagnose(case_dir, model=model)
        if verbose:
            print(f"{case_dir.name}: {diagnosis.get('root_component')} / "
                  f"{diagnosis.get('category')} (${diagnosis.get('cost_usd', 0):.4f})")
        out.append({"case_id": case_dir.name, "method": "b2", **diagnosis})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Baseline B2: one shot, no tools.")
    parser.add_argument("--out", type=Path, default=DIAGNOSES_DIR / "b2.jsonl")
    parser.add_argument("--split", default=None, help="only this split's cases (e.g. test)")
    parser.add_argument("--cases", default=None, help="comma-separated case ids")
    parser.add_argument("--model", default=None, help="override the investigator's model")
    parser.add_argument("--print-prompt", default=None, metavar="CASE_ID",
                        help="print the prompt for one case and exit, without calling the model")
    args = parser.parse_args()

    if args.print_prompt:
        print(_vocabulary())
        print("\n" + "=" * 70 + "\n")
        print(build_prompt(RUNS_DIR / args.print_prompt))
        return 0

    case_ids = None
    if args.cases:
        case_ids = [c.strip() for c in args.cases.split(",") if c.strip()]
    elif args.split:
        from eval import splits
        if args.split == "test":
            # The test split goes through the same gate the investigator does, so a stale lock or
            # an unfrozen prompt stops the baseline too and the two runs stay comparable.
            case_ids = splits.lockable_test_ids()
            splits.guard_test_run(case_ids)
        else:
            case_ids = splits.split_ids(args.split)

    diagnoses = run_b2(case_ids)
    if not diagnoses:
        print("no recorded cases matched")
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(json.dumps(d) + "\n" for d in diagnoses), encoding="utf-8")
    total = sum(d.get("cost_usd") or 0 for d in diagnoses)
    print(f"wrote {len(diagnoses)} diagnoses to {args.out} (${total:.4f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
