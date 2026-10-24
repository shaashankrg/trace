"""The verification gate: plain code that decides whether a drafted diagnosis has enough evidence.
No model calls.

It accepts the draft's hypothesis (its root_component) only if

  1. every ref the draft cites (evidence, ruled_out, cause_ref) is one a tool returned
     during this investigation, i.e. it is in the evidence pool;
  2. counting only items RELEVANT to the hypothesis, by the fixed mapping in `components_for`
     (a chunk supports knowledge_base or retriever, a tool.X span or log supports tool:X, a change
     supports whatever its target maps to, ...);
  3. either at least 2 distinct evidence types support it, or at least 1 does and a replay
     whose patch targets the hypothesis fixed the answer while the unpatched control did not;
  4. playbook and past-incident results never count (failure scenario F9).

The component an item supports comes from the item's `meta`, which agent/evidence.py filled in
from the tool output. The draft's own `supports` field is ignored.
"""
from dataclasses import asdict, dataclass, field

from tools.replay import CONFIG_TARGETS

NEVER_COUNTS = {"playbook", "incident"}           # F9: retrieved memory is a hint, never evidence
ROOT_CONFIG_PREFIX = "config."


def _config_component(key: str) -> str:
    if key == "prompt_version":
        return "prompt"
    return CONFIG_TARGETS.get(key, "config")


def components_for(item: dict, attribute: str | None = None) -> set[str]:
    """The components a pool item can support. `attribute` narrows a root span to one config key."""
    kind, meta = item["type"], item.get("meta") or {}
    if kind in NEVER_COUNTS:
        return set()
    if kind == "trace":
        name = meta.get("span", "")
        if name.startswith("tool."):
            return {f"tool:{name.removeprefix('tool.')}"}
        if name == "retrieve":
            return {"retriever", "knowledge_base"}
        if name == "guardrail":
            return {"guardrail"}
        if name == "llm.chat":
            return {"model", "prompt"}
        if meta.get("root"):
            key = (attribute or "").removeprefix("kestrel.").removeprefix(ROOT_CONFIG_PREFIX)
            if key and f"{ROOT_CONFIG_PREFIX}{key}" in meta.get("attrs", {}):
                return {_config_component(key)}
            return {"config"}
        return set()
    if kind == "chunk":
        return {"knowledge_base", "retriever"}
    if kind == "log":
        component = meta.get("component", "")
        if component.startswith("tool."):
            return {f"tool:{component.removeprefix('tool.')}"}
        return {"retrieve": {"retriever"}, "retrieval": {"retriever"}, "guardrail": {"guardrail"}}.get(component, set())
    if kind == "change":
        target = meta.get("target", "")
        if target.startswith("doc:"):
            return {"knowledge_base"}
        if target.startswith("config:"):
            return {_config_component(target.removeprefix("config:"))}
        if target == "prompt":
            return {"prompt"}
        return set()
    if kind == "replay":
        return {meta["target_component"]} if meta.get("proves_fix") and meta.get("target_component") else set()
    return set()  # metrics show the symptom, not which component caused it


@dataclass
class GateResult:
    passed: bool
    reason: str
    hypothesis: str | None = None
    supporting_types: list[str] = field(default_factory=list)
    replay_ref: str | None = None
    unresolved: list[str] = field(default_factory=list)
    irrelevant: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def cited_refs(draft: dict) -> list[str]:
    refs = [e["ref"] for e in draft.get("evidence", [])]
    refs += [r["ref"] for r in draft.get("ruled_out", []) if r.get("ref")]
    if draft.get("cause_ref"):
        refs.append(draft["cause_ref"])
    return refs


def gate(draft: dict, pool: list[dict]) -> GateResult:
    if draft.get("abstained") or not draft.get("root_component"):
        return GateResult(False, "the draft names no root component")
    hypothesis = draft["root_component"]
    by_ref = {e["ref"]: e for e in pool}

    unresolved = [r for r in cited_refs(draft) if r not in by_ref]
    if unresolved:
        return GateResult(False, f"cites refs no tool returned in this investigation: {', '.join(unresolved)}",
                          hypothesis, unresolved=unresolved)

    types, irrelevant, replay = set(), [], None
    for item in draft.get("evidence", []):
        found = by_ref[item["ref"]]
        if hypothesis not in components_for(found, item.get("attribute")):
            irrelevant.append(item["ref"])
        elif found["type"] == "replay":
            replay = item["ref"]
        else:
            types.add(found["type"])

    if len(types) >= 2 or (types and replay):
        how = f"{len(types)} evidence types ({', '.join(sorted(types))})" + (f" and replay {replay}" if replay else "")
        return GateResult(True, f"{how} support {hypothesis}", hypothesis, sorted(types), replay,
                          irrelevant=irrelevant)

    have = f"{len(types)} evidence type{'s' if len(types) != 1 else ''}" + (f" ({', '.join(sorted(types))})" if types else "")
    need = "no replay that fixes the answer by patching it" if not replay else "a replay but no other evidence type"
    reason = f"only {have} relevant to {hypothesis}, {need}"
    if irrelevant:
        reason += f"; not relevant to {hypothesis}: {', '.join(irrelevant)}"
    return GateResult(False, reason, hypothesis, sorted(types), replay, irrelevant=irrelevant)
