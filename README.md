# Tracewise

The Kestrel support assistant (RAG over policy docs with Claude), instrumented end to end with OpenTelemetry.

```text
POST /chat ──► retrieve (pgvector top-k) ──► llm.chat (Claude)
      │ spans (OTLP gRPC :4317)
      ▼
otel-collector ──► Jaeger (UI :16686)
                                   logs ──► OpenSearch (:9200, kestrel-logs-*)
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
python -m injector.make_cases      # 20 cases -> injector/cases/*.json and eval.cases
python -m eval.run_case case-001   # reset, inject, record, snapshot to eval/runs/case-001/, revert, check recovery
python -m eval.run_case --all
python -m eval.baselines b1        # rule-based baseline over the recorded traces
python -m eval.score eval/diagnoses/b1.jsonl   # -> eval/results/b1.md, b1.csv, b1_cases.csv
```

Fault types are in `injector/faults.py`, decoys in `injector/decoys.py`. Each fault has `apply()`, an idempotent
`revert()`, a `change_record()` (or `None` for an outage) and a `ground_truth()`. Real changes and decoys are
written to the `changes` table in the same format.

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
| `{"kind": "set_config", "key", "value"}` | `retriever` (top_k, threshold, embedding model), `model`, `guardrail`, else `config` |
| `{"kind": "set_prompt_version", "version"}` | `prompt` |
| `{"kind": "rerun_tool", "tool"}`, `{"kind": "bypass_cache", "tool": "check_inventory"}` | `tool:<tool>` |

Versions must exist in the snapshot, and a `set_config` value must appear in the change log or on the trace. The
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
python -m eval.replay_case --cases case-002 case-007 --run-id try
```

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
