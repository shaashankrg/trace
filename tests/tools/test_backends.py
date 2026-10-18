"""Backend behaviour and the output rules, on synthetic snapshots so nothing depends on a model."""
import json
from datetime import datetime, timezone

import pytest

from tools import handlers, output, refs
from tools.backend import SnapshotBackend, get_backend

TRACE_ID = "a" * 32
T0 = datetime(2026, 10, 14, 3, 0, tzinfo=timezone.utc)


def write_case(tmp_path, **files):
    case = tmp_path / "case-x"
    case.mkdir()
    files.setdefault("alert", {"conversation_id": "conv-1", "trace_id": TRACE_ID})
    for name, data in files.items():
        (case / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
    return SnapshotBackend("case-x", tmp_path)


def test_get_backend_reads_the_environment(tmp_path, monkeypatch):
    write_case(tmp_path)
    monkeypatch.setenv("TRACEWISE_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("TRACEWISE_BACKEND", "snapshot:case-x")
    assert get_backend().case_id == "case-x"
    monkeypatch.setenv("TRACEWISE_BACKEND", "live")
    assert type(get_backend()).__name__ == "LiveBackend"
    for bad in ("snapshot:", "snapshot", "postgres", "snapshot:../x"):
        with pytest.raises(ValueError):
            get_backend(bad, tmp_path)


def test_a_missing_snapshot_file_reads_as_empty_not_as_a_crash(tmp_path):
    b = write_case(tmp_path)
    assert b.changes(None, None) == [] and b.prompt_versions() == [] and b.metric_names() == []
    assert handlers.recent_changes(b) == {"total": 0, "returned": 0, "changes": []}
    assert "error" in handlers.document_history(b, "refund-policy")


def test_search_logs_reports_the_total_when_it_returns_fewer(tmp_path):
    logs = [{"log_id": f"l{i:03d}", "timestamp": f"2026-10-14T03:{i // 60:02d}:{i % 60:02d}.000+00:00",
             "level": "INFO", "component": "chat", "message": f"line {i} " + "x" * 600} for i in range(120)]
    b = write_case(tmp_path, logs=logs)
    out = handlers.search_logs(b, "chat", "", "", "", 20)
    assert out["total_matches"] == 120 and out["returned"] == 20 and out["truncated"] is True
    assert all(len(r["message"]) <= handlers.TEXT_LIMIT for r in out["logs"])
    assert len(json.dumps(out)) <= output.MAX_CHARS


def test_the_size_cap_drops_items_but_keeps_the_totals(tmp_path):
    logs = [{"log_id": f"l{i:03d}", "timestamp": f"2026-10-14T03:{i // 60:02d}:{i % 60:02d}.000+00:00",
             "level": "INFO", "component": "chat", "message": "y" * 600} for i in range(120)]
    b = write_case(tmp_path, logs=logs)
    out = handlers.search_logs(b, "", "", "", "", 50)  # 50 lines x ~450 chars is over 14,000
    assert len(json.dumps(out)) <= output.MAX_CHARS
    assert out["truncated"] is True and out["total_matches"] == 120
    assert 0 < len(out["logs"]) < 50 and out["returned"] == 50  # `returned` is what the backend gave; the list was cut


def test_enforce_cap_drops_a_lone_oversized_list_item_and_says_so():
    out = output.enforce_cap({"total": 1, "x": ["z" * 50_000]}, ("x",))
    assert out == {"total": 1, "x": [], "truncated": True}


def test_enforce_cap_gives_up_politely_on_an_oversized_scalar():
    out = output.enforce_cap({"diff": "z" * 50_000}, ("x",))
    assert out["error"] == "response too large" and out["truncated"] is True


def test_every_line_of_text_is_clipped_with_an_ellipsis():
    assert output.clip("abc", 5) == "abc"
    assert output.clip("abcdefghij", 5) == "abcd…"
    assert output.clip(None, 5) == ""


def test_limit_arguments_are_clamped(tmp_path):
    logs = [{"log_id": f"l{i}", "timestamp": f"2026-10-14T03:00:{i:02d}.000+00:00", "level": "INFO",
             "component": "chat", "message": "m"} for i in range(30)]
    b = write_case(tmp_path, logs=logs)
    assert handlers.search_logs(b, limit=0)["returned"] == 1
    assert handlers.search_logs(b, limit=10_000)["returned"] == 30
    assert handlers.search_logs(b, limit=5)["returned"] == 5


def test_component_filter_matches_exact_or_dotted_prefix(tmp_path):
    comps = ["tool.lookup_order", "tool.check_inventory", "toolbox", "chat"]
    logs = [{"log_id": f"l{i}", "timestamp": f"2026-10-14T03:00:0{i}.000+00:00", "level": "INFO",
             "component": c, "message": "m"} for i, c in enumerate(comps)]
    b = write_case(tmp_path, logs=logs)
    assert handlers.search_logs(b, "tool")["total_matches"] == 2
    assert handlers.search_logs(b, "tool.lookup_order")["total_matches"] == 1
    assert handlers.search_logs(b, "toolbox")["total_matches"] == 1


def test_metric_refs_resolve_only_at_real_sample_times(tmp_path):
    ts = int(T0.timestamp())
    metrics = {"kestrel_canary_accuracy": [
        {"metric": {"__name__": "kestrel_canary_accuracy", "suite": "smoke"},
         "values": [[ts + 15 * i, str(1 - i / 10)] for i in range(5)]}]}
    b = write_case(tmp_path, metrics=metrics)
    out = handlers.query_metrics(b, "kestrel_canary_accuracy")
    point = out["series"][0]["points"][2]
    assert refs.resolve_ref(b, point["ref"])["record"]["value"] == point["value"]
    with pytest.raises(refs.RefError):
        refs.resolve_ref(b, refs.metric_ref("kestrel_canary_accuracy", ts + 7))
    with pytest.raises(refs.RefError):
        refs.resolve_ref(b, refs.metric_ref("other_metric", ts))


def test_query_metrics_picks_evenly_spaced_points_and_keeps_the_ends(tmp_path):
    ts = int(T0.timestamp())
    values = [[ts + 15 * i, "0.5"] for i in range(100)]
    b = write_case(tmp_path, metrics={"m": [{"metric": {"__name__": "m"}, "values": values}]})
    series = handlers.query_metrics(b, "m")["series"][0]
    assert series["count"] == 100 and len(series["points"]) == handlers.POINTS_PER_SERIES
    assert series["points"][0]["ts"] == refs.format_ts(ts)
    assert series["points"][-1]["ts"] == refs.format_ts(ts + 15 * 99)


def test_metric_window_is_applied(tmp_path):
    ts = int(T0.timestamp())
    values = [[ts + 60 * i, "1"] for i in range(10)]
    b = write_case(tmp_path, metrics={"m": [{"metric": {"__name__": "m"}, "values": values}]})
    out = handlers.query_metrics(b, "m", refs.format_ts(ts + 120), refs.format_ts(ts + 300))
    assert out["series"][0]["count"] == 4


def test_document_history_diff_names_the_change(tmp_path):
    docs = [
        {"doc_id": "d", "version": "v1", "title": "D", "published_at": "2025-01-01T00:00:00+00:00",
         "body": "Returns are accepted within 30 days of delivery for a full refund.",
         "chunk_count": 1, "indexed_chunks": 1},
        {"doc_id": "d", "version": "v2", "title": "D", "published_at": "2026-01-01T00:00:00+00:00",
         "body": "Returns are accepted within 14 days of delivery for a full refund.",
         "chunk_count": 1, "indexed_chunks": 1},
    ]
    out = handlers.document_history(write_case(tmp_path, documents=docs), "d")
    assert out["versions"][0]["diff_vs_previous"] == "first version"
    assert "[30 -> 14]" in out["versions"][1]["diff_vs_previous"]
    assert [v["indexed_chunk_count"] for v in out["versions"]] == [1, 1]


def test_prompt_diff_is_capped_at_80_lines(tmp_path):
    versions = [
        {"version": "v1", "template": "\n".join(f"line {i}" for i in range(200)), "created_at": "2026-01-01T00:00:00+00:00"},
        {"version": "v2", "template": "\n".join(f"LINE {i}" for i in range(200)), "created_at": "2026-02-01T00:00:00+00:00"},
    ]
    out = handlers.diff_prompt_versions(write_case(tmp_path, prompt_versions=versions), "v1", "v2")
    assert out["diff_lines"] > 80 and out["truncated"] is True
    assert out["diff"].count("\n") + 1 == 80
