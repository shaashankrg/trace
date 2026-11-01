"""The day budget (eval/spend.py) and the response cache (agent/cache.py).

Both exist to stop a run costing more than intended, so the things worth asserting are: the budget
refuses a call once the window is spent, and a cache hit is both faithful and free.
"""
import pytest

from agent import cache, llm
from eval import spend


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "spend.json"


@pytest.fixture
def live_cache(tmp_path, monkeypatch):
    """Turn the cache back on, pointed at a temp dir (tests/conftest.py disables it)."""
    monkeypatch.setenv("INVESTIGATOR_CACHE", "1")
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "llm_cache")
    return cache.CACHE_DIR


# --- the day budget ---------------------------------------------------------------------------

def test_a_fresh_window_starts_at_zero(ledger):
    spend.open_window(3.00, "day9", ledger)
    d = spend.read(ledger)
    assert (d["usd"], d["budget_usd"], d["calls"]) == (0.0, 3.00, 0)
    assert spend.remaining(ledger) == 3.00


def test_a_missing_ledger_is_an_empty_window(tmp_path):
    assert spend.read(tmp_path / "nope.json")["usd"] == 0.0


def test_check_refuses_only_once_the_window_is_spent(ledger):
    spend.open_window(0.10, path=ledger)
    spend.add(0.04, ledger)
    spend.check(ledger)  # under budget: the call goes ahead
    spend.add(0.07, ledger)  # the call in flight may overshoot
    with pytest.raises(spend.BudgetExceeded):
        spend.check(ledger)
    assert spend.remaining(ledger) == pytest.approx(-0.01)


def test_opening_a_window_discards_the_previous_total(ledger):
    spend.open_window(1.00, path=ledger)
    spend.add(0.90, ledger)
    spend.open_window(1.00, path=ledger)
    assert spend.read(ledger)["usd"] == 0.0


def test_add_counts_calls_and_survives_a_reread(ledger):
    spend.open_window(1.00, path=ledger)
    for _ in range(3):
        spend.add(0.01, ledger)
    assert spend.read(ledger)["calls"] == 3
    assert spend.read(ledger)["usd"] == pytest.approx(0.03)


def test_install_makes_llm_enforce_the_window(ledger, monkeypatch):
    monkeypatch.setattr(llm, "SPEND_LEDGER", ledger.parent / "lifetime.json")
    monkeypatch.setattr(llm, "SPEND_CAP_USD", 999.0)  # the lifetime cap must not be what refuses
    spend.open_window(0.02, path=ledger)
    spend.install(ledger)
    try:
        chat = llm.Chat("system", model="gemini-3.8-flash")
        monkeypatch.setattr(chat, "_gemini", lambda turn: llm.Reply("ok", usage=llm.Usage(40_000)))
        chat.send("first")  # $0.03, over the $0.02 window
        assert spend.read(ledger)["usd"] == pytest.approx(0.03)
        with pytest.raises(spend.BudgetExceeded):
            chat.send("second")
    finally:
        llm.set_budget_hooks(None, None)


# --- the response cache ----------------------------------------------------------------------

def test_a_hit_replays_text_tool_calls_and_is_free(live_cache, ledger, monkeypatch):
    monkeypatch.setattr(llm, "SPEND_LEDGER", ledger.parent / "lifetime.json")
    monkeypatch.setattr(llm, "SPEND_CAP_USD", 999.0)
    spend.open_window(1.00, path=ledger)
    spend.install(ledger)
    try:
        reply = llm.Reply("found it", [llm.ToolCall("c1", "get_trace", {"id": "t1"})], llm.Usage(1000, 50))
        calls = {"n": 0}

        def fake(turn):
            calls["n"] += 1
            return reply

        c1 = llm.Chat("sys", model="gemini-3.8-flash")
        monkeypatch.setattr(c1, "_gemini", fake)
        c1.send("investigate")
        spent_after_first = spend.read(ledger)["usd"]
        assert calls["n"] == 1 and spent_after_first > 0

        c2 = llm.Chat("sys", model="gemini-3.8-flash")
        monkeypatch.setattr(c2, "_gemini", fake)
        got = c2.send("investigate")
        assert calls["n"] == 1, "the provider was called again on a hit"
        assert got.text == "found it"
        assert [(t.id, t.name, t.input) for t in got.tool_calls] == [("c1", "get_trace", {"id": "t1"})]
        assert c2.cached_calls == 1
        assert spend.read(ledger)["usd"] == spent_after_first, "a hit was charged"
    finally:
        llm.set_budget_hooks(None, None)


def test_the_key_covers_model_system_and_history(live_cache):
    tools = [{"name": "t", "description": "d", "input_schema": {"type": "object"}}]
    base = cache.key("m1", "sys", tools, [], {})
    assert base == cache.key("m1", "sys", tools, [], {}), "not deterministic"
    assert base != cache.key("m2", "sys", tools, [], {}), "model ignored"
    assert base != cache.key("m1", "other", tools, [], {}), "system prompt ignored"
    assert base != cache.key("m1", "sys", tools, ["a turn"], {}), "history ignored"
    assert base != cache.key("m1", "sys", tools, [], {"json_mode": True}), "extra ignored"


def test_a_volatile_thought_signature_does_not_change_the_key(live_cache):
    """Gemini stamps each reply with a per-response signature; keying on it would miss every turn
    after the first, since a replayed history cannot reproduce it."""
    a = [{"role": "model", "parts": [{"text": "hi", "thought_signature": "aabb"}]}]
    b = [{"role": "model", "parts": [{"text": "hi", "thought_signature": "ccdd"}]}]
    assert cache.key("m", "s", [], a) == cache.key("m", "s", [], b)
    c = [{"role": "model", "parts": [{"text": "different", "thought_signature": "aabb"}]}]
    assert cache.key("m", "s", [], a) != cache.key("m", "s", [], c)


def test_disabling_the_cache_makes_every_call_a_miss(live_cache, monkeypatch):
    k = cache.key("m", "s", [], ["x"])
    cache.put(k, {"text": "stored"})
    assert cache.get(k)["text"] == "stored"
    monkeypatch.setenv("INVESTIGATOR_CACHE", "0")
    assert cache.get(k) is None, "a disabled cache still served an entry"
    cache.put(k, {"text": "ignored"})  # writes are no-ops too


def test_get_tolerates_a_corrupt_entry(live_cache):
    k = cache.key("m", "s", [], ["y"])
    p = cache._path(k)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert cache.get(k) is None
