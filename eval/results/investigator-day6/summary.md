# investigator-day6: investigator vs. investigator-day5-v2 vs. B1

Investigator model `gemini-3.8-flash`, 20 cases, total cost $1.0697 ($0.0535 per case, replays included), mean wall time 69s. B1 makes no model calls.

Top-1, category and cause count an abstention as no answer and a hallucinated diagnosis (any citation the verifier rejects) as wrong.

| method | cases | n | top-1 | category | cause | precision when answered | abstain rate | abstention value | hallucinated evidence | fix success | judge fallback | cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| investigator | valid | 12 | 92% | 92% | 58% | 92% | 0% | n/a | 8% | 92% | 0% | $1.0697 |
| investigator-day5-v2 | valid | 12 | 67% | 67% | 58% | 67% | 0% | n/a | 0% | 0% | n/a | $0.8086 |
| B1 | valid | 12 | 0% | 0% | 0% | 0% | 0% | n/a | 0% | 0% | n/a | $0.0000 |
| investigator | all recorded | 20 | 80% | 80% | 40% | 84% | 5% | 100% | 16% | 89% | 0% | $1.0697 |
| investigator-day5-v2 | all recorded | 20 | 75% | 75% | 50% | 75% | 0% | n/a | 0% | 0% | n/a | $0.8086 |
| B1 | all recorded | 20 | 0% | 0% | 0% | 0% | 0% | n/a | 0% | 0% | n/a | $0.0000 |

valid = the fault made the answer wrong when it was recorded; all recorded adds the cases recorded with `run_case --force` (fault applied, answer not shown to be wrong).
precision when answered = correct / non-abstained. abstention value = of the abstained cases, how many had a wrong top candidate. hallucinated evidence = diagnoses with a citation the verifier rejects / non-abstained diagnoses. fix success = fresh replays of the fix_patch that match the correction with a patch targeting the true root component / non-abstained diagnoses. judge fallback = replays checked by the LLM judge / all replays.

## Per case

| case | fault | valid | decoy | truth | investigator | gate | verifier | fix replay | tools | replays | cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| case-001 | prompt_regression | forced |  | prompt / prompt_edit | prompt / prompt_edit | passed | ok | not fixed via prompt | 13 | 1 | $0.0553 |
| case-002 | threshold_too_strict | yes |  | retriever / config_change | retriever / config_change | passed | ok | fixed via retriever | 13 | 1 | $0.0668 |
| case-003 | stale_document | yes | yes | knowledge_base / doc_update | knowledge_base / doc_update | passed | span:80cbd5775e51e7db340d3dfb1511f295/94138b49249b8d89: span retrieve has no attribute chunk_ids; doc:warranty@v1: quote | fixed via knowledge_base (control also right) | 15 | 1 | $0.0637 |
| case-004 | stale_document | forced |  | knowledge_base / doc_update | abstained (top: model) | failed x1 | ok | none | 15 | 2 | $0.0720 |
| case-005 | stale_tool_data | forced | yes | tool:check_inventory / stale_data | tool:check_inventory / stale_data | passed | ok | fixed via tool:check_inventory | 8 | 1 | $0.0301 |
| case-006 | missing_document | yes |  | knowledge_base / doc_delete | knowledge_base / doc_delete | passed | ok | fixed via knowledge_base | 4 | 1 | $0.0155 |
| case-007 | silent_tool_failure | yes |  | tool:lookup_order / tool_outage | tool:lookup_order / tool_outage | passed | ok | fixed via tool:lookup_order | 14 | 1 | $0.1056 |
| case-008 | silent_tool_failure | yes | yes | tool:lookup_order / tool_outage | tool:lookup_order / tool_outage | passed | ok | fixed via tool:lookup_order | 11 | 1 | $0.0641 |
| case-009 | missing_document | yes | yes | knowledge_base / doc_delete | knowledge_base / doc_delete | passed | ok | fixed via knowledge_base | 4 | 1 | $0.0163 |
| case-010 | threshold_too_strict | yes |  | retriever / config_change | retriever / config_change | passed | ok | patch rejected: similarity_threshold=0.5 is not a value the  | 15 | 0 | $0.0696 |
| case-011 | prompt_regression | forced | yes | prompt / prompt_edit | prompt / prompt_edit | passed | prompt:v2: quote not in the prompt text | fixed via prompt | 14 | 1 | $0.0714 |
| case-012 | stale_tool_data | forced | yes | tool:check_inventory / stale_data | tool:check_inventory / stale_data | passed | ok | fixed via tool:check_inventory | 8 | 1 | $0.0293 |
| case-013 | silent_tool_failure | yes | yes | tool:lookup_order / tool_outage | tool:lookup_order / tool_outage | passed | ok | fixed via tool:lookup_order | 11 | 1 | $0.0619 |
| case-014 | threshold_too_strict | yes | yes | retriever / config_change | retriever / config_change | passed | ok | fixed via retriever | 13 | 1 | $0.0878 |
| case-015 | missing_document | yes | yes | knowledge_base / doc_delete | knowledge_base / doc_delete | passed | ok | fixed via knowledge_base | 6 | 1 | $0.0234 |
| case-016 | silent_tool_failure | yes |  | tool:lookup_order / tool_outage | tool:lookup_order / tool_outage | passed | ok | fixed via tool:lookup_order | 15 | 1 | $0.0864 |
| case-017 | stale_document | forced | yes | knowledge_base / doc_update | knowledge_base / doc_update | passed | span:25be7fd577de9a67c81dbb56d7131416/925e25e874efdc4f: span retrieve has no attribute chunk_ids; doc:shipping-expedited | fixed via knowledge_base (control also right) | 14 | 2 | $0.0716 |
| case-018 | prompt_regression | yes |  | prompt / prompt_edit | prompt / prompt_edit | passed | ok | fixed via prompt | 5 | 1 | $0.0196 |
| case-019 | stale_tool_data | forced |  | tool:check_inventory / stale_data | tool:check_inventory / stale_data | passed | ok | fixed via tool:check_inventory | 7 | 1 | $0.0249 |
| case-020 | stale_tool_data | forced |  | tool:check_inventory / stale_data | tool:check_inventory / stale_data | passed | ok | fixed via tool:check_inventory | 9 | 1 | $0.0344 |
