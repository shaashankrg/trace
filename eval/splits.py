"""The train/dev/test split, the test-split lock, and the honest log of test-split runs.

Why a lock. The test split is only worth something if the agent was never tuned on it. Nothing
stops someone from quietly regenerating a case after a bad test run, so the split is pinned:
`eval/test_split.lock` holds the test case ids and a hash of their JSON, and a test-split run
refuses to start unless what is on disk still hashes to the locked value.

Why a log. `eval/results/test_runs.log` gets a line per test-split run (time, git commit, prompt
hash, case count, run id). Looking at test results many times is how a held-out split quietly
becomes a dev split; the log makes the count visible instead of forgettable.

Usage:
  python -m eval.splits --write-lock      # pin the current test split (once, on Day 8)
  python -m eval.splits --check           # verify the lock against the cases on disk
  python -m eval.splits --show            # print the split sizes and the test ids
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK_PATH = ROOT / "eval" / "test_split.lock"
RUN_LOG_PATH = ROOT / "eval" / "results" / "test_runs.log"
PROMPT_PATH = ROOT / "agent" / "prompts" / "investigator.md"
FROZEN_PROMPT_PATH = ROOT / "eval" / "frozen_prompt.sha"
SPLITS = ("train", "dev", "test")


class LockError(RuntimeError):
    """The test split on disk is not the split that was locked."""


def load_cases() -> list[dict]:
    from injector.make_cases import load_cases as _load
    return _load()


def split_ids(split: str, cases: list[dict] | None = None) -> list[str]:
    cases = cases if cases is not None else load_cases()
    return sorted(c["case_id"] for c in cases if c.get("split") == split)


def case_hash(case: dict) -> str:
    """A hash of everything that defines the case, ignoring key order. `split` is included: moving
    a case between splits has to break the lock."""
    payload = {k: case[k] for k in ("case_id", "conversation_id", "fault", "decoy", "alert",
                                    "answer_check", "split") if k in case}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def split_hash(split: str, cases: list[dict] | None = None) -> str:
    cases = cases if cases is not None else load_cases()
    members = sorted((c for c in cases if c.get("split") == split), key=lambda c: c["case_id"])
    if not members:
        raise LockError(f"no cases in the {split!r} split")
    joined = "\n".join(f"{c['case_id']} {case_hash(c)}" for c in members)
    return hashlib.sha256(joined.encode()).hexdigest()


def prompt_hash(path: Path = PROMPT_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def git_dirty() -> bool:
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        return bool(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


RUNS_DIR = ROOT / "eval" / "runs"


def recorded_ids() -> set[str]:
    if not RUNS_DIR.exists():
        return set()
    return {p.name for p in RUNS_DIR.iterdir() if (p / "alert.json").exists()}


def valid_recorded_ids() -> set[str]:
    """Cases whose recorded snapshot holds an answer that actually fails the case's own check.

    This reads the snapshot on disk rather than eval.case_runs, deliberately. The assistant model
    is not deterministic, so re-running a case can log "the fault did not make the answer wrong"
    after an earlier attempt recorded a genuinely wrong answer; the latest row then disagrees with
    the recording the scorer will actually replay. The snapshot is what gets evaluated, so the
    snapshot decides. A case that could not be made to fail is not a test case and stays out of
    the lock rather than padding the split."""
    from eval.canary import score

    good = set()
    for case in load_cases():
        cid = case["case_id"]
        alert = RUNS_DIR / cid / "alert.json"
        if not alert.exists():
            continue
        try:
            answer = json.loads(alert.read_text(encoding="utf-8")).get("answer") or ""
        except (OSError, ValueError):
            continue
        if score(case["answer_check"], answer):
            good.add(cid)
    return good


def lockable_test_ids(cases: list[dict] | None = None) -> list[str]:
    """The test cases the lock covers: in the test split, recorded, and recorded as valid."""
    cases = cases if cases is not None else load_cases()
    good = valid_recorded_ids()
    return [c for c in split_ids("test", cases) if c in good]


def locked_hash(case_ids: list[str], cases: list[dict]) -> str:
    by_id = {c["case_id"]: c for c in cases}
    return hashlib.sha256("\n".join(f"{cid} {case_hash(by_id[cid])}"
                                    for cid in case_ids if cid in by_id).encode()).hexdigest()


def write_lock(path: Path = LOCK_PATH) -> dict:
    cases = load_cases()
    ids = lockable_test_ids(cases)
    if not ids:
        raise LockError("no valid recorded test cases to lock; run eval.run_case first")
    by_id = {c["case_id"]: c for c in cases}
    lock = {
        "split": "test",
        "locked_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        "case_count": len(ids),
        "case_ids": ids,
        # Test-split cases that never produced a wrong answer, so they are not in the benchmark.
        "excluded_not_valid": [c for c in split_ids("test", cases) if c not in ids],
        "split_hash": locked_hash(ids, cases),
        "case_hashes": {cid: case_hash(by_id[cid]) for cid in ids},
    }
    path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    return lock


def read_lock(path: Path = LOCK_PATH) -> dict:
    if not path.exists():
        raise LockError(f"no test-split lock at {path}; run python -m eval.splits --write-lock")
    return json.loads(path.read_text(encoding="utf-8"))


def check_lock(path: Path = LOCK_PATH) -> dict:
    """Raise LockError unless the test split on disk is exactly the locked one."""
    lock = read_lock(path)
    cases = load_cases()
    # Compare against the locked ids, not the whole split: the split may legitimately still hold
    # cases that never recorded a failure, and the lock lists those under `excluded_not_valid`.
    in_split = set(split_ids("test", cases))
    still_there = [c for c in lock["case_ids"] if c in in_split]
    if still_there != lock["case_ids"]:
        removed = sorted(set(lock["case_ids"]) - in_split)
        raise LockError(f"{len(removed)} locked test case(s) left the test split: {removed[:5]}")
    if locked_hash(lock["case_ids"], cases) != lock["split_hash"]:
        by_id = {c["case_id"]: c for c in cases}
        drifted = [cid for cid, h in lock["case_hashes"].items()
                   if cid in by_id and case_hash(by_id[cid]) != h]
        raise LockError(f"test-split contents changed since the lock: {drifted[:8]} "
                        f"({len(drifted)} case(s) differ)")
    return lock


# What the freeze covers besides the prompt file. Cheap-first routing and the step budget change
# the agent's answers as surely as a prompt edit does, so a test run has to pin them too: without
# this, the escalation threshold could be retuned after the freeze and the lock would not notice,
# which is the loophole the held-out split exists to close. Each entry is an env var and the
# default agent/ uses when it is unset, so an unset var and a var set to the default agree.
CONFIG_KEYS = {
    "INVESTIGATOR_MODEL": "gemini-3.8-flash",          # llm.DEFAULT_MODEL
    "INVESTIGATOR_CHEAP_MODEL": "gemini-3.5-flash-lite",
    "INVESTIGATOR_ROUTING": "cheap_first",
    "INVESTIGATOR_ESCALATE_CONFIDENCE": "0.6",
    "INVESTIGATOR_TOOL_BUDGET": "15",                  # mcp_tools.TOOL_BUDGET
    "INVESTIGATOR_REPLAY_BUDGET": "3",                 # mcp_tools.REPLAY_BUDGET
    "INVESTIGATOR_MAX_TOKENS": "4096",                 # llm.MAX_TOKENS
    "INVESTIGATOR_THINKING": "LOW",
    "TOOL_OUTPUT_MAX_CHARS": "14000",                  # tools/output.py MAX_CHARS
}


def agent_config() -> dict[str, str]:
    """The pinned settings as they are right now, defaults filled in."""
    return {k: os.environ.get(k) or default for k, default in sorted(CONFIG_KEYS.items())}


def config_hash() -> str:
    blob = json.dumps(agent_config(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def freeze_hash() -> str:
    """One hash over the prompt and the configuration that decides how it is used."""
    return hashlib.sha256(f"{prompt_hash()}:{config_hash()}".encode("utf-8")).hexdigest()


def _frozen_fields() -> dict[str, str]:
    """The frozen file parsed into {key: value}; the first line's hash is under 'freeze'."""
    if not FROZEN_PROMPT_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for i, line in enumerate(FROZEN_PROMPT_PATH.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if i == 0:
            out["freeze"] = line.split()[0]
        elif "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def frozen_prompt() -> str | None:
    """The frozen combined hash, or None. Kept under its original name: every caller wants
    'the thing a test run must match', which is now the prompt plus its configuration."""
    return _frozen_fields().get("freeze")


def frozen_config() -> dict[str, str]:
    return {k: v for k, v in _frozen_fields().items() if k in CONFIG_KEYS}


def freeze_prompt(path: Path = FROZEN_PROMPT_PATH) -> str:
    """Pin the prompt and the agent configuration. A test run must match both."""
    h = freeze_hash()
    cfg = agent_config()
    lines = [f"{h}  prompt+config",
             f"# prompt {PROMPT_PATH.relative_to(ROOT).as_posix()} sha256={prompt_hash()}",
             f"# frozen {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
             "# the settings below are part of the freeze; changing one invalidates a test run"]
    lines += [f"{k}={v}" for k, v in cfg.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return h


def log_test_run(run_id: str, case_ids: list[str], note: str = "",
                 path: Path = RUN_LOG_PATH) -> str:
    """Append one line to the test-run log and return it.

    The log is the honest record of how many times the test split was looked at. Interviewers
    respect "I ran the test split 4 times, here is the log" far more than a single clean number.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ("# Every test-split run, appended. time | git | prompt_sha | cases | run_id | note\n"
              "# A held-out split stops being held out once you tune against what you read here.\n")
    if not path.exists():
        path.write_text(header, encoding="utf-8")
    line = " | ".join([
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        git_commit() + ("+dirty" if git_dirty() else ""),
        prompt_hash()[:12],
        str(len(case_ids)),
        run_id,
        note or "-",
    ])
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return line


def test_run_count(path: Path = RUN_LOG_PATH) -> int:
    if not path.exists():
        return 0
    return sum(1 for ln in path.read_text(encoding="utf-8").splitlines()
               if ln.strip() and not ln.startswith("#"))


def guard_test_run(case_ids: list[str], require_frozen_prompt: bool = True,
                   path: Path = LOCK_PATH) -> dict:
    """Called before a test-split run. Refuses unless the lock matches and the prompt is the
    frozen one; returns the lock on success."""
    lock = check_lock(path)
    locked = set(lock["case_ids"])
    stray = sorted(set(case_ids) - locked)
    if stray:
        raise LockError(f"not in the locked test split: {stray[:8]}")
    if require_frozen_prompt:
        want = frozen_prompt()
        if want is None:
            raise LockError(f"no frozen prompt at {FROZEN_PROMPT_PATH}; "
                            "freeze it after tuning on train (python -m eval.splits --freeze-prompt)")
        if want != freeze_hash():
            # Say what moved: "the hash differs" sends you hunting through a prompt that may be
            # untouched when an env var is what changed.
            frozen_cfg, now = frozen_config(), agent_config()
            drift = [f"{k}: frozen {frozen_cfg[k]!r}, now {now[k]!r}"
                     for k in sorted(frozen_cfg) if frozen_cfg[k] != now.get(k)]
            # The recorded prompt sha lives in a comment line. Read it defensively: the freeze
            # value can come from somewhere other than this file (a test stub), and failing to
            # explain a real drift is worse than explaining it incompletely.
            frozen_lines = (FROZEN_PROMPT_PATH.read_text(encoding="utf-8").splitlines()
                            if FROZEN_PROMPT_PATH.exists() else [])
            prompt_moved = any(ln.startswith("# prompt ") and f"sha256={prompt_hash()}" not in ln
                               for ln in frozen_lines)
            why = (["the prompt file changed"] if prompt_moved else []) + drift
            raise LockError(
                f"the investigator is not the frozen one: {freeze_hash()[:12]} on disk, "
                f"{want[:12]} in {FROZEN_PROMPT_PATH.name}. A test run has to use the frozen "
                f"prompt and configuration. Changed: " + ("; ".join(why) or "unknown"))
    return lock


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-lock", action="store_true", help="pin the current test split")
    parser.add_argument("--check", action="store_true", help="verify the lock against the cases on disk")
    parser.add_argument("--show", action="store_true", help="print split sizes and test ids")
    parser.add_argument("--freeze-prompt", action="store_true", help="pin the current investigator prompt")
    args = parser.parse_args()

    if args.write_lock:
        if LOCK_PATH.exists():
            print(f"refusing to overwrite {LOCK_PATH.name}: a locked split is meant to stay locked.\n"
                  f"delete it deliberately if the benchmark really is being rebuilt.")
            return 1
        lock = write_lock()
        print(f"locked {lock['case_count']} test cases, hash {lock['split_hash'][:16]} -> {LOCK_PATH}")
        return 0

    if args.freeze_prompt:
        print(f"frozen prompt {freeze_prompt()[:16]} -> {FROZEN_PROMPT_PATH}")
        return 0

    if args.check:
        try:
            lock = check_lock()
        except LockError as e:
            print(f"LOCK FAILED: {e}")
            return 1
        print(f"lock ok: {lock['case_count']} test cases, hash {lock['split_hash'][:16]}, "
              f"locked {lock['locked_at']}")
        print(f"test-split runs so far: {test_run_count()}")
        return 0

    cases = load_cases()
    for s in SPLITS:
        ids = split_ids(s, cases)
        print(f"{s:>6}: {len(ids):3} cases")
    print()
    print("test ids:", ", ".join(split_ids("test", cases)))
    fp = frozen_prompt()
    print(f"\nprompt on disk: {prompt_hash()[:16]}")
    print(f"frozen prompt : {fp[:16] if fp else '(not frozen)'}")
    print(f"test-split runs: {test_run_count()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
