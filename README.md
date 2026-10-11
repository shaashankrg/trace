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
