"""The evidence verifier against a real recording (tests/fixtures/case-007) and a replay log.
It must catch the four kinds of made-up evidence: an invented ref, a real chunk with a wrong
quote, a wrong attribute value, and a failed replay cited as a success."""
import json
from pathlib import Path

import pytest

from eval import verifier

CASE = Path(__file__).resolve().parent / "fixtures" / "case-007"
TRACE = "c3ccf0cb7092aa97ad23c056bad187ac"
TOOL_SPAN = f"span:{TRACE}/30253ebd5cda320e"
RETRIEVE_SPAN = f"span:{TRACE}/c52e649f0e61a91c"
LOG = "log:769a9660-4f81-46b4-b2c5-887857816d34"
CHUNK = "chunk:refund-policy@v2#0"


@pytest.fixture
def replay_log(tmp_path) -> Path:
    path = tmp_path / "replays.jsonl"
    rows = [{"replay_id": "aaa111", "matches_correction": True}, {"replay_id": "bbb222", "matches_correction": False}]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def check(items: list[dict], replay_log: Path | None = None, **extra) -> dict:
    return verifier.verify({"evidence": items, **extra}, CASE, [replay_log] if replay_log else [])


def chunk_text() -> str:
    chunks = json.loads((CASE / "chunks.json").read_text(encoding="utf-8"))
    return next(c["text"] for c in chunks if c["chunk_id"] == CHUNK.removeprefix("chunk:"))


def test_true_citations_pass(replay_log):
    words = " ".join(chunk_text().split()[2:8])
    out = check([
        {"type": "trace", "ref": TOOL_SPAN, "claim": "c", "attribute": "tool.result_count", "value": 0},
        {"type": "trace", "ref": RETRIEVE_SPAN, "claim": "c", "attribute": "kestrel.retrieval.top_k", "value": "4"},
        {"type": "log", "ref": LOG, "claim": "c", "quote": "  Timed OUT after 1ms "},
        {"type": "chunk", "ref": CHUNK, "claim": "c", "quote": words.upper() + "…"},
        {"type": "replay", "ref": "replay:aaa111", "claim": "c", "attribute": "matches_correction", "value": True},
    ], replay_log)
    assert out == {"hallucinated": False, "checked": 5, "failures": []}


def test_a_made_up_chunk_id_is_caught():
    out = check([{"type": "chunk", "ref": "chunk:refund-policy@v7#3", "claim": "c"}])
    assert out["hallucinated"] and out["failures"][0]["problem"] == "no such chunk"


def test_a_real_chunk_with_a_wrong_quote_is_caught():
    out = check([{"type": "chunk", "ref": CHUNK, "claim": "c", "quote": "within 90 days of delivery"}])
    assert out["hallucinated"] and out["failures"][0]["problem"] == "quote not in the chunk text"


def test_a_wrong_attribute_value_is_caught():
    out = check([{"type": "trace", "ref": TOOL_SPAN, "claim": "c", "attribute": "tool.result_count", "value": 3}])
    assert out["hallucinated"] and "is 0, not 3" in out["failures"][0]["problem"]
    out = check([{"type": "trace", "ref": TOOL_SPAN, "claim": "c", "attribute": "tool.cache_age_s", "value": 1}])
    assert out["hallucinated"] and "has no attribute" in out["failures"][0]["problem"]


def test_a_failed_replay_cited_as_a_success_is_caught(replay_log):
    out = check([{"type": "replay", "ref": "replay:bbb222", "claim": "fixed it",
                  "attribute": "matches_correction", "value": True}], replay_log)
    assert out["hallucinated"] and "matches_correction=False" in out["failures"][0]["problem"]
    out = check([{"type": "replay", "ref": "replay:zzz999", "claim": "c"}], replay_log)
    assert out["failures"][0]["problem"] == "no such replay in the replay log"


def test_ruled_out_and_cause_refs_are_checked(replay_log):
    out = check([], replay_log, ruled_out=[{"change": "x", "reason": "y", "ref": "change:config-change-0000"}],
                cause_ref="change:invented")
    assert out["hallucinated"] and {f["where"] for f in out["failures"]} == {"ruled_out", "cause_ref"}


def test_the_verifier_reads_snapshot_files_not_the_tools():
    import ast
    tree = ast.parse((Path(verifier.__file__)).read_text(encoding="utf-8"))
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.split(".")[0] in {"tools", "agent"} for m in imported), imported


@pytest.mark.parametrize("claimed, actual, same", [
    (0, 0, True), ("4", 4, True), (0.85, 0.8500001, True), (0.3, 0.85, False),
    (True, "true", True), (True, 1, False), (["a", "b"], '["a","b"]', True), ("Allow", "allow", True),
])
def test_value_comparison(claimed, actual, same):
    assert verifier.same_value(claimed, actual) is same
