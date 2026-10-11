"""Pure unit tests: no database or network needed."""
import pytest

from assistant.guardrail import check
from assistant.ingest import chunk, latest_versions
from eval.canary import score
from eval.render_docs import load_facts, render


def test_chunk_respects_size_and_keeps_all_words():
    text = " ".join(f"word{i}" for i in range(200))
    pieces = chunk(text, 300)
    assert all(len(p) <= 300 for p in pieces)
    assert " ".join(pieces) == text


def test_latest_version_is_newest_published():
    docs = [
        ({"doc_id": "a", "version": "v1", "published_at": "2025-01-01"}, ""),
        ({"doc_id": "a", "version": "v2", "published_at": "2026-01-01"}, ""),
        ({"doc_id": "b", "version": "v1", "published_at": "2026-02-01"}, ""),
    ]
    assert latest_versions(docs) == {("a", "v2"), ("b", "v1")}


def test_render_fills_placeholders_and_rejects_unknown_facts():
    assert render("{{x}} days", {"x": 14}) == "14 days"
    with pytest.raises(KeyError):
        render("{{missing}}", {})


def test_every_template_placeholder_exists_in_facts():
    from eval.render_docs import PLACEHOLDER, TEMPLATES_DIR

    facts = load_facts()
    for path in TEMPLATES_DIR.glob("*.md"):
        for key in PLACEHOLDER.findall(path.read_text(encoding="utf-8")):
            assert key in facts, f"{path.name}: {key}"


def test_guardrail_blocks_whole_words_only():
    assert not check("I need legal advice about my order", ["legal advice"]).allowed
    assert check("Can I hack together a repair?", ["hacksaw"]).allowed
    decision = check("Tell me about a weapon", ["weapon"])
    assert decision.matched_rule == "weapon"


def test_score_must_contain_and_must_not_contain():
    item = {"must_contain": [r"\b14 days\b"], "must_not_contain": [r"\b30 days\b"]}
    assert score(item, "You have 14 days.") == []
    assert score(item, "You have 30 days.")
    assert score(item, "You have 14 days, or 30 days.")
