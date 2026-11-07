"""The freeze covers the agent's configuration, not just its prompt file.

Cheap-first routing and the step budget change the agent's answers as much as a prompt edit, so a
test run has to pin them. These tests are the proof that retuning a threshold after the freeze is
caught rather than silently allowed.
"""
import pytest

from eval import splits


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    path = tmp_path / "frozen_prompt.sha"
    monkeypatch.setattr(splits, "FROZEN_PROMPT_PATH", path)
    for k in splits.CONFIG_KEYS:  # start from the defaults, whatever the developer's .env says
        monkeypatch.delenv(k, raising=False)
    return path


def test_defaults_and_an_explicit_default_hash_the_same(frozen, monkeypatch):
    """An unset var and a var set to its default describe the same agent, so they must agree -
    otherwise exporting a var you were already relying on would break the lock."""
    before = splits.config_hash()
    monkeypatch.setenv("INVESTIGATOR_TOOL_BUDGET", splits.CONFIG_KEYS["INVESTIGATOR_TOOL_BUDGET"])
    assert splits.config_hash() == before


def test_changing_a_pinned_setting_changes_the_hash(frozen, monkeypatch):
    before = splits.config_hash()
    monkeypatch.setenv("INVESTIGATOR_TOOL_BUDGET", "10")
    assert splits.config_hash() != before


def test_freeze_writes_the_settings_it_pinned(frozen):
    h = splits.freeze_prompt(frozen)
    text = frozen.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith(h)
    assert "INVESTIGATOR_TOOL_BUDGET=15" in text
    assert f"sha256={splits.prompt_hash()}" in text
    assert splits.frozen_prompt() == h
    assert splits.frozen_config()["INVESTIGATOR_TOOL_BUDGET"] == "15"


def test_the_guard_accepts_the_frozen_configuration(frozen, monkeypatch):
    splits.freeze_prompt(frozen)
    monkeypatch.setattr(splits, "check_lock", lambda path=None: {"case_ids": ["case-001"]})
    assert splits.guard_test_run(["case-001"])  # no raise


def test_the_guard_refuses_a_retuned_threshold_and_names_it(frozen, monkeypatch):
    """The loophole: tune the escalation threshold after freezing the prompt. The prompt file is
    untouched, so a prompt-only hash would have allowed this."""
    splits.freeze_prompt(frozen)
    monkeypatch.setattr(splits, "check_lock", lambda path=None: {"case_ids": ["case-001"]})
    monkeypatch.setenv("INVESTIGATOR_ESCALATE_CONFIDENCE", "0.9")
    with pytest.raises(splits.LockError) as e:
        splits.guard_test_run(["case-001"])
    msg = str(e.value)
    assert "INVESTIGATOR_ESCALATE_CONFIDENCE" in msg
    assert "'0.6'" in msg and "'0.9'" in msg
    assert "the prompt file changed" not in msg, "blamed the prompt for a config change"


def test_the_guard_refuses_a_lowered_step_budget(frozen, monkeypatch):
    splits.freeze_prompt(frozen)
    monkeypatch.setattr(splits, "check_lock", lambda path=None: {"case_ids": ["case-001"]})
    monkeypatch.setenv("INVESTIGATOR_TOOL_BUDGET", "10")
    with pytest.raises(splits.LockError, match="INVESTIGATOR_TOOL_BUDGET"):
        splits.guard_test_run(["case-001"])


def test_the_guard_refuses_a_model_swap(frozen, monkeypatch):
    """Switching the investigator to the cheap model after freezing would make the test numbers
    describe a different agent than the one that was tuned."""
    splits.freeze_prompt(frozen)
    monkeypatch.setattr(splits, "check_lock", lambda path=None: {"case_ids": ["case-001"]})
    monkeypatch.setenv("INVESTIGATOR_MODEL", "gemini-3.5-flash-lite")
    with pytest.raises(splits.LockError, match="INVESTIGATOR_MODEL"):
        splits.guard_test_run(["case-001"])


def test_no_freeze_file_still_refuses_with_the_how_to(frozen, monkeypatch):
    monkeypatch.setattr(splits, "check_lock", lambda path=None: {"case_ids": ["case-001"]})
    assert splits.frozen_prompt() is None
    with pytest.raises(splits.LockError, match="freeze-prompt"):
        splits.guard_test_run(["case-001"])


def test_the_tool_output_cap_is_what_the_freeze_pins(frozen, monkeypatch):
    """tools/output.py reads the var at import, so the pinned value and the module default must be
    the same number; if one is edited without the other the freeze would record a fiction."""
    import importlib

    from tools import output
    monkeypatch.setenv("TOOL_OUTPUT_MAX_CHARS", splits.CONFIG_KEYS["TOOL_OUTPUT_MAX_CHARS"])
    importlib.reload(output)
    try:
        assert output.MAX_CHARS == int(splits.CONFIG_KEYS["TOOL_OUTPUT_MAX_CHARS"])
    finally:
        monkeypatch.delenv("TOOL_OUTPUT_MAX_CHARS", raising=False)
        importlib.reload(output)
