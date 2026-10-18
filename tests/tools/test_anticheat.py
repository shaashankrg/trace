"""Anti-cheating: nothing a tool returns, and nothing a backend can open, may carry the answer.

What this catches: the labelled fields and words the generator writes (`ground_truth`, `fault`,
`decoy`, `eval.`...), any tool output field that names the cause (`root_cause`, `current`,
`correct`...), an internal fact name from facts.yaml, and a backend reaching for a file outside
its case folder.

What it misses: a leak written in ordinary words (a change description that says "this broke the
refund policy"), a value that is true and relevant but was never labelled, and anything the
generator forgets to label. Realism of the decoys and descriptions is Day 3's job, not this test's.
"""
import ast
import json
import re
from pathlib import Path

import pytest
import yaml
from conftest import FIXTURES, run_all_tools

from tools import backend as backend_module
from tools.backend import SNAPSHOT_FILES, SnapshotBackend

ROOT = Path(__file__).resolve().parents[2]
FACTS = yaml.safe_load((ROOT / "assistant" / "facts.yaml").read_text(encoding="utf-8"))

# Whole words only: "default" must not trip "fault".
MARKERS = re.compile(r"\b(ground[_ ]?truth|fault|faults|faulty|decoys?|facts\.yaml)\b|\beval\.", re.I)
# Keys that would say what the answer is. `active` and `indexed` are plain state, not verdicts.
VERDICT_KEYS = {"ground_truth", "fault", "fault_type", "decoy", "root_cause", "root_component", "cause",
                "cause_ref", "correct", "is_correct", "expected", "truth", "current",
                "is_current", "latest", "stale", "is_stale", "answer_check", "must_contain"}
FACT_NAMES = re.compile(r"\b(" + "|".join(re.escape(str(k)) for k in FACTS) + r")\b")


def keys_of(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from keys_of(v)
    elif isinstance(value, list):
        for v in value:
            yield from keys_of(v)


def test_no_tool_output_contains_ground_truth_markers(env):
    for name, out in run_all_tools(env).items():
        text = json.dumps(out)
        assert not MARKERS.search(text), f"{name}: {MARKERS.search(text).group(0)!r} in output"
        assert not FACT_NAMES.search(text), f"{name}: facts.yaml name {FACT_NAMES.search(text).group(0)!r}"
        assert not VERDICT_KEYS & set(keys_of(out)), f"{name}: {VERDICT_KEYS & set(keys_of(out))}"


def test_recorded_snapshots_carry_no_labels(snap):
    """The files themselves, not just what the tools choose to show."""
    for path in (FIXTURES / snap.name).glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        text = path.read_text(encoding="utf-8")
        assert not VERDICT_KEYS & set(keys_of(data)), path.name
        assert not MARKERS.search(text), f"{path.name}: {MARKERS.search(text).group(0)!r}"
    assert not (FIXTURES / snap.name / "ground_truth.json").exists()


def test_snapshot_backend_opens_only_snapshot_files_in_its_case_folder(snap, monkeypatch):
    opened: list[Path] = []
    real = Path.read_text

    def spy(self, *a, **k):
        opened.append(Path(self).resolve())
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy)
    run_all_tools(type(snap)(snap.name, SnapshotBackend(snap.name, FIXTURES), snap.alert))
    assert opened, "the backend should have read something"
    case_dir = (FIXTURES / snap.name).resolve()
    for path in opened:
        assert path.parent == case_dir, path
        assert path.stem in SNAPSHOT_FILES, path


@pytest.mark.parametrize("name", ["ground_truth", "cases", "facts", "../alert", "case", "spec", ""])
def test_snapshot_backend_refuses_anything_that_is_not_a_snapshot_file(snap, name):
    with pytest.raises(PermissionError):
        snap.backend._load(name)


@pytest.mark.parametrize("case_id", ["..", "../x", "a/b", "a\\b", "", ".hidden", "-x", "x y"])
def test_snapshot_backend_rejects_odd_case_ids(case_id):
    with pytest.raises(ValueError):
        SnapshotBackend(case_id, FIXTURES)


def test_snapshot_backend_rejects_a_case_that_does_not_exist():
    with pytest.raises(FileNotFoundError):
        SnapshotBackend("case-999", FIXTURES)


def test_tools_package_never_imports_eval_or_injector():
    for path in (ROOT / "tools").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for n in names:
                assert n.split(".")[0] not in {"eval", "injector"}, f"{path.name} imports {n}"


def test_the_live_backend_connects_as_the_tools_role_and_cannot_read_eval():
    import psycopg
    live = backend_module.LiveBackend()
    try:
        conn = live._pg()
    except psycopg.OperationalError:
        pytest.skip("database is down")
    with conn:
        assert conn.execute("SELECT current_user").fetchone()[0] == "tools"
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM eval.cases")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT ground_truth FROM eval.cases")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM changes")
