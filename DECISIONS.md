# Decisions

Each one: what we chose, what we didn't, and why. Written in my own words.

## Day 1

1. **OpenTelemetry Collector between app and Jaeger, not exporting straight to Jaeger**, because…

2. **Local sentence-transformers embeddings, not a hosted embedding API**, because…

3. **`doc_id@version#index` chunk IDs, not auto-increment integers**, because…

4. **An `indexed` flag, not deleting rows**, because…

5. **`result_count` as a span attribute, not only logging results**, because…

6. **Default model `claude-sonnet-4-6`.** The spec requires temperature 0. Claude Opus 5.5 rejects `temperature`, and Sonnet 5.5 and Haiku 5.5 reject non-default values, so the default is a model that accepts it. The model is still set by `ANTHROPIC_MODEL`.
