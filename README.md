# Tracewise

An agent that figures out **why** an AI support assistant gave a wrong answer — and proves it.

When a support bot answers wrong, the usual verdict is "the model hallucinated." Usually that's
wrong: the model did its job with bad inputs — a stale document, a tool that timed out and returned
nothing, a retrieval miss. Tracewise traces the failure to the step that actually broke, **verifies
every piece of evidence it cites** against the real data, and **proves the fix by replaying the
conversation** with the suspected cause patched. When the evidence isn't there, it abstains instead
of guessing.

Faults are injected on purpose, so the true cause of each case is known. Every number below comes
from a **locked held-out split the prompt was never tuned on**.

## Results

23 held-out failures. One run.

| method | top-1 | hallucinated evidence | fix proven by replay | $/case |
|---|---|---|---|---|
| **Tracewise** | **52%** | **0%** | **83%** | $0.078 |
| same model, no tools | 43% | 0% | 0% | $0.005 |
| blame the error span | 0% | 0% | 0% | — |

"Blame the error span" scores 0% because these faults *leave no error span* — a swallowed timeout
is an OK span, a stale document retrieves fine. The no-tools baseline is the real one, and it's
strong: the same model, handed the transcript and the change list, gets 43%. Tracewise's edge isn't
answering more — it answers *less*. It's that **when it answers, it's right 12 out of 12**, cites
nothing it can't support, and proves 83% of its fixes by replay. The baseline's 43% comes with no
way to tell which 43% to trust.

It abstains on about half the cases rather than guess — every abstention ran out of tool-call budget
holding the right idea but one signal short. That's a budget limit, not a reasoning one, and it was
left un-tuned on purpose: the split was opened once, and tuning on it would make the number dishonest.

## See one investigation

![Tracewise investigating a held-out silent tool failure](docs/demo.gif)

*One real, unedited run. The bot says a customer has no orders; the order exists. Every span reports
OK, so the error-span heuristic has nothing and the no-tools baseline blamed the model. Tracewise
finds the tool call that returned nothing, confirms the timeout in the logs, has one replay patch
**refused** for citing a value the history never showed, then proves the cause with a legitimate one.*

## A worked example

The readable version of the run above — a silent tool failure, held out.

**The bot says it can't find the customer's order, so it refuses the return.** The order exists and
is inside the window, so the answer should be yes. **Nothing in the trace is marked as an error:**
the lookup span is status OK; it just returned nothing.

1. Tracewise goes **upstream** instead of blaming the model — pulls the trace, logs, recent changes.
2. It finds **two independent signals** at the same component: the tool span returned 0 results, and
   a log line shows the lookup timed out and returned empty.
3. Its first **replay patch is refused** for citing a value the change log never shows. It recovers
   with a legitimate patch; the answer comes back correct and an unpatched control still fails — so
   the replay *proves* the fix.
4. It **rules out the decoy** — an unrelated document change sitting in the log just before the
   failure — instead of blaming the coincidence.

Diagnosis: `tool:lookup_order`, timeout misconfigured. Evidence verified, fix confirmed by replay —
the same case the no-tools baseline called a hallucination.

Steps 1–3 are the run in the GIF. Step 4 is from the scored run this case's numbers come from
(`eval/results/investigator-test-r1/case-023.json`): the agent isn't deterministic and writes a
`ruled_out` entry about half the time, and the recorded run didn't.

## What makes the result trustworthy

- **The agent can't see the answers.** No "correct version" flag anywhere it can read; ground truth
  lives only in the evaluator. It reasons from dates, diffs and evidence like a real on-call engineer.
- **Decoys.** The change feed mixes real changes with innocent ones, unlabelled. It has to not blame
  the coincidence.
- **Replay can't cheat.** Each replay applies one patch whose target component is fixed in code — it
  can't "fix" the answer by changing something unrelated and taking credit.
- **A code gate, not the model, decides.** A diagnosis is accepted only with two independent pieces
  of evidence, or one plus a proven replay — which is what holds hallucinated evidence at 0%.

## Known limits

Synthetic assistant, one domain, faults injected one at a time. Small benchmark (23 scored cases, so
one case moves top-1 ~4 points). Abstains on half — useful precision, limited coverage. Stated
plainly because the project is built to find out whether the agent is good, not to look good.

## How it's built

```text
support assistant (every step an OpenTelemetry span)
   | traces / logs / metrics          fault injector --> change log (+ decoys)
   v
MCP tool server (10 tools, typed refs the verifier can re-check)
   v
LangGraph investigator: triage -> hypothesize -> investigate -> verify (code) -> conclude | abstain
   v
evaluator: evidence verifier + replay checker + scorer   (ground truth lives here only)
```

**Python · LangGraph · Claude / Gemini · MCP · OpenTelemetry · Jaeger · OpenSearch · pgvector**

Once the stack is up and seeded ([setup](ARCHITECTURE.md#run-it)), you can watch an investigation
run against a recorded case:

```bash
python -m eval.replay_case --cases case-023 --verbose
```

*Full build & component reference in [`ARCHITECTURE.md`](ARCHITECTURE.md); design decisions in
[`DECISIONS.md`](DECISIONS.md).*
