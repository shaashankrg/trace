# Decisions

Each one: what we chose, what we didn't, and why. Written in my own words.

## Day 1

1. **OpenTelemetry Collector between app and Jaeger, not exporting straight to Jaeger**, because…

2. **Local sentence-transformers embeddings, not a hosted embedding API**, because…

3. **`doc_id@version#index` chunk IDs, not auto-increment integers**, because…

4. **An `indexed` flag, not deleting rows**, because…

5. **`result_count` as a span attribute, not only logging results**, because…

6. **Default model `claude-sonnet-4-6`.** The spec requires temperature 0. Claude Opus 5.5 rejects `temperature`, and Sonnet 5.5 and Haiku 5.5 reject non-default values, so the default is a model that accepts it. The model is still set by `ANTHROPIC_MODEL`.

## Day 2

1. **Read config per request, not once at startup**, because…

2. **Pushgateway, not a `/metrics` endpoint, for canary results**, because…

3. **Code-scored canary answers, not an LLM judge**, because…

4. **The silent-empty-result tool wrapper exists on purpose**, because…

5. **`facts.yaml` as a single source of truth**, because…

Notes from building it (not part of the five above):

- **Tool timeouts are swallowed on purpose.** `execute_tool` catches `QueryCanceled`, logs a WARN, returns `[]`, and leaves the span status OK. Verified: with `tool_timeout_ms=1`, "how many orders have I placed?" gets the confident wrong answer "no orders on file". The only signals are `kestrel.tool.result_count = 0`, a latency near the timeout, the `timed_out` flag in `tool_calls`, and the WARN log.
- **Yes/no answers end with `Answer: Yes|No`.** The first canary run scored 96.7% because the model sometimes wrote "No..." and then corrected itself to yes mid-answer. A fixed final line is what the code scorer checks.
- **Docs are rendered from templates.** `assistant/doc_templates/` holds `{{fact}}` placeholders and `eval/render_docs.py` fills them from `facts.yaml`, failing on an unknown fact. The app never reads `facts.yaml`.
- **Pushgateway has host port 9091, so Prometheus moved to 9092** (`PROMETHEUS_PORT`).
