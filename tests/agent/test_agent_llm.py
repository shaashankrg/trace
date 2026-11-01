"""The spend cap and the crash description, without calling a model."""
import pytest

from agent import llm
from eval.replay_case import describe


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "spend.json"
    monkeypatch.setattr(llm, "SPEND_LEDGER", path)
    monkeypatch.setattr(llm, "SPEND_CAP_USD", 0.05)
    monkeypatch.setattr(llm, "_budget_check", None)  # no day budget in a cap test
    monkeypatch.setattr(llm, "_budget_add", None)
    return path


def fake_reply(cost_tokens: int):
    return llm.Reply("ok", usage=llm.Usage(input_tokens=cost_tokens))


def test_spend_is_recorded_and_the_cap_refuses_the_next_call(ledger, monkeypatch):
    chat = llm.Chat("system", model="gemini-3.8-flash")
    monkeypatch.setattr(chat, "_gemini", lambda turn: fake_reply(40_000))  # $0.03 at $0.75/M input

    chat.send("first")
    assert llm.spent() == pytest.approx(0.03)
    chat.send("second")  # under the cap before the call, over it after
    assert llm.spent() == pytest.approx(0.06)
    with pytest.raises(llm.SpendCapReached):
        chat.send("third")
    assert llm.spent() == pytest.approx(0.06)


def test_a_missing_ledger_counts_as_nothing_spent(ledger):
    assert not ledger.exists()
    assert llm.spent() == 0.0


def test_describe_unwraps_single_exception_groups():
    e = ExceptionGroup("unhandled errors in a TaskGroup", [ExceptionGroup("inner", [ValueError("boom")])])
    assert describe(e) == "ValueError: boom"
    both = ExceptionGroup("g", [ValueError("a"), KeyError("b")])
    assert describe(both) == "ValueError: a; KeyError: 'b'"
