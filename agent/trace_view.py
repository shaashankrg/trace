"""A live view of one investigation, for watching it work instead of waiting 95 seconds in silence.

Everything printed here is read out of the graph's own state as it runs: the node it entered, the
tool it called with the arguments it chose, the typed evidence `agent/evidence.py` built from the
tool's reply, and the verification gate's own verdict. Nothing is staged, re-ordered or restated
from a script — if a line appears, the investigation produced the fact behind it, and a step that
did not happen prints nothing.

Two facts here are derived rather than copied, and both are plain arithmetic over real data:
`span statuses` counts ERROR statuses in the trace the agent just pulled, and the running totals
are sums of the transcript. That is the honest version of the premise this project rests on - the
trace looks fine - and it is worth saying out loud rather than leaving the reader to count spans.

Used by `eval/replay_case.py --verbose`. It is a reader: it never feeds anything back into the
graph, so a run with the view on reaches the same diagnosis as a run without it.
"""
from __future__ import annotations

import json
import shutil
import sys

# ASCII only: Windows consoles default to cp1252, which cannot encode box-drawing characters, and
# a crash in the view would take the investigation down with it.
BAR = "-"
EVIDENCE_MARK = {"trace": "span", "log": "log", "chunk": "chunk", "change": "change",
                 "metric": "metric", "replay": "replay"}
EVIDENCE_SHOWN = 3        # per tool call; the rest are counted, not listed


def _w(limit: int = 96) -> int:
    return min(shutil.get_terminal_size((limit, 24)).columns, limit)


def _cut(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) > n:
        text = text[: max(n - 3, 1)] + "..."
    # Tool output and model text can carry characters a cp1252 console cannot encode; the view is
    # a reader and must never be the thing that fails a run.
    return text.encode("ascii", "replace").decode("ascii")


class TraceView:
    """Prints each new piece of state the graph produces. `on_state` is called with the whole state
    after every node; it diffs against what it has already shown and prints only the new part."""

    def __init__(self, case_id: str, stream=None, width: int | None = None) -> None:
        self.case_id = case_id
        self.out = stream or sys.stdout
        self.width = width or _w()
        self._nodes: list[str] = []
        self._transcript = 0
        self._evidence = 0
        self._candidates = False
        self._gate_rounds = 0

    # -- output helpers ----------------------------------------------------------------------
    def _p(self, line: str = "") -> None:
        try:
            print(line, file=self.out, flush=True)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode("ascii"), file=self.out, flush=True)

    def rule(self, title: str = "") -> None:
        if not title:
            self._p(BAR * self.width)
            return
        pad = max(self.width - len(title) - 3, 0)
        self._p(f"{BAR * 2} {title} {BAR * pad}")

    # -- the alert ---------------------------------------------------------------------------
    def alert(self, alert: dict) -> None:
        """The incident as it arrives: a reviewer-flagged answer, or a metric that fell."""
        self.rule(f"ALERT  {self.case_id}  ({alert.get('type', 'unknown')})")
        if alert.get("type") == "quality_drop":
            self._p(f"  metric    {alert.get('metric', 'canary accuracy')}: "
                    f"{alert.get('before')} -> {alert.get('after')}")
            for s in (alert.get("failing_samples") or [])[:2]:
                self._p(f"  failing   {_cut(s.get('question'), self.width - 12)}")
        if alert.get("question"):
            self._p(f"  asked     {_cut(alert['question'], self.width - 12)}")
        if alert.get("answer"):
            self._p(f"  answered  {_cut(alert['answer'], self.width - 12)}")
        if alert.get("reviewer_correction"):
            self._p(f"  truth     {_cut(alert['reviewer_correction'], self.width - 12)}")
        self._p()

    # -- per-node state ----------------------------------------------------------------------
    def on_state(self, state: dict) -> None:
        node = self._entered(state)
        if node:
            self._node_header(node, state)
        self._show_triage(state, node)
        self._show_candidates(state)
        self._show_transcript(state)
        self._show_evidence(state)
        self._show_gate(state)

    def _entered(self, state: dict) -> str | None:
        """Which node produced this state, taken from the last transcript entry's own `node`."""
        tr = state.get("transcript") or []
        node = tr[-1].get("node") if tr else None
        if node and (not self._nodes or self._nodes[-1] != node):
            self._nodes.append(node)
            return node
        if node is None and state.get("diagnosis") and "conclude/abstain" not in self._nodes:
            self._nodes.append("conclude/abstain")
            return "conclude/abstain"
        return None

    # Nodes that print their own, more informative header below (verify) or whose work is already
    # visible in the lines around them (draft, conclude, abstain) get no banner of their own.
    QUIET_NODES = {"verify", "draft", "conclude", "abstain", "conclude/abstain"}

    def _node_header(self, node: str, state: dict) -> None:
        if node in self.QUIET_NODES:
            return
        if node == "investigate" and self._nodes.count("investigate") > 1:
            return
        self.rule(node.upper())

    def _show_triage(self, state: dict, node: str | None) -> None:
        """Triage summarizes the trace and the change feed in code. The span-status count is the
        premise of the whole project, so it gets its own line."""
        text = state.get("triage")
        if not text or getattr(self, "_triaged", False):
            return
        self._triaged = True
        lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
        for line in lines:
            self._p(f"  {_cut(line, self.width - 4)}")
        # The premise, counted off the span lines triage just printed rather than asserted: each
        # one carries its own "[STATUS, Nms]". If any span were ERROR, this line would say so.
        spans = [ln for ln in lines if ln.startswith("- ") and "[" in ln]
        if spans:
            errors = sum(1 for ln in spans if "[ERROR" in ln.upper())
            verdict = (f"{errors} of {len(spans)} spans report ERROR" if errors
                       else f"no span reports an error ({len(spans)} spans, all OK) - "
                            f"nothing here looks broken")
            self._p(f"  => {verdict}")
        self._p()

    def _show_candidates(self, state: dict) -> None:
        cands = state.get("candidates") or []
        if not cands or self._candidates:
            return
        self._candidates = True
        for i, c in enumerate(cands[:5], 1):
            self._p(f"  {i}. {c.get('root_component', '?'):<22} {c.get('score', 0):.2f}  "
                    f"{_cut(c.get('rationale'), self.width - 36)}")
        self._p()

    def _show_transcript(self, state: dict) -> None:
        tr = state.get("transcript") or []
        for entry in tr[self._transcript:]:
            if entry.get("kind") == "tool":
                self._tool(entry)
        self._transcript = len(tr)

    def _tool(self, entry: dict) -> None:
        args = entry.get("args") or {}
        shown = {k: v for k, v in args.items() if k not in ("conversation_id",)}
        arg_text = ", ".join(f"{k}={json.dumps(v, default=str)}" if not isinstance(v, str) else f"{k}={v}"
                             for k, v in shown.items())
        head = f"  -> {entry.get('tool')}({_cut(arg_text, max(self.width - 34, 20))})"
        self._p(head)
        if entry.get("error"):
            # A rejected patch or a failed call is part of the investigation, not noise to hide.
            self._p(f"     refused: {_cut(entry['error'], self.width - 15)}")
        else:
            self._p(f"     {entry.get('result_chars', 0)} chars")

    def _show_evidence(self, state: dict) -> None:
        """Typed evidence, built in code from the tool's reply. The ref is what the verifier later
        resolves against the raw stores, so it is printed exactly as the tool returned it.

        One broad `search_logs` can add twenty items at once. Printing all of them buries the
        step that matters, so each step shows the first few and then says how many more it added -
        the count is real and the full set is in the run's JSON."""
        ev = state.get("evidence") or []
        new = ev[self._evidence:]
        self._evidence = len(ev)
        for e in new[:EVIDENCE_SHOWN]:
            kind = EVIDENCE_MARK.get(e.get("type"), e.get("type"))
            self._p(f"     [{kind}] {_cut(e.get('ref'), 46)}")
            self._p(f"            {_cut(e.get('claim'), self.width - 13)}")
        if len(new) > EVIDENCE_SHOWN:
            self._p(f"     ... {len(new) - EVIDENCE_SHOWN} more item(s) from this call")

    def _show_gate(self, state: dict) -> None:
        """The verification gate's own verdict: plain code in agent/verify.py, no model call."""
        gate = state.get("gate")
        rounds = state.get("rounds") or 0
        if not gate or rounds <= self._gate_rounds:
            return
        self._gate_rounds = rounds
        self.rule("VERIFY  (plain code, no model call)")
        draft = state.get("draft") or {}
        if draft.get("root_component"):
            self._p(f"  claim     {draft['root_component']} / {draft.get('category')}")
        verdict = "ACCEPTED" if gate.get("passed") else "REJECTED"
        self._p(f"  verdict   {verdict}")
        reason = gate.get("reason") or gate.get("missing") or ""
        if reason:
            self._p(f"  because   {_cut(reason, self.width - 12)}")
        self._p()

    # -- the ending --------------------------------------------------------------------------
    def finish(self, diagnosis: dict, verification: dict | None, fix: dict | None,
               record: dict | None = None) -> None:
        """The diagnosis, then what the evaluator independently made of it."""
        self.rule("DIAGNOSIS")
        if diagnosis.get("abstained"):
            self._p("  ABSTAINED - not enough evidence inside the budget")
            top = diagnosis.get("top_candidate") or {}
            if top.get("root_component"):
                self._p(f"  top candidate was {top['root_component']} / {top.get('category')}")
            if diagnosis.get("missing_evidence"):
                self._p(f"  missing   {_cut(diagnosis['missing_evidence'], self.width - 12)}")
        else:
            self._p(f"  {diagnosis.get('root_component')} / {diagnosis.get('category')}"
                    f"   confidence {diagnosis.get('confidence')}")
            self._p(f"  {_cut(diagnosis.get('summary'), self.width - 4)}")
            for e in diagnosis.get("evidence") or []:
                self._p(f"    cites [{EVIDENCE_MARK.get(e.get('type'), e.get('type'))}] "
                        f"{_cut(e.get('ref'), 46)}")
            for r in diagnosis.get("ruled_out") or []:
                self._p(f"    ruled out {_cut(r.get('change'), 28)}: "
                        f"{_cut(r.get('reason'), max(self.width - 44, 20))}")
            if diagnosis.get("suggested_fix"):
                self._p(f"  fix       {_cut(diagnosis['suggested_fix'], self.width - 12)}")
        self._p()

        self.rule("EVALUATOR  (independent of the agent)")
        if verification is not None:
            bad = verification.get("hallucinated")
            n = verification.get("checked", len(diagnosis.get("evidence") or []))
            self._p(f"  evidence  {'HALLUCINATED' if bad else f'all {n} cited refs resolve in the raw stores'}")
            for failure in (verification.get("failures") or [])[:3]:
                self._p(f"            {_cut(failure if isinstance(failure, str) else json.dumps(failure, default=str), self.width - 14)}")
        if fix:
            if fix.get("error"):
                self._p(f"  fix       not testable: {_cut(fix['error'], self.width - 26)}")
            else:
                self._p(f"  fix       replayed on {fix.get('target_component')}: "
                        f"answer {'matches' if fix.get('matches_correction') else 'does not match'}"
                        f" the correction, control {'fails' if fix.get('proves_fix') else 'unchanged'}"
                        f" -> proves_fix={fix.get('proves_fix')}")
        if record:
            self._p(f"  budget    {record.get('tool_calls_used')} tool calls, "
                    f"{record.get('replays_used')} replays, {record.get('rounds')} draft(s), "
                    f"${record.get('cost_usd', 0):.4f}")
        self.rule()
