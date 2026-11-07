# Tracewise - build and component reference

The full technical reference. For what Tracewise is, the held-out results and a worked example,
start at the [README](README.md); design decisions and their alternatives are in
[DECISIONS.md](DECISIONS.md).

```text
   Kestrel support assistant (FastAPI): guardrail -> retrieve (pgvector) -> llm.chat -> tool.* -> llm.chat
     every step an OpenTelemetry span
        |                    |                 |
     traces               logs             metrics            Fault injector ---> change log
        v                    v                 v                                  (+ decoys)
     Jaeger            OpenSearch         Prometheus
        \__________________ | _________________/
                            v
              MCP tool server (10 tools, compact output, typed refs)
                            v
        LangGraph investigator: triage -> hypothesize -> investigate
                            -> draft -> verify (code) -> conclude | abstain
                            v
     Evaluator: evidence verifier + replay checker + scorer   (ground truth lives here only)
```

Python - LangGraph - Gemini / Claude - MCP - OpenTelemetry - Jaeger - OpenSearch - Postgres + pgvector - Prometheus - Docker Compose

## How the benchmark works

A fault is injected into a healthy assistant, the failing conversation is recorded, and the fault is reverted. The
recorded snapshot — trace, logs, chunks, config, change log, store data — is what the investigator is run against
later, so results reproduce without re-injecting.

### The anti-cheating rule

The investigator cannot see the fault injector, its labels, or anything marking a version as the correct one. In
real life there is no file telling you what broke. Concretely:

- `document_history` returns every version with timestamps and indexed-chunk counts, and **no "current" flag**. The
  agent reasons from dates and the change log.
- `recent_changes` returns real changes **and decoys in the same format**, unlabelled. A decoy is a genuine but
  innocent change written shortly before the failure: a typo fix in the prompt, an unrelated document published, a
  config change to an unrelated setting.
- `replay_conversation` applies only patches built from what the agent can legitimately see — a document version
  from history, a prompt version, a config value present in the change log or on the trace. **The component a patch
  targets is fixed by the patch kind in code, never chosen by the agent**, so a replay cannot "fix" the answer by
  changing something unrelated and taking credit for it.
- Ground truth lives only in the evaluator: the `eval` schema, which the `tools` role the MCP server connects as
  cannot read.

### Two signals, or one plus a proven fix

`agent/verify.py` is plain code, not a model call. It accepts a diagnosis only when every cited ref came from a
tool during that investigation, and the evidence *relevant to the named component* covers two distinct types, or
one type plus a replay that fixed the answer by patching that component. Retrieved playbooks and past incidents
never count as evidence, only as hints. This is what keeps hallucinated evidence at 0% and precision at 100%, and
it is also what drives the 48% abstention rate — the two are the same mechanism.

### Splits

`train` (17 cases) is for tuning prompts. `dev` (5) was read during development and is never reported as held out.
`test` (28 locked) contains **every** case of two fault types the prompt never met during tuning — `embedding_mismatch`
and `guardrail_misfire` — so it measures generalization rather than recognition.

Two locks guard it. `eval/test_split.lock` pins the case ids and a hash of their JSON. `eval/frozen_prompt.sha`
pins the prompt **and the configuration that decides how it is used** (model, tool and replay budgets, escalation
threshold, output cap), because retuning a step budget changes the agent as much as a prompt edit would. A test run
refuses to start unless both still match, and names what moved if they do not. `eval/results/test_runs.log` records
every test-split run — the honest count of how many times the split has been looked at.

## Reproducing the results

```bash
python -m eval.b2 --split test                 # baseline B2 over the locked split
python -m eval.baselines b1                    # baseline B1 over the recordings
python -m eval.replay_case --agent investigator --split test   # refuses unless both locks match
python -m eval.score --compare \
    investigator=eval/results/investigator-test-r1/diagnoses.jsonl \
    b1=eval/diagnoses/b1_test.jsonl \
    b2=eval/diagnoses/b2_test.jsonl --split test
```

## Run it

```bash
cp .env.example .env            # set ANTHROPIC_API_KEY
docker compose up -d            # postgres, jaeger, otel-collector, opensearch, prometheus, pushgateway

python -m venv .venv && .venv/Scripts/activate   # or source .venv/bin/activate
pip install -r requirements.txt

python -m eval.render_docs      # facts.yaml + doc_templates/ -> assistant/docs/
python -m assistant.bootstrap   # schema, config, prompts, store data, ingest docs
uvicorn assistant.app:app --port 8000
```

```bash
curl -X POST localhost:8000/chat -H 'content-type: application/json' \
  -d '{"conversation_id":"c-001","question":"How long do I have to return boots?"}'
```

The response includes `trace_id`. Open http://localhost:16686, pick service `kestrel-assistant`, and paste the ID into the search box.

## Canary

```bash
python -m eval.make_canary      # rebuild assistant/canary.json from facts.yaml + store seed
python -m eval.canary           # all 60 questions; pushes kestrel_canary_* to Pushgateway
python -m eval.canary --smoke   # the 14-question subset
```

Accuracy is queryable in Prometheus (http://localhost:9092) as `kestrel_canary_accuracy`, with a `category` label for `docs`, `tools` and `multistep`.

Runtime config lives in the `app_config` table and is read on every request. `python -m assistant.bootstrap --reset-config` restores the defaults.

## Fault injection and evaluation

```bash
python -m eval.setup_eval          # `eval` schema + `eval` / `tools` roles (ground truth is unreadable by `tools`)
python -m injector.make_cases      # 52 cases -> injector/cases/*.json and eval.cases
python -m eval.run_case case-001   # reset, inject, record, snapshot to eval/runs/case-001/, revert, check recovery
python -m eval.run_case --all
python -m eval.baselines b1        # rule-based baseline over the recorded traces
python -m eval.score eval/diagnoses/b1.jsonl   # -> eval/results/b1.md, b1.csv, b1_cases.csv
```

Fault types are in `injector/faults.py`, decoys in `injector/decoys.py`. Each fault has `apply()`, an idempotent
`revert()`, a `change_record()` (or `None` for an outage) and a `ground_truth()`. Real changes and decoys are
written to the `changes` table in the same format.

Eleven fault types, nine of which produce cases:

| Fault | Component / category | Cases |
|---|---|---|
| `stale_document` | `knowledge_base` / `doc_update` | yes |
| `missing_document` | `knowledge_base` / `doc_delete` | yes |
| `threshold_too_strict` | `retriever` / `config_change` | yes |
| `context_overflow` | `retriever` / `config_change` | none: a bigger `top_k` only ever added recall |
| `silent_tool_failure` | `tool:lookup_order` / `tool_outage` | yes |
| `stale_tool_data` | `tool:check_inventory` / `stale_data` | yes |
| `prompt_regression` | `prompt` / `prompt_edit` | yes |
| `chunking_regression` | `retriever` / `retrieval_failure` | yes (sizes 40-80, per doc) |
| `embedding_mismatch` | `retriever` / `retrieval_failure` | yes, **held out** |
| `model_downgrade` | `model` / `model_change` | none: no available smaller model fails silently |
| `guardrail_misfire` | `guardrail` / `guardrail_error` | yes, **held out** |

The two "none" rows keep their class and their apply/revert tests; a fault that cannot make an answer wrong
repeatably is not a valid case. The probes behind both calls are in `eval/probes/`.

### Alert types

`flagged_conversation` is one reviewer-flagged answer. `quality_drop` is a metric that fell: `run_case` runs the
full 60-question canary while healthy and again under the fault, and the alert carries `metric`, `before`, `after`
and up to 5 `failing_samples` (traces for up to 10 of them are snapshotted). A quality drop is confirmed in replay
only when one patch fixes at least **2 of 3** sampled failing questions — one lucky fix is not evidence of a broad
cause (`replay.run_quality_replay`).

### Split and lock commands

See [Splits](#splits) above for what the splits are and why they are locked.

```bash
python -m eval.splits --show            # split sizes, test ids, hashes, test-run count
python -m eval.splits --write-lock      # pin the test split (once)
python -m eval.splits --check           # verify the lock against the cases on disk
python -m eval.splits --freeze-prompt   # pin the prompt + config after tuning on train
python -m eval.replay_case --split train
python -m eval.replay_case --split test # refuses unless both locks still match
```

## Investigator tools (MCP)

Ten tools behind one MCP server (stdio). Eight work; `search_playbooks` and `similar_incidents` (Day 10) are
stubs with their real signatures.

```bash
python -m tools.server                                   # live stack (Jaeger, OpenSearch, Postgres as `tools`, Prometheus)
TRACEWISE_BACKEND=snapshot:case-007 python -m tools.server   # a recording from eval/runs/case-007/
TRACEWISE_BACKEND=snapshot:case-007 TRACEWISE_REPLAY=1 python -m tools.server   # with replay (~30 s to start)
npx @modelcontextprotocol/inspector .venv/Scripts/python.exe -m tools.server
```

| Tool | Returns |
|---|---|
| `get_trace(conversation_id)` | span tree: ref, parent, duration, status, key attributes |
| `get_retrieved_chunks(span_ref)` | ref, doc, version, score, text (300 chars) per chunk |
| `document_history(doc_id)` | every version, indexed chunk count, short diff; no "current" flag |
| `search_logs(component, query, start, end, limit)` | log lines + `total_matches` |
| `recent_changes(start, end)` | prompt, doc and config changes, decoys included |
| `diff_prompt_versions(a, b)` | unified diff, 80 lines max |
| `query_metrics(metric, start, end)` | min/max/mean and up to 10 sample points per series |
| `replay_conversation(conversation_id, patch)` | the answer with one patch applied, `matches_correction`, `proves_fix` |

Every fact carries a typed ref (`tools/refs.py`): `span:<trace>/<span>`, `chunk:<doc>@<v>#<n>`, `doc:<doc>@<v>`,
`log:<id>`, `change:<id>`, `prompt:<v>`, `metric:<name>@<ts>`, `replay:<id>`. `resolve_ref(backend, ref)` looks
one up. Responses stay under about 4,000 tokens, lists carry a `total`, and errors come back
as `{"error", "hint"}`.

Tests (`pytest tests/tools`) run every tool against each recording in `tests/fixtures/` and, if the stack is up,
against the live backend. Fixtures are copies of `eval/runs/<case>/`; regenerate one with
`python -m eval.run_case --force <case>`.

## Replay

`tools/replay.py` re-runs the assistant's own chat loop (`assistant.pipeline.answer`) on a recording with exactly one
patch, in memory: retrieval over the snapshot's chunks (re-embedded), its prompt versions and config, and the tool
responses recorded for the alert's trace. A patched tool is re-run against `store.json` instead.

| Patch | Targets (set by code, never by the caller) |
|---|---|
| `{"kind": "use_doc_version", "doc_id", "version"}`, `{"kind": "restore_doc", "doc_id"}` | `knowledge_base` |
| `{"kind": "set_config", "key", "value"}` | `retriever` (top_k, threshold, embedding model, chunk_size), `model`, `guardrail`, else `config` |
| `{"kind": "set_prompt_version", "version"}` | `prompt` |
| `{"kind": "rerun_tool", "tool"}`, `{"kind": "bypass_cache", "tool": "check_inventory"}` | `tool:<tool>` |

Versions must exist in the snapshot, and a `set_config` value must appear in the change log or on the trace.
`chunk_size` is patchable because a re-chunked doc keeps both chunkings in the table at once, distinguished by a
chunk-ref suffix (`chunk:refund-policy@v2#3/c80`); switching it moves index flags rather than re-chunking anything. The
assistant model is not deterministic at temperature 0, so each replay takes 3 samples and the majority counts, and
every patched replay is compared with an unpatched control: `proves_fix` is true only when the patch fixes the answer
and the control does not. The answer is checked against the alert's `answer_check` patterns in code, with an LLM
judge only when the answer contains both the right and the wrong fact. Every replay goes to a JSONL replay log.

```bash
python -m eval.backfill_snapshots       # add store.json + answer_check to cases recorded before Day 6
python -m eval.replay_determinism       # empty-patch replay of every case vs. its recording
```

## Investigator (LangGraph)

```text
triage ──► hypothesize ──► investigate ⟲ ──► draft ──► verify ─┬─► conclude
               ▲                                              ├─► hypothesize   (rejected, budget left)
               └──────────────────────────────────────────────┘
                                                              └─► abstain       (rejected, budget gone)
```

`agent/` holds the graph. **triage** calls `get_trace` and `recent_changes` (7 days) and summarizes them in code.
**hypothesize** asks the model for 3 to 5 ranked candidates. **investigate** is a tool-use loop, one model turn
per visit, and stops when the model has no more calls or the budget (15 tool calls, 3 replays, enforced in
`Toolbox.call_tool`) is used up. **draft** writes the spec 9.3 diagnosis, validated with Pydantic. **verify**
(`agent/verify.py`, plain code) accepts it only if every cited ref came from a tool and the evidence relevant to
the named component covers 2 types, or 1 type plus a replay that proved the fix by patching that component. A
rejection goes back to hypothesize with the reason, up to 3 drafts; then **abstain** records the top candidates and
what was missing. Tool results become typed evidence items in code, so every cited ref was copied from a tool.

The model is `INVESTIGATOR_MODEL` (default `gemini-3.8-flash`; `claude-*` models use the Anthropic SDK), temperature 0.

```bash
python -m eval.replay_case --agent investigator --cases all     # every case in eval/runs/, on its snapshot
python -m eval.replay_case --cases case-023 --verbose           # watch one investigation step by step
python -m eval.replay_case --cases case-002 case-007 --run-id try
```

`--verbose` (`agent/trace_view.py`) prints each step as it happens — the node entered, each tool call with the
arguments the agent chose, the typed evidence built from each reply, and the gate's verdict. It reads the state the
graph already streams and never writes back, so a run watched this way reaches the same diagnosis as one that is
not; an observer that raises is swallowed rather than failing the investigation. It forces one case at a time,
since two investigations printing at once interleave. `TRACEWISE_VIEW_WIDTH` overrides the output width (default:
the terminal, capped at 96), which is how the README's screenshots were captured without truncated lines.

After each investigation the evaluator runs the evidence verifier (`eval/verifier.py`), which reads the raw
snapshot files and replay log, never the tools: an invented ref, a quote not in the text, a wrong attribute value
or a failed replay cited as a success marks the diagnosis hallucinated, and it scores as wrong. Then it applies the
diagnosis's `fix_patch` in a fresh replay: fix success means the answer matches and the patch targets the true root
component.

Each run writes `eval/results/<run_id>/`: one JSON per case (diagnosis, gate result, verification, fix replay,
transcript, tokens, cost, wall time), the replay logs in `replays/`, `diagnoses.jsonl`, and `summary.md` with the
investigator, the Day 5 run and B1 side by side. `--rescore --run-id <id>` rebuilds the summary without model calls.

## Tests

```bash
pytest     # needs the stack up and `python -m assistant.bootstrap` done
```

## Trace shape

| Span | Key attributes |
|---|---|
| `POST /chat` (root, FastAPI) | `kestrel.conversation_id`, `kestrel.config.*` |
| `guardrail` | `kestrel.guardrail.decision`, `.matched_rule` |
| `retrieve` | `kestrel.retrieval.top_k`, `.threshold`, `.result_count`, `.chunk_ids`, `.scores`, `kestrel.embedding.model` |
| `tool.<name>` | `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, `kestrel.tool.result_count`, `.latency_ms`, `.cache_age_s` (inventory) |
| `llm.chat` (one per model turn) | `gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.request.temperature`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `kestrel.prompt.version` |

Chunk IDs use the format `doc_id@version#index`, for example `refund-policy@v2#0`.
