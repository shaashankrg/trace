"""Suite-wide defaults.

The LLM response cache (agent/cache.py) is off for every test. A cache hit is free and returns a
recorded reply, which is right for a replay and wrong for a test: whether an assertion about spend,
tokens or call counts held would depend on what happened to be on this machine's disk. A test that
wants the cache turns it back on itself with a tmp_path cache dir.
"""
import pytest


@pytest.fixture(autouse=True)
def _no_llm_cache(monkeypatch):
    monkeypatch.setenv("INVESTIGATOR_CACHE", "0")
