# investigator-day8-train-r1: investigator vs. investigator-day5-v2 vs. B1

Investigator model `gemini-3.8-flash`, 17 cases, total cost $0.9236 ($0.0543 per case, replays included), mean wall time 52s. B1 makes no model calls.

Top-1, category and cause count an abstention as no answer and a hallucinated diagnosis (any citation the verifier rejects) as wrong.

| method | cases | n | top-1 | category | cause | precision when answered | abstain rate | abstention value | hallucinated evidence | fix success | judge fallback | cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| investigator | valid | 13 | 100% | 85% | 54% | 100% | 0% | n/a | 0% | 92% | 0% | $0.9236 |
| investigator-day5-v2 | valid | 13 | 38% | 38% | 38% | 45% | 0% | n/a | 27% | 0% | n/a | $0.8086 |
| B1 | valid | 13 | 0% | 0% | 0% | 0% | 0% | n/a | 0% | 0% | n/a | $0.0000 |
| investigator | all recorded | 17 | 100% | 88% | 59% | 100% | 0% | n/a | 0% | 76% | 0% | $0.9236 |
| investigator-day5-v2 | all recorded | 17 | 47% | 47% | 47% | 53% | 0% | n/a | 20% | 0% | n/a | $0.8086 |
| B1 | all recorded | 17 | 0% | 0% | 0% | 0% | 0% | n/a | 0% | 0% | n/a | $0.0000 |

valid = the fault made the answer wrong when it was recorded; all recorded adds the cases recorded with `run_case --force` (fault applied, answer not shown to be wrong).
precision when answered = correct / non-abstained. abstention value = of the abstained cases, how many had a wrong top candidate. hallucinated evidence = diagnoses with a citation the verifier rejects / non-abstained diagnoses. fix success = fresh replays of the fix_patch that match the correction with a patch targeting the true root component / non-abstained diagnoses. judge fallback = replays checked by the LLM judge / all replays.

## Per case

| case | fault | valid | decoy | truth | investigator | gate | verifier | fix replay | tools | replays | cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| case-001 | prompt_regression | forced |  | prompt / prompt_edit | prompt / prompt_edit | passed | ok | not fixed via prompt | 12 | 1 | $0.0529 |
| case-002 | threshold_too_strict | yes |  | retriever / config_change | retriever / config_change | passed | ok | fixed via retriever | 15 | 1 | $0.0710 |
| case-004 | stale_document | forced |  | knowledge_base / doc_update | knowledge_base / doc_update | passed | ok | none | 15 | 1 | $0.0839 |
| case-005 | stale_tool_data | yes | yes | tool:check_inventory / stale_data | tool:check_inventory / stale_data | passed | ok | fixed via tool:check_inventory | 7 | 1 | $0.0273 |
| case-006 | missing_document | yes |  | knowledge_base / doc_delete | knowledge_base / doc_delete | passed | ok | fixed via knowledge_base | 4 | 1 | $0.0167 |
| case-008 | silent_tool_failure | yes | yes | tool:lookup_order / tool_outage | tool:lookup_order / tool_outage | passed | ok | fixed via tool:lookup_order | 12 | 1 | $0.0943 |
| case-009 | missing_document | yes | yes | knowledge_base / doc_delete | knowledge_base / doc_delete | passed | ok | fixed via knowledge_base | 4 | 1 | $0.0173 |
| case-010 | threshold_too_strict | yes |  | retriever / config_change | retriever / config_change | passed | ok | fixed via retriever | 13 | 1 | $0.0854 |
| case-012 | stale_tool_data | yes | yes | tool:check_inventory / stale_data | tool:check_inventory / stale_data | passed | ok | fixed via tool:check_inventory | 8 | 1 | $0.0337 |
| case-013 | silent_tool_failure | yes | yes | tool:lookup_order / tool_outage | tool:lookup_order / tool_outage | passed | ok | fixed via tool:lookup_order | 12 | 1 | $0.0882 |
| case-015 | missing_document | yes | yes | knowledge_base / doc_delete | knowledge_base / doc_delete | passed | ok | fixed via knowledge_base | 6 | 1 | $0.0233 |
| case-016 | silent_tool_failure | yes |  | tool:lookup_order / tool_outage | tool:lookup_order / tool_outage | passed | ok | fixed via tool:lookup_order | 11 | 1 | $0.0517 |
| case-017 | stale_document | forced | yes | knowledge_base / doc_update | knowledge_base / doc_update | passed | ok | fixed via knowledge_base (control also right) | 14 | 3 | $0.0748 |
| case-018 | prompt_regression | forced |  | prompt / prompt_edit | prompt / prompt_edit | passed | ok | not fixed via prompt | 11 | 1 | $0.0559 |
| case-020 | stale_tool_data | yes |  | tool:check_inventory / stale_data | tool:check_inventory / stale_data | passed | ok | fixed via tool:check_inventory | 6 | 1 | $0.0235 |
| case-035 | chunking_regression | yes | yes | retriever / retrieval_failure | retriever / config_change | passed | ok | not fixed via retriever | 15 | 1 | $0.0663 |
| case-043 | chunking_regression | yes |  | retriever / retrieval_failure | retriever / config_change | passed | ok | fixed via retriever | 15 | 1 | $0.0575 |
