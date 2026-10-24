"""The verification gate, rule by rule. Pure code: pool items in, a verdict out."""
from agent.verify import components_for, gate

SPAN_TOOL = {"type": "trace", "ref": "span:" + "a" * 32 + "/" + "1" * 16, "claim": "c",
             "meta": {"span": "tool.lookup_order", "root": False, "attrs": {"tool.result_count": 0}}}
SPAN_ROOT = {"type": "trace", "ref": "span:" + "a" * 32 + "/" + "2" * 16, "claim": "c",
             "meta": {"span": "POST /chat", "root": True,
                      "attrs": {"config.tool_timeout_ms": 1, "config.similarity_threshold": 0.85}}}
SPAN_RETRIEVE = {"type": "trace", "ref": "span:" + "a" * 32 + "/" + "3" * 16, "claim": "c",
                 "meta": {"span": "retrieve", "root": False, "attrs": {}}}
LOG_TOOL = {"type": "log", "ref": "log:1", "claim": "c", "meta": {"component": "tool.lookup_order"}}
LOG_CHAT = {"type": "log", "ref": "log:2", "claim": "c", "meta": {"component": "chat"}}
CHUNK = {"type": "chunk", "ref": "chunk:warranty@v1#0", "claim": "c", "meta": {"doc_id": "warranty"}}
DOC_CHANGE = {"type": "change", "ref": "change:doc-update-1036", "claim": "c",
              "meta": {"target": "doc:warranty", "kind": "doc_update"}}
CONFIG_CHANGE = {"type": "change", "ref": "change:config-change-1", "claim": "c",
                 "meta": {"target": "config:max_response_tokens", "kind": "config_change"}}
THRESHOLD_CHANGE = {"type": "change", "ref": "change:config-change-2", "claim": "c",
                    "meta": {"target": "config:similarity_threshold", "kind": "config_change"}}
METRIC = {"type": "metric", "ref": "metric:kestrel_canary_accuracy@2026-10-17T23:00Z", "claim": "c",
          "meta": {"metric": "kestrel_canary_accuracy"}}
REPLAY_KB = {"type": "replay", "ref": "replay:abc", "claim": "c",
             "meta": {"target_component": "knowledge_base", "matches_correction": True, "proves_fix": True}}
REPLAY_FLAKY = {"type": "replay", "ref": "replay:def", "claim": "c",  # control was right too
                "meta": {"target_component": "knowledge_base", "matches_correction": True, "proves_fix": False}}
PLAYBOOK = {"type": "playbook", "ref": "playbook:stale-docs", "claim": "c", "meta": {}}
POOL = [SPAN_TOOL, SPAN_ROOT, SPAN_RETRIEVE, LOG_TOOL, LOG_CHAT, CHUNK, DOC_CHANGE, CONFIG_CHANGE,
        THRESHOLD_CHANGE, METRIC, REPLAY_KB, REPLAY_FLAKY, PLAYBOOK]


def draft(component: str, *items: dict, **extra) -> dict:
    return {"root_component": component, "category": "x", "abstained": False,
            "evidence": [{"type": i["type"], "ref": i["ref"], "claim": "c", **extra.get(i["ref"], {})} for i in items],
            "ruled_out": extra.get("ruled_out", []), "cause_ref": extra.get("cause_ref")}


# rule 1: every cited ref resolves --------------------------------------------------------------

def test_a_ref_no_tool_returned_fails_the_gate_whatever_else_is_cited():
    d = draft("tool:lookup_order", SPAN_TOOL, LOG_TOOL)
    d["evidence"].append({"type": "chunk", "ref": "chunk:made-up@v1#0", "claim": "c"})
    r = gate(d, POOL)
    assert not r.passed and r.unresolved == ["chunk:made-up@v1#0"]


def test_ruled_out_refs_and_cause_ref_must_resolve_too():
    d = draft("tool:lookup_order", SPAN_TOOL, LOG_TOOL, ruled_out=[{"change": "x", "reason": "y", "ref": "change:nope"}])
    assert gate(d, POOL).unresolved == ["change:nope"]
    d = draft("knowledge_base", CHUNK, DOC_CHANGE, cause_ref="change:invented-1")
    assert gate(d, POOL).unresolved == ["change:invented-1"]


# rule 2: relevance by a fixed mapping ----------------------------------------------------------

def test_the_mapping():
    assert components_for(SPAN_TOOL) == {"tool:lookup_order"}
    assert components_for(LOG_TOOL) == {"tool:lookup_order"}
    assert components_for(CHUNK) == {"knowledge_base", "retriever"}
    assert components_for(SPAN_RETRIEVE) == {"retriever", "knowledge_base"}
    assert components_for(DOC_CHANGE) == {"knowledge_base"}
    assert components_for(THRESHOLD_CHANGE) == {"retriever"}
    assert components_for(CONFIG_CHANGE) == {"config"}
    assert components_for(SPAN_ROOT) == {"config"}
    assert components_for(SPAN_ROOT, "config.similarity_threshold") == {"retriever"}
    assert components_for(SPAN_ROOT, "kestrel.config.tool_timeout_ms") == {"config"}
    assert components_for(LOG_CHAT) == set() and components_for(METRIC) == set()


def test_evidence_about_another_component_does_not_count():
    # a decoy config change and a chat log line say nothing about the knowledge base
    r = gate(draft("knowledge_base", CHUNK, CONFIG_CHANGE, LOG_CHAT), POOL)
    assert not r.passed and set(r.irrelevant) == {CONFIG_CHANGE["ref"], LOG_CHAT["ref"]}
    assert "only 1 evidence type (chunk)" in r.reason


def test_the_draft_cannot_declare_relevance():
    d = draft("knowledge_base", CHUNK, LOG_CHAT, **{LOG_CHAT["ref"]: {"supports": "knowledge_base"}})
    assert not gate(d, POOL).passed


def test_the_pool_type_counts_not_the_type_the_draft_claims():
    d = draft("tool:lookup_order", SPAN_TOOL)
    d["evidence"].append({"type": "log", "ref": SPAN_TOOL["ref"], "claim": "same span, relabelled"})
    assert not gate(d, POOL).passed


# rule 3: two evidence types, or one plus a replay that fixed it ------------------------------------

def test_two_relevant_evidence_types_pass():
    r = gate(draft("tool:lookup_order", SPAN_TOOL, LOG_TOOL), POOL)
    assert r.passed and r.supporting_types == ["log", "trace"]


def test_one_type_twice_is_still_one_type():
    r = gate(draft("tool:lookup_order", SPAN_TOOL, SPAN_ROOT), POOL)  # root span maps to config, not the tool
    assert not r.passed and r.reason.startswith("only 1 evidence type (trace) relevant to tool:lookup_order")


def test_one_type_plus_a_replay_that_proved_the_fix_passes():
    r = gate(draft("knowledge_base", CHUNK, REPLAY_KB), POOL)
    assert r.passed and r.replay_ref == "replay:abc"


def test_a_replay_whose_control_was_also_right_proves_nothing():
    r = gate(draft("knowledge_base", CHUNK, REPLAY_FLAKY), POOL)
    assert not r.passed and "no replay that fixes the answer" in r.reason


def test_a_replay_counts_only_for_the_component_its_patch_targets():
    r = gate(draft("retriever", CHUNK, REPLAY_KB), POOL)
    assert not r.passed and REPLAY_KB["ref"] in r.irrelevant


def test_a_replay_alone_is_not_enough():
    r = gate(draft("knowledge_base", REPLAY_KB), POOL)
    assert not r.passed and "a replay but no other evidence type" in r.reason


# rule 4: playbooks and past incidents never count ---------------------------------------------------

def test_playbook_results_never_count():
    assert components_for(PLAYBOOK) == set()
    assert not gate(draft("knowledge_base", CHUNK, PLAYBOOK), POOL).passed


def test_an_abstaining_draft_is_not_accepted():
    assert not gate({"abstained": True, "evidence": []}, POOL).passed
