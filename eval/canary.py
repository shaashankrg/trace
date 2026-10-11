"""Run the canary set against the assistant and push accuracy to Pushgateway.

Usage: python -m eval.canary [--smoke] [--workers 4] [--no-push]
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from prometheus_client import CollectorRegistry, push_to_gateway
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.samples import Sample

load_dotenv()

CANARY_PATH = Path(__file__).resolve().parent.parent / "assistant" / "canary.json"
ASSISTANT_URL = os.environ.get("ASSISTANT_URL", "http://localhost:8000")
PUSHGATEWAY_URL = os.environ.get("PUSHGATEWAY_URL", "localhost:9091")
CATEGORIES = ("docs", "tools", "multistep")


def _matches(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE | re.MULTILINE) is not None


def score(item: dict, answer: str) -> list[str]:
    """Return the reasons an answer fails; empty means it passes."""
    failures = []
    for required in item["must_contain"]:
        options = required if isinstance(required, list) else [required]
        if not any(_matches(p, answer) for p in options):
            failures.append(f"missing {options}")
    for forbidden in item["must_not_contain"]:
        if _matches(forbidden, answer):
            failures.append(f"contains {forbidden!r}")
    return failures


def ask(question: str, conversation_id: str) -> dict:
    body = json.dumps({"conversation_id": conversation_id, "question": question}).encode()
    req = urllib.request.Request(
        f"{ASSISTANT_URL}/chat", data=body, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def run_one(item: dict, run_id: str) -> dict:
    try:
        resp = ask(item["question"], f"canary-{run_id}-{item['id']}")
        failures = score(item, resp["answer"])
        return {**item, "answer": resp["answer"], "trace_id": resp["trace_id"],
                "passed": not failures, "failures": failures}
    except Exception as e:
        return {**item, "answer": "", "trace_id": None, "passed": False, "failures": [f"error: {e}"]}


class _Snapshot:
    """Collector for one run's results. Emits the overall accuracy without a
    category label and per-category accuracy with one, as the same metric."""

    def __init__(self, results: list[dict]):
        self.results = results

    def collect(self):
        acc = GaugeMetricFamily("kestrel_canary_accuracy", "Fraction of canary questions answered correctly")
        acc.samples.append(Sample("kestrel_canary_accuracy", {}, _accuracy(self.results)))
        for cat in CATEGORIES:
            subset = [r for r in self.results if r["category"] == cat]
            if subset:
                acc.samples.append(Sample("kestrel_canary_accuracy", {"category": cat}, _accuracy(subset)))
        yield acc
        ts = GaugeMetricFamily("kestrel_canary_run_timestamp", "Unix time the canary run finished")
        ts.add_metric([], time.time())
        yield ts


def _accuracy(results: list[dict]) -> float:
    return sum(r["passed"] for r in results) / len(results)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true", help="run only questions tagged smoke")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args()

    items = json.loads(CANARY_PATH.read_text(encoding="utf-8"))
    if args.smoke:
        items = [x for x in items if x["smoke"]]
    suite = "smoke" if args.smoke else "full"
    run_id = uuid.uuid4().hex[:8]

    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(lambda x: run_one(x, run_id), items))

    for r in results:
        if not r["passed"]:
            print(f"FAIL {r['id']} [{r['category']}] {r['question']}\n"
                  f"     {'; '.join(r['failures'])}\n"
                  f"     trace={r['trace_id']} answer={r['answer'][:200]!r}")
    for cat in CATEGORIES:
        subset = [r for r in results if r["category"] == cat]
        if subset:
            print(f"{cat:>10}: {sum(r['passed'] for r in subset)}/{len(subset)}")
    print(f"{suite} accuracy: {_accuracy(results):.3f} ({len(results)} questions, run {run_id})")

    if not args.no_push:
        registry = CollectorRegistry()
        registry.register(_Snapshot(results))
        push_to_gateway(PUSHGATEWAY_URL, job="kestrel_canary", grouping_key={"suite": suite},
                        registry=registry)
        print(f"pushed to {PUSHGATEWAY_URL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
