"""The train/dev/test split, the test-split lock and the test-run log.

The lock exists to stop the test split drifting after someone reads a bad test result, so these
tests are mostly about what the lock *refuses*.
"""
import json

import pytest

from eval import splits


@pytest.fixture
def cases():
    return splits.load_cases()


def test_every_case_is_in_exactly_one_split(cases):
    assert {c.get("split") for c in cases} == set(splits.SPLITS)
    sizes = {s: len(splits.split_ids(s, cases)) for s in splits.SPLITS}
    assert sum(sizes.values()) == len(cases)
    # ids are disjoint across splits
    seen = set()
    for s in splits.SPLITS:
        ids = set(splits.split_ids(s, cases))
        assert not (ids & seen)
        seen |= ids


def test_case_hash_covers_the_split_so_moving_a_case_breaks_the_lock(cases):
    case = next(c for c in cases if c["split"] == "test")
    moved = {**case, "split": "train"}
    assert splits.case_hash(moved) != splits.case_hash(case)


def test_case_hash_ignores_key_order(cases):
    case = cases[0]
    shuffled = dict(reversed(list(case.items())))
    assert splits.case_hash(shuffled) == splits.case_hash(case)


def test_case_hash_changes_when_the_question_changes(cases):
    case = next(c for c in cases if c["split"] == "test")
    reworded = {**case, "alert": {**case["alert"], "question": "something else?"}}
    assert splits.case_hash(reworded) != splits.case_hash(case)


# --- the lock -------------------------------------------------------------------------------

def write_temp_lock(tmp_path, monkeypatch, cases, valid=None):
    """A lock over `cases`, with load_cases and the validity source patched.

    `valid` defaults to every test case, so a test that is not about validity gets a lock whose
    ids are exactly the test split and can only fail on the thing it is checking."""
    monkeypatch.setattr(splits, "load_cases", lambda: cases)
    ids = valid if valid is not None else {c["case_id"] for c in cases if c["split"] == "test"}
    monkeypatch.setattr(splits, "valid_recorded_ids", lambda: set(ids))
    path = tmp_path / "test_split.lock"
    return path, splits.write_lock(path)


def test_a_lock_round_trips(tmp_path, monkeypatch, cases):
    path, lock = write_temp_lock(tmp_path, monkeypatch, cases)
    assert lock["case_count"] == len(lock["case_ids"]) > 0
    assert splits.check_lock(path)["split_hash"] == lock["split_hash"]


def test_the_lock_rejects_a_case_that_left_the_test_split(tmp_path, monkeypatch, cases):
    path, lock = write_temp_lock(tmp_path, monkeypatch, cases)
    target = lock["case_ids"][0]
    moved = [{**c, "split": "train"} if c["case_id"] == target else c for c in cases]
    monkeypatch.setattr(splits, "load_cases", lambda: moved)
    with pytest.raises(splits.LockError, match="left the test split"):
        splits.check_lock(path)


def test_the_lock_rejects_a_silently_reworded_case(tmp_path, monkeypatch, cases):
    """The subtle one: same ids, edited content. A hash of the ids alone would miss it."""
    path, lock = write_temp_lock(tmp_path, monkeypatch, cases)
    target = lock["case_ids"][0]
    edited = [{**c, "alert": {**c["alert"], "question": "reworded after a bad run?"}}
              if c["case_id"] == target else c for c in cases]
    monkeypatch.setattr(splits, "load_cases", lambda: edited)
    with pytest.raises(splits.LockError, match="contents changed"):
        splits.check_lock(path)


def test_a_missing_lock_is_an_error_not_a_pass(tmp_path):
    with pytest.raises(splits.LockError, match="no test-split lock"):
        splits.check_lock(tmp_path / "nope.lock")


def test_the_lock_only_covers_cases_with_a_failing_recording(tmp_path, monkeypatch, cases):
    """A case that never produced a wrong answer is not part of the benchmark."""
    monkeypatch.setattr(splits, "load_cases", lambda: cases)
    test_ids = splits.split_ids("test", cases)
    keep = set(test_ids[:-1])
    monkeypatch.setattr(splits, "valid_recorded_ids", lambda: keep)
    path = tmp_path / "l.lock"
    lock = splits.write_lock(path)
    assert test_ids[-1] not in lock["case_ids"]
    assert test_ids[-1] in lock["excluded_not_valid"]
    assert splits.check_lock(path)  # an excluded case still in the split is not drift


# --- the guard and the run log --------------------------------------------------------------

def test_a_test_run_needs_the_frozen_prompt(tmp_path, monkeypatch, cases):
    path, lock = write_temp_lock(tmp_path, monkeypatch, cases)
    monkeypatch.setattr(splits, "frozen_prompt", lambda: "deadbeef" * 8)
    # the ids are the locked ones, so only the prompt check can fail here
    with pytest.raises(splits.LockError, match="not the frozen one"):
        splits.guard_test_run(list(lock["case_ids"]), path=path)
    # with the real hash it passes
    monkeypatch.setattr(splits, "frozen_prompt", splits.prompt_hash)
    assert splits.guard_test_run(list(lock["case_ids"]), path=path)["case_count"] == lock["case_count"]


def test_a_test_run_refuses_cases_outside_the_locked_split(tmp_path, monkeypatch, cases):
    path, lock = write_temp_lock(tmp_path, monkeypatch, cases)
    monkeypatch.setattr(splits, "frozen_prompt", splits.prompt_hash)
    with pytest.raises(splits.LockError, match="not in the locked test split"):
        splits.guard_test_run(lock["case_ids"] + ["case-999"], path=path)


def test_the_run_log_appends_one_countable_line_per_run(tmp_path):
    log = tmp_path / "test_runs.log"
    assert splits.test_run_count(log) == 0
    splits.log_test_run("run-1", ["case-021", "case-022"], path=log)
    splits.log_test_run("run-2", ["case-021"], note="after a prompt fix", path=log)
    assert splits.test_run_count(log) == 2
    body = log.read_text(encoding="utf-8")
    assert body.startswith("#"), "the log explains itself at the top"
    assert "run-1" in body and "after a prompt fix" in body


def test_freezing_the_prompt_records_the_hash_on_disk(tmp_path, monkeypatch):
    path = tmp_path / "frozen.sha"
    h = splits.freeze_prompt(path)
    assert h == splits.prompt_hash()
    monkeypatch.setattr(splits, "FROZEN_PROMPT_PATH", path)
    assert splits.frozen_prompt() == h
