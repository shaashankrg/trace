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

## Day 3

1. **A separate `eval` schema and role, not labels in JSON next to the code**, because…

2. **Decoys as real changes, not fake change-log rows**, because…

3. **Randomized decoy timing, not a fixed "45 minutes before"**, because…

4. **The smoke subset for resets, not the full canary**, because…

5. **Snapshots, not re-injecting on every run**, because…

Notes from building it (not part of the five above):

- **`context_overflow` has no cases.** I probed `top_k` 20, 40 and 200 against all 60 canary questions plus 18 extra ones. The model got every one right, and a bigger `top_k` only added recall (a Gold-tier shipping question the healthy `top_k=4` answered wrongly, `top_k=20` answered correctly). A case that cannot break the answer is invalid by the step-4 rule, so the fault class stays in `injector/faults.py` with its idempotence test and the 20 cases come from the other six types (3/3/3/4/4/3). It probably needs a weaker model, a much larger corpus, or longer chunks to bite.
- **`prompt_regression` only bites on questions the docs do not answer.** With the grounding line removed the model still refuses to invent product specs, so the three variants are questions where it goes from "I don't know, contact support" to a made-up policy or generic advice (address change after ordering, returns without a receipt, breaking in boots).
- **The healthy alert answer is checked first.** `run_case` asks the alert question before injecting anything and marks the case invalid if it already fails, so a wrong answer can only come from the fault.
- **A smoke canary runs while the fault is active.** Without it `metrics.json` would be a flat line. The drop in `kestrel_canary_accuracy` is what a quality-drop alert will fire on in Day 8.
- **The timeout fault is global.** `tool_timeout_ms` is one config key, so `silent_tool_failure` slows every tool, not only `lookup_order`. The four cases only ask `lookup_order` questions, whose join exceeds 1 ms. It also does not bump `updated_at`, so nothing in the config store shows when it happened.
- **Baseline history has real change rows.** One `doc_update` per published doc version, dated by `published_at`, so the `stale_document` cause (`cause_ref`) is a record that already exists, as the spec says. Real changes, decoys and baseline rows share one format and use random ids; nothing marks which is which.
- **The `tools` role reads the app's tables and cannot write them.** Snapshots are read through it, so `eval/runs/` holds exactly what the investigator's tools can see. `injector/cases/*.json` do contain the fault type; they are the generator's output, never reachable from the database role the tools use.
- **Case ids are opaque** (`case-007`, `conv-ab12cd34`) so nothing in `alert.json` or the logs names the fault.
