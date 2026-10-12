"""Fault and decoy tests. Need the stack up and `python -m eval.setup_eval` done.

They mutate the database and restore it with reset_healthy(), so do not run them while
`python -m eval.run_case` is running."""
import json
from datetime import datetime, timezone

import psycopg
import pytest

from assistant import db
from eval.setup_eval import connect_tools, setup
from injector import decoys, faults, make_cases, state

NOW = datetime.now(timezone.utc)

FAULT_SPECS = {
    "stale_document": {"doc_id": "refund-policy", "old_version": "v1", "new_version": "v2"},
    "missing_document": {"doc_id": "gift-cards"},
    "threshold_too_strict": {"value": 0.85},
    "context_overflow": {"value": 20},
    "silent_tool_failure": {"tool": "lookup_order"},
    "stale_tool_data": {"product_id": "P-003", "new_qty": 23},
    "prompt_regression": {},
}
DECOY_SPECS = {
    "typo_prompt_edit": {},
    "unrelated_doc_added": {"doc_id": "gear-donation"},
    "unrelated_config_change": {"value": 700},
}
CHANGE_KEYS = {"change_id", "kind", "target", "description", "diff", "author", "created_at"}


@pytest.fixture
def conn():
    with db.connect() as c:
        setup(c)
        state.reset_healthy(c)
        yield c
        state.reset_healthy(c)


def world(conn) -> dict:
    """Everything a fault can touch, minus timestamps."""
    q = lambda sql: [tuple(r) for r in conn.execute(sql).fetchall()]
    return {
        "config": q("SELECT key, value FROM app_config ORDER BY key"),
        "index": q("SELECT chunk_id, indexed FROM chunks ORDER BY chunk_id"),
        "prompts": q("SELECT version, template FROM prompt_versions ORDER BY version"),
        "docs": q("SELECT doc_id, version FROM documents ORDER BY doc_id, version"),
        "inventory": q("SELECT product_id, qty FROM inventory_source ORDER BY product_id"),
        "cache": q("SELECT product_id, qty FROM inventory_cache ORDER BY product_id"),
    }


def build_fault(conn, ftype: str) -> faults.Fault:
    spec = {"type": ftype, "params": FAULT_SPECS[ftype], "age_min": 30, "change_id": "change:test-0001",
            "description": "test"}
    return faults.build(conn, spec, NOW)


def build_decoy(conn, dtype: str) -> decoys.Decoy:
    spec = {"type": dtype, "params": DECOY_SPECS[dtype], "age_min": 30, "change_id": "change:test-0002",
            "description": "test"}
    return decoys.build(conn, spec, NOW)


@pytest.mark.parametrize("ftype", list(FAULT_SPECS))
def test_fault_applies_and_reverts_idempotently(conn, ftype):
    healthy = world(conn)
    fault = build_fault(conn, ftype)
    fault.apply()
    assert world(conn) != healthy, "apply() changed nothing"
    fault.revert()
    assert world(conn) == healthy
    fault.revert()  # apply -> revert -> revert
    assert world(conn) == healthy


@pytest.mark.parametrize("dtype", list(DECOY_SPECS))
def test_decoy_applies_and_reverts_idempotently(conn, dtype):
    healthy = world(conn)
    decoy = build_decoy(conn, dtype)
    decoy.apply()
    assert world(conn) != healthy
    decoy.revert()
    decoy.revert()
    assert world(conn) == healthy


def test_reverting_before_applying_is_harmless(conn):
    healthy = world(conn)
    for ftype in FAULT_SPECS:
        build_fault(conn, ftype).revert()
    for dtype in DECOY_SPECS:
        build_decoy(conn, dtype).revert()
    assert world(conn) == healthy


def test_outages_leave_no_change_record(conn):
    for ftype in ("stale_document", "silent_tool_failure", "stale_tool_data"):
        assert build_fault(conn, ftype).change_record() is None


@pytest.mark.parametrize("ftype", ["missing_document", "threshold_too_strict", "context_overflow", "prompt_regression"])
def test_changes_have_a_record_in_the_shared_format(conn, ftype):
    fault = build_fault(conn, ftype)
    fault.apply()
    rec = fault.change_record()
    assert set(rec) == CHANGE_KEYS
    assert rec["change_id"] == "change:test-0001"
    state.insert_change(conn, rec)
    row = conn.execute("SELECT kind FROM changes WHERE change_id = 'change:test-0001'").fetchone()
    assert row[0] == rec["kind"]


@pytest.mark.parametrize("dtype", list(DECOY_SPECS))
def test_decoy_records_look_like_real_ones(conn, dtype):
    decoy = build_decoy(conn, dtype)
    decoy.apply()
    rec = decoy.change_record()
    assert set(rec) == CHANGE_KEYS
    assert rec["kind"] in {"prompt_edit", "doc_update", "config_change"}
    assert rec["created_at"] < NOW


def test_decoys_are_real_changes(conn):
    typo = build_decoy(conn, "typo_prompt_edit")
    typo.apply()
    active = state.get_config(conn, "prompt_version")
    assert active != "v1"
    assert conn.execute("SELECT count(*) FROM prompt_versions").fetchone()[0] == 2

    doc = build_decoy(conn, "unrelated_doc_added")
    doc.apply()
    assert conn.execute("SELECT count(*) FROM chunks WHERE doc_id = 'gear-donation' AND indexed").fetchone()[0] > 0


def test_stale_document_cause_is_the_existing_v2_publish(conn):
    fault = build_fault(conn, "stale_document")
    cause = fault.cause_ref()
    row = conn.execute("SELECT target, description FROM changes WHERE change_id = %s", (cause,)).fetchone()
    assert row == ("doc:refund-policy", "Publish refund-policy@v2")


def test_stale_document_serves_both_versions(conn):
    fault = build_fault(conn, "stale_document")
    fault.apply()
    versions = {r[0] for r in conn.execute(
        "SELECT version FROM chunks WHERE doc_id = 'refund-policy' AND indexed").fetchall()}
    assert versions == {"v1", "v2"}


def test_ground_truth_labels(conn):
    truth = {t: build_fault(conn, t).ground_truth() for t in FAULT_SPECS}
    assert truth["silent_tool_failure"]["root_component"] == "tool:lookup_order"
    assert truth["stale_tool_data"]["root_component"] == "tool:check_inventory"
    assert truth["threshold_too_strict"]["root_component"] == "retriever"
    assert truth["prompt_regression"]["category"] == "prompt_edit"
    assert truth["missing_document"]["cause_ref"] == "change:test-0001"
    assert all(set(t) == {"root_component", "category", "cause_ref", "fault_type"} for t in truth.values())


def test_reset_healthy_clears_everything(conn):
    healthy = world(conn)
    for ftype in ("missing_document", "threshold_too_strict", "stale_tool_data", "prompt_regression"):
        f = build_fault(conn, ftype)
        f.apply()
        if f.change_record():
            state.insert_change(conn, f.change_record())
    build_decoy(conn, "unrelated_doc_added").apply()
    state.reset_healthy(conn)
    assert world(conn) == healthy
    ids = {r[0] for r in conn.execute("SELECT change_id FROM changes").fetchall()}
    assert ids == {r["change_id"] for r in state.baseline_changes()}


# --- the anti-cheating rule, enforced by Postgres -------------------------------------------

def test_tools_role_cannot_read_ground_truth(conn):
    with connect_tools() as tools:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            tools.execute("SELECT * FROM eval.cases")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            tools.execute("SELECT * FROM eval.case_runs")


def test_tools_role_reads_the_app_but_cannot_change_it(conn):
    with connect_tools() as tools:
        assert tools.execute("SELECT count(*) FROM changes").fetchone()[0] > 0
        assert tools.execute("SELECT count(*) FROM chunks").fetchone()[0] > 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            tools.execute("UPDATE app_config SET value = '1' WHERE key = 'top_k'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            tools.execute("DELETE FROM changes")


def test_tools_role_cannot_list_eval_tables(conn):
    with connect_tools() as tools:
        rows = tools.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'eval'").fetchall()
        assert rows == []


# --- cases ---------------------------------------------------------------------------------

def test_cases_are_deterministic_and_match_the_files_on_disk():
    cases = make_cases.build_cases()
    assert cases == make_cases.build_cases()
    on_disk = make_cases.load_cases()
    assert json.loads(json.dumps(cases)) == on_disk


def test_twenty_cases_half_with_decoys():
    cases = make_cases.build_cases()
    assert len(cases) == 20
    assert len({c["case_id"] for c in cases}) == 20
    assert len({c["conversation_id"] for c in cases}) == 20
    assert sum(1 for c in cases if c["decoy"]) == 10
    types = {c["fault"]["type"] for c in cases}
    assert types == set(faults.FAULTS) - {"context_overflow"}


def test_decoy_timing_is_mixed_and_never_fixed():
    decoy_cases = [c for c in make_cases.build_cases() if c["decoy"]]
    positions = {c["decoy"]["position"] for c in decoy_cases}
    assert positions == {"before", "after"}
    for c in decoy_cases:
        older = c["decoy"]["age_min"] > c["fault"]["age_min"]
        assert older == (c["decoy"]["position"] == "before")
    gaps = {abs(c["decoy"]["age_min"] - c["fault"]["age_min"]) for c in decoy_cases}
    assert len(gaps) > 3, "decoy gap should vary"


def test_every_case_has_a_checkable_answer_and_a_correction():
    for c in make_cases.build_cases():
        assert c["alert"]["type"] == "flagged_conversation"
        assert c["alert"]["reviewer_correction"]
        assert c["answer_check"]["must_contain"]
        assert not any("fault" in k for k in c["alert"])


def test_prompt_regression_never_gets_the_typo_decoy():
    for c in make_cases.build_cases():
        if c["fault"]["type"] == "prompt_regression" and c["decoy"]:
            assert c["decoy"]["type"] != "typo_prompt_edit"


def test_change_ids_are_unique_and_do_not_collide_with_history():
    cases = make_cases.build_cases()
    ids = [c["fault"].get("change_id") for c in cases] + [c["decoy"]["change_id"] for c in cases if c["decoy"]]
    ids = [i for i in ids if i]
    assert len(ids) == len(set(ids))
    assert not set(ids) & {r["change_id"] for r in state.baseline_changes()}


def test_eval_cases_loaded_with_ground_truth(conn):
    from eval.setup_eval import connect_eval
    with connect_eval() as ev:
        n, decoys_n = ev.execute("SELECT count(*), count(*) FILTER (WHERE has_decoy) FROM eval.cases").fetchone()
    assert (n, decoys_n) == (20, 10)
