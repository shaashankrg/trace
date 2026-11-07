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

## Day 4

1. **A backend interface over querying stores directly inside tools**, because…

2. **Typed refs like `chunk:doc@v#n` over raw database IDs**, because…

3. **Summaries with a `total` field over full dumps**, because…

4. **`document_history` without a "current" flag**, because…

5. **Errors returned as data over raised exceptions**, because…

Notes from building it (not part of the five above):

- **`mcp` is pinned below 2.** mcp 2.x renamed `FastMCP` to `MCPServer`; the 1.x API is what the day's plan uses.
- **The backend interface grew three methods**: `trace_id_for(conversation_id)` (tools take a conversation, Jaeger stores traces), `log(log_id)` and `metric_names()` (so `log:` refs resolve and a bad metric name gets a useful hint). `logs()` returns `(rows, total)` so `search_logs` can report `total_matches`.
- **A seventh ref type, `doc:<doc_id>@<version>`.** `document_history` items needed a ref, and none of the six named a document version.
- **Refs live under keys named `ref`, `*_ref` or `*_refs`.** That is how the tests find every ref to resolve. A change's `target` (`doc:refund-policy`) is plain text, not a ref.
- **`resolve_ref` returns `{"ref", "kind", "record"}`.** Spreading the record into the result let a change's own `kind` (`config_change`) overwrite the ref kind.
- **Snapshots now include `chunks.json`.** `get_retrieved_chunks` needs chunk text and index flags, which `documents.json` does not have.
- **The doc diff is word-level** (`within [30 -> 14] days`). A line diff hid the changed number inside a long paragraph.
- **Gemini 3.x rejects `thinking_budget=0`** with a bare 400 INVALID_ARGUMENT. It takes `thinking_level="MINIMAL"` instead. This was the 400 from the end of Day 3.
- **With Gemini 3.5 Flash-Lite most cases are invalid.** Only case-002, case-006 and case-007 broke the answer as designed on the first try, and case-003 on its second. The prompt-regression and stale-tool-data cases either fail while healthy or survive the fault, so their fixtures (case-001, case-005) were recorded with the new `run_case --force`, which snapshots anyway and still records the run as invalid. The tool tests only need the fault applied, not the answer wrong. Re-run the evaluation cases on a model they were designed for before scoring anything.
- **The Inspector does not pass your shell environment to the server it starts.** Without `-e TRACEWISE_BACKEND=snapshot:<case>` it silently uses the live backend, which after `run_case` is back to healthy.
- **Manual run-through (case-003, stale warranty doc):** `get_trace` (retrieve span shows `warranty@v1#0` and `warranty@v2#0` both in the top 4) -> `get_retrieved_chunks` (1-year vs 2-year text) -> `document_history("warranty")` (both versions have indexed chunks; v2 is newer) -> `recent_changes` (the v2 publish record, plus the unrelated config-change decoy).


## Day 5

1. **Plain code for triage, not a model turn**, because…

2. **Evidence items built from tool results in code, not written by the model**, because…

3. **Direct SDK calls, not a LangChain chat-model wrapper**, because…

4. **The tool budget enforced in `Toolbox.call_tool`, not only in the prompt**, because…

5. **Pydantic validation with one retry, then abstain, not accepting whatever JSON comes back**, because…

Notes from building it (not part of the five above):

- **First top-1 number: 67% (8 of 12 valid cases), B1 0%.** Run `investigator-day5-v2`, `gemini-3.8-flash`, $0.81 for 20 cases, mean 29s per case. With the 8 forced cases added it is 75% of 20. Decoy cases scored the same as non-decoy ones (67% / 67% on valid). B1 gets 0% because no fault produces an error span, so it always says "model".
- **All four valid misses are `silent_tool_failure`.** The investigator answers `config / config_change` (it finds `tool_timeout_ms=1` on the root span); the label is `tool:lookup_order / tool_outage`. The investigator's reading is defensible, since the fault *is* a config value. I left the label alone: changing ground truth after seeing results is how numbers get inflated. Decide before Day 8 whether this fault should be labelled `config` or whether the vocabulary needs a "which layer" rule, and re-record the cases if the label changes.
- **case-004 (stale document, forced) is the only other miss**, `model / model_change` in both runs.
- **The first full run (`investigator-day5`, 58% valid) had a 49-minute hang on case-014** that ended in a bare `ExceptionGroup`. Blocking SDK calls run in worker threads, which asyncio cannot cancel, so nothing above them could time out. Fixed with an HTTP timeout on both clients (`INVESTIGATOR_HTTP_TIMEOUT_S`, 120s, with two retries on timeout), a per-tool MCP read timeout (60s), a 900s per-case `wait_for`, and `describe()` unwrapping the ExceptionGroup to the real error. The server's stderr now goes to `eval/results/<run>/<case>.server.log`. The rerun had no errors.
- **A hard spend cap across runs**: `.spend.json` (gitignored) holds the running total and `Chat.send` refuses once it reaches `INVESTIGATOR_SPEND_CAP_USD` ($2). The call in flight can overshoot by its own cost.
- **The Day 6 and Day 10 stub tools are hidden from the model.** Offering `replay_conversation` before it works only burns budget on errors.
- **Cited refs not in the evidence pool: 0 in all 20 cases.** That is a property of the design (the model can only copy refs it was shown), not yet a verified hallucination rate; the Day 6 verifier checks refs against the stores.

## Day 6

1. **Overrides over modifying the database for replay**, because…

2. **Code derives a patch's target component instead of the agent stating it**, because…

3. **The gate is code over asking the LLM "are you confident?"**, because…

4. **The verifier reads raw snapshots instead of calling the MCP tools**, because…

5. **Hallucinated evidence scores as wrong even when the component is right**, because…

Notes from building it (not part of the five above):

- **Results (`investigator-day6`, 20 cases, $1.07 including replays).** On the 12 valid cases: top-1 92% (Day 5: 67%, B1: 0%), precision when answered 92%, abstain rate 0%, hallucinated evidence 8% (1 of 12), fix success 92%, judge fallback 0%. On all 20 recorded cases: top-1 80% (Day 5: 75%), precision 84% (Day 5: 75%), abstain rate 5%, abstention value 100%, hallucinated evidence 16% (3 of 19), fix success 89%. Day 5's 0% hallucination is not comparable: its citations had no quotes or attribute values to check, only refs, and all of those existed.
- **The gate raised precision and cost one abstention.** case-004 (stale document, forced) is the miss both Day 5 runs got wrong as `model`; this time the gate rejected it (only one evidence type relevant to `model`) and it abstained with `model` as the top candidate, which was wrong. All four `silent_tool_failure` cases, which Day 5 called `config`, now come out `tool:lookup_order`: the `rerun_tool` replay fixed the answer against a failing control, and the config hypothesis could not be proved because no change record shows `tool_timeout_ms` with another value. The gate is code and the labels did not change; this is the gate doing its job, not the label question from Day 5 going away.
- **All three hallucinated diagnoses are citation-format errors, not invented facts.** Every ref existed and every claim was true, but the agent quoted `document_history`'s summary line ("v1 published …, 1/1 chunks indexed") against the document body, cited the attribute `chunk_ids` instead of `retrieval.chunk_ids`, or quoted the line a prompt diff removed against the version that no longer has it. I kept the verifier strict, because relaxing it after seeing results is how numbers get inflated. Day 7: tell the draft to quote only source text, and re-run.
- **Empty-patch determinism: 19/20 cases, 56/60 samples.** Retrieval and tool calls matched the recording in all 60 samples; only the assistant model's text varies (exact text repeated in 4 of 20 cases). case-003 never reproduced: its wrong answer (mentioning the old 1-year warranty as an aside) is a minority sample, and the case was only valid on its third recording attempt. Inputs were checked identical (config, chunks, order, prompt).
- **That is why a replay has samples and a control.** One run proves little when the model itself is noisy. Each replay takes 3 samples and the majority counts, and each patched replay is compared with an unpatched control: `proves_fix` only when the patch fixes it and the control does not. Otherwise a flaky case like case-003 would "fix" itself under any patch, which is failure scenario F5 by another route. A replay costs about $0.0003 (gemini-3.5-flash-lite).
- **Snapshots gained `store.json` and `answer_check`** (`eval.backfill_snapshots`, and `run_case` records both from now on). Re-running a tool needs the store as it was during the fault. The store seed is deterministic and only `stale_tool_data` touches it, so the backfill rebuilds it in one rolled-back transaction (reset, apply decoy and fault with the original timestamps, dump), then checks it against every tool output recorded in `tool_io.json`: all 20 match.
- **`tool_io.json` holds the healthy pre-fault run too.** Replay serves only the calls from the alert's own trace, in recorded order, with keys put back in SQL column order (jsonb had re-ordered them, and the model originally saw the SQL order).
- **`top_k` is a retriever patch.** The plan lists `set_config` as `config` "or retriever for threshold and embedding settings". The schema's component list and the context-overflow ground truth both put `top_k` under the retriever, so it maps there. `chunk_size` cannot be patched, because chunks are already cut.
- **`pipeline.answer` is shared by the app and replay.** `pipeline.run` now reads config from the database and hands retrieval, the prompt and the tool executor to `answer`; replay passes snapshot-backed ones. One chat loop, so a replay cannot drift from the real assistant.
- **The MCP server preloads the assistant when `TRACEWISE_REPLAY=1`** (about 30 s). On Windows, loading a native library (numpy, torch) while another thread is blocked reading the stdin pipe waits for that read, so a lazy import inside the server hung the first replay for the full 300 s timeout. A server without the flag answers replay with an error instead of hanging.
- **A malformed `fix_patch` is dropped, not fatal.** In the first smoke run the draft wrote `{"kind": "set_config", "tool_timeout_ms": 5000}`, failed validation twice, and a correct diagnosis was thrown away. It now scores as "no fix" and keeps `fix_patch_error`.
- **The anti-cheating rule caught real attempts.** The agent tried `tool_timeout_ms=5000` (case-007, smoke run) and `similarity_threshold=0.5` (case-010): values that appear nowhere in the history. Both were rejected in code; case-010's fix therefore counts as failed.
- **`answer_check` is now in `alert.json`.** The Day 4 anti-cheat test banned it from snapshot files; it is allowed in `alert.json` only, and no tool output shows it. The investigator never reads it either; only the replay's answer check does.
- **Judge fallback is 0%.** The LLM judge runs only when an answer has every required pattern and a forbidden one (it states both facts). No replay in this run needed it, so the judge's own accuracy is untested on real answers.

## Day 8

1. **A same-dimension embedding model for the mismatch fault**, because…

2. **A separate dev split for the Day 3 cases instead of putting them in test**, because…

3. **Held-out failure types (`embedding_mismatch`, `guardrail_misfire`) only in test**, because…

4. **The 2-of-3 replay rule for quality drops**, because…

5. **Logging every test-split run**, because…

Notes from building it (not part of the five above):

- **Two of the four new fault types produce no cases, and the honest reason is different for each.**
  `model_downgrade`: every smaller model available on this key is unusable as a *silent*
  downgrade. `gemini-2.5-flash` and `gemini-2.5-flash-lite` return 404 ("no longer available to
  new users"). The Gemma models (`gemma-4-26b-a4b-it`, `gemma-4-31b-it`) raise, because
  `assistant.llm.is_gemini` routes any name that is not `gemini-*` to the Anthropic SDK — a loud
  failure, and a routing artifact rather than a model-quality signal. `gemini-3.1-flash-lite` is
  callable and silent but too capable: right 3/3 on all 13 two-hop questions (order date vs.
  policy window) and on 7 of 8 three-hop ones. The single question it failed 3/3 in one sweep
  passed 3/3 in the next, so that was sampling noise at the edge of its ability, not a fault.
  A fault that only shows up as flakiness cannot be scored, because the replay rule needs a patch
  to flip the answer repeatably. Same call as `context_overflow` on Day 3: the fault class and its
  apply/revert tests stay, the cases do not. Probe: `eval/probes/probe_model_downgrade.py`.

- **`chunking_regression` needed 40-60 chars, not the 80 I started with.** At 80 the model
  simply reassembled the answer out of several small chunks: of 12 policy questions only
  `price-matching` lost its answer. A sweep over 40/60/80/120/1000/2000
  (`eval/probes/probe_chunking_sizes.py`) shows 40 and 60 are where the key sentence fragments
  badly enough that no retrieved chunk carries the whole fact, and where the answer actually
  breaks. 1000 and 2000 broke nothing: a bigger chunk still contains the sentence. So the
  intensity is per (doc, size), measured, not guessed.

- **The embedding mismatch is silent, and that took choosing the model on evidence.**
  `all-MiniLM-L12-v2` and `multi-qa-MiniLM-L6-cos-v1` are both 384-dim, so neither raises a
  dimension error. But `multi-qa` broke 0 of 24 questions: it ranks this corpus about as well as
  the indexed model. `all-MiniLM-L12-v2` drops the answer chunk on 3, so that is the one the
  cases use. A 768-dim model would have thrown on the first query, which is the loud failure the
  plan warns about and useless as a test.

- **Chunk refs carry their chunking: `chunk:refund-policy@v2#3/c80`.** Both chunkings of a
  re-chunked doc live in the `chunks` table at once and only the index flag moves, so a replay
  can switch between them in memory without re-chunking anything. That is what made
  `set_config chunk_size` replayable, which Day 6 had refused with "documents are already
  chunked". `_switch_chunking` only touches doc/version pairs that actually have both chunkings.

- **`guardrail_misfire` needs the blocked topic to appear as a whole word in the question.** The
  guardrail matches on `\b`-delimited topics, so blocking "return" does nothing to "returning".
  The cases block `refund`, `shipping`, `warranty`, `exchange`, `deposit`, each paired with a
  question that uses that exact word. Their `must_not_contain` carries the refusal text, so a
  blocked answer fails the check even where the answer pattern is loose.

- **Three new categories were needed, or the new ground truth would not have been scoreable.**
  `retrieval_failure` and `guardrail_error` are not in the Day 5 vocabulary, so
  `agent/schema.py` would have rejected the labels for `chunking_regression`,
  `embedding_mismatch` and `guardrail_misfire`. Added both to `CATEGORIES`; the components
  (`retriever`, `guardrail`) already existed.

- **The investigator prompt changed for the new alert shape, not for accuracy.** It said "a
  reviewer flagged one of its answers as wrong", which is false for a `quality_drop` alert. It
  now describes both alert types and states the 2-of-3 rule. This is before the freeze and is not
  tuning: it tells the agent what the input is, which no train miss was consulted for.

- **`alert_text` hides `answer_check` from the failing samples.** The samples go to the model so
  it can see the shape of the drop, but their grading patterns do not: that would hand it the
  answer key. The Day 4 anti-cheat rule keeps `answer_check` to `alert.json` alone.

- **A `run_case` shadowing bug surfaced while wiring quality drops.** Inside the injection loop
  `record = item.change_record()` overwrote the function's own `record` flag, so the later
  `if record:` was always truthy and `record_run()` was handed a dict where a bool belonged. The
  change record is now `change`.

- **`wait_for_scrape` had to learn which canary suite it is waiting for.** It only ever queried
  `suite="smoke"`, but a quality-drop case runs the full canary, which pushes under
  `suite="full"`, so the first quality-drop recording timed out waiting for a scrape that was
  never going to arrive.

- **The benchmark as recorded: 52 cases, 45 with a valid recording.** train 13/17, dev 4/5,
  test 28/30. Test covers 8 fault types and holds all 10 quality-drop alerts and **every** case of
  both held-out types (`guardrail_misfire` 5, `embedding_mismatch` 3). The four train cases and one
  dev case without a valid recording are the Day 3 `--force` cases, which were already recorded
  that way before Day 8; the scorer reports "valid" and "all recorded" separately, as it did on
  Day 6. The two test cases left out (`case-032`, `case-039`, both `chunking_regression`) never
  produced a wrong answer and are excluded from the lock rather than padding the split.

- **The lock reads the snapshots, not `eval.case_runs`.** The first version used the latest row
  per case, and it disagreed with the recordings: six cases had an earlier run that recorded a
  genuinely wrong answer and a later run that logged "the fault did not make the answer wrong".
  Both are true — the assistant model is not deterministic — but the snapshot is what a replay
  actually evaluates, so validity is now derived by scoring each snapshot's recorded answer
  against that case's own `answer_check`. That moved the valid test count from 23 to 28.

- **`guard_test_run` ignored a caller-supplied lock path.** It called `check_lock()` with no
  argument, so it always read `eval/test_split.lock` regardless of what it was handed. Caught by
  a test that passed a temporary lock and watched the wrong error come back.

- **Test split locked at 28 cases, hash `3d497defc7e3b08f`, 0 test runs.** `eval/test_split.lock`
  pins the ids and a hash over each case's JSON including its `split`, so both a case leaving the
  split and a case being quietly reworded break the lock (both have tests).

- **Section 5 (tuning on train) did not run: both model providers are out of budget.** Gemini
  returns `429 RESOURCE_EXHAUSTED` ("project has exceeded its monthly spending cap") and Anthropic
  returns `400` ("credit balance is too low"), so no investigator run is possible and the prompt is
  deliberately **not** frozen. `eval/frozen_prompt.sha` is absent, and `--split test` refuses to
  run without it, which is the behaviour we want: the test split stays untouched rather than being
  spent on an untuned prompt. To resume: top up either provider, run
  `python -m eval.replay_case --split train`, read the misses, add general guidance only, re-run
  train, then `python -m eval.splits --freeze-prompt` before any test run.

- **The investigator prompt changed only to describe the new alert shape.** No train miss has been
  read, so no accuracy tuning has happened. That edit is listed above and is the only prompt change
  on Day 8.

## Day 9

- **Freeze the prompt *and* the configuration, over freezing the prompt file alone, because a
  step budget is as much a tuning knob as a sentence of instructions.** The prompt-only freeze had
  a loophole worth naming out loud: after freezing, you could lower the escalation confidence,
  raise the tool budget or swap in a different model, and the prompt file's hash would still match,
  so a test run would be allowed even though it described a different agent. `freeze_hash()` now
  covers the prompt plus `CONFIG_KEYS` (model, cheap model, routing, escalation threshold, tool and
  replay budgets, max tokens, thinking level, tool output cap), and `guard_test_run` names which
  setting moved rather than just reporting a hash mismatch — "the hash differs" sends you hunting
  through a prompt that may be untouched when an env var is what changed.

- **Keep `case-032` and `case-039` out of the lock, over padding the split to 30.** Both are
  `chunking_regression` cases whose fault never made an answer wrong, so they were never recorded
  and `valid_recorded_ids()` excludes them; the lock lists them under `excluded_not_valid`. A case
  that cannot be made to fail is not a test case. **Locked count stays 28.**

- **Baseline B2 gets the chunks retrieval actually returned, over every chunk in the store.**
  Handing it the whole store would be a different and easier problem: it could diff v1 against v2
  directly instead of reasoning about what retrieval served. It also gets the transcript and the
  change list with decoys unlabelled, the same vocabulary as the investigator from
  `agent/schema.py`, and the same model — so the only thing removed is the investigation itself.

- **An unparseable B2 reply scores as a wrong answer, over being skipped.** Dropping it would take
  the case out of B2's denominator, and a baseline that crashes on the hard cases would then
  outscore one that attempted them and got them wrong. The same rule makes every method score over
  the same case set in `compare_methods`.

- **B2 is reported even though it scores well (43% vs. 52%), because a baseline chosen to lose
  proves nothing.** The narrow gap is the finding: the model alone solves the cases whose cause is
  visible in the transcript and change list, and the tools earn their place on the rest — plus on
  being able to say *which* answers are supported, which B2 structurally cannot.

- **Top-3 counts a hallucinated diagnosis as wrong at every k, over scoring the ranked list
  independently.** Otherwise top-3 would quietly launder exactly what the evidence verifier exists
  to catch.

- **Cost and wall time average only over methods that have them, over treating a missing value as
  zero.** B1 calls no model and has no wall time; averaging it as $0.00 would make it look fast and
  free next to the investigator, which is a statement about the comparison, not a fact about B1.

- **The test split was run once, and the 48% abstention rate is reported rather than tuned away.**
  Every abstention hit the 15-tool-call ceiling holding one evidence type, and on 8 of 11 the top
  candidate was already the right component, so the obvious fix is a larger budget. That experiment
  is deliberately not run: the split has been opened once, and tuning against what it showed would
  turn the held-out number into a dev number. The next budget change belongs on train, with a
  re-freeze before any second test run — which `test_runs.log` would record.

- **`gt_tmp.json` was gitignored after being found untracked in the repo root.** It holds raw
  ground truth, which the anti-cheating rule says lives only in the evaluator. Nothing read it, but
  a ground-truth file sitting next to the agent's code is the kind of thing that eventually does.

- **Supersedes the last Day 8 note:** the budget block is resolved, the prompt and config are
  frozen (`eval/frozen_prompt.sha`), and both the train and the held-out test runs have been made.
