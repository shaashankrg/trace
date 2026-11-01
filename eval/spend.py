"""A day's spend limit, separate from the lifetime cap in agent/llm.py.

agent/llm.py holds one hard cap over the project's whole life. That is the wrong instrument for
"today's test run must not cost more than $3": the lifetime number keeps climbing, so a per-day
budget has to track its own total and be resettable. This module is that second limit.

The ledger is a JSON file holding one window: when it opened, what has been spent in it, and the
budget. `open_window` starts a fresh one; `add` records a call's cost and raises BudgetExceeded
once the window passes its budget. The file is the state, so a run that dies mid-case and is
resumed keeps counting against the same window.

Usage:
    python -m eval.spend --open 3.00      # start a window with a $3 budget
    python -m eval.spend                  # print the window
"""
import argparse
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = Path(os.environ.get("EVAL_SPEND_LEDGER", ROOT / "eval" / "results" / "spend.json"))
DEFAULT_BUDGET = float(os.environ.get("EVAL_DAY_BUDGET_USD", "3.00"))

_lock = threading.Lock()


class BudgetExceeded(RuntimeError):
    """The day's window is out of budget. Distinct from llm.SpendCapReached (the lifetime cap)."""


def read(path: Path = LEDGER) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"opened": None, "usd": 0.0, "budget_usd": DEFAULT_BUDGET, "calls": 0, "label": ""}
    d.setdefault("usd", 0.0)
    d.setdefault("budget_usd", DEFAULT_BUDGET)
    d.setdefault("calls", 0)
    return d


def _write(d: dict, path: Path = LEDGER) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")


def open_window(budget_usd: float = DEFAULT_BUDGET, label: str = "", path: Path = LEDGER) -> dict:
    """Start a fresh window. The previous window's total is discarded, so call this once a day."""
    d = {"opened": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "usd": 0.0, "budget_usd": float(budget_usd), "calls": 0, "label": label}
    _write(d, path)
    return d


def remaining(path: Path = LEDGER) -> float:
    d = read(path)
    return d["budget_usd"] - d["usd"]


def check(path: Path = LEDGER) -> None:
    """Raise if the window is already spent. Called before a call, so the one in flight is the
    last one: a call's cost is only known after it returns."""
    d = read(path)
    if d["usd"] >= d["budget_usd"]:
        raise BudgetExceeded(
            f"day budget reached: ${d['usd']:.4f} of ${d['budget_usd']:.2f} spent in the window "
            f"opened {d['opened']} ({path}). Raise it with "
            f"`python -m eval.spend --open <usd>` once you mean to spend more.")


def add(usd: float, path: Path = LEDGER) -> dict:
    with _lock:
        d = read(path)
        d["usd"] = round(d["usd"] + float(usd), 6)
        d["calls"] += 1
        _write(d, path)
        return d


def install(path: Path = LEDGER) -> None:
    """Make agent/llm.py enforce this budget.

    The dependency has to point this way: agent/ never imports eval/ (there is a test), so eval/
    hands agent/ the two callables instead. Any entry point that spends money calls this once.
    """
    from agent import llm
    llm.set_budget_hooks(check=lambda: check(path), add=lambda usd: add(usd, path))


def describe(path: Path = LEDGER) -> str:
    d = read(path)
    return (f"${d['usd']:.4f} of ${d['budget_usd']:.2f} over {d['calls']} call(s)"
            + (f" [{d['label']}]" if d.get("label") else ""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--open", type=float, metavar="USD", help="open a fresh window with this budget")
    parser.add_argument("--label", default="", help="what the window is for, e.g. 'day9 test split'")
    args = parser.parse_args()
    if args.open is not None:
        d = open_window(args.open, args.label)
        print(f"window open: ${d['budget_usd']:.2f} budget, opened {d['opened']}  -> {LEDGER}")
    else:
        print(f"{describe()}  -> {LEDGER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
