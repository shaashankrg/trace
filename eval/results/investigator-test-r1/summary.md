# investigator-test-r1: investigator vs. investigator-day5-v2 vs. B1

Investigator model `gemini-3.8-flash`, 28 cases, total cost $2.1814 ($0.0779 per case, replays included), mean wall time 64s. B1 makes no model calls.

Top-1, category and cause count an abstention as no answer and a hallucinated diagnosis (any citation the verifier rejects) as wrong.

| method | cases | n | top-1 | category | cause | precision when answered | abstain rate | abstention value | hallucinated evidence | fix success | judge fallback | cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| investigator | valid | 23 | 52% | 52% | 13% | 100% | 48% | 27% | 0% | 83% | 0% | $2.1814 |
| investigator-day5-v2 | valid | 23 | 0% | 0% | 0% | n/a | 0% | n/a | n/a | n/a | n/a | $0.0000 |
| B1 | valid | 23 | 0% | 0% | 0% | 0% | 0% | n/a | 0% | 0% | n/a | $0.0000 |
| investigator | all recorded | 28 | 50% | 46% | 18% | 100% | 50% | 21% | 0% | 86% | 3% | $2.1814 |
| investigator-day5-v2 | all recorded | 28 | 0% | 0% | 0% | n/a | 0% | n/a | n/a | n/a | n/a | $0.0000 |
| B1 | all recorded | 28 | 0% | 0% | 0% | 0% | 0% | n/a | 0% | 0% | n/a | $0.0000 |

valid = the fault made the answer wrong when it was recorded; all recorded adds the cases recorded with `run_case --force` (fault applied, answer not shown to be wrong).
precision when answered = correct / non-abstained. abstention value = of the abstained cases, how many had a wrong top candidate. hallucinated evidence = diagnoses with a citation the verifier rejects / non-abstained diagnoses. fix success = fresh replays of the fix_patch that match the correction with a patch targeting the true root component / non-abstained diagnoses. judge fallback = replays checked by the LLM judge / all replays.

## Per case

| case | fault | valid | decoy | truth | investigator | gate | verifier | fix replay | tools | replays | cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| case-021 | guardrail_misfire | yes |  | guardrail / guardrail_error | guardrail / guardrail_error | passed | ok | fixed via guardrail | 12 | 1 | $0.0728 |
| case-022 | prompt_regression | yes |  | prompt / prompt_edit | abstained (top: model) | failed x1 | ok | none | 15 | 0 | $0.1159 |
| case-023 | silent_tool_failure | yes | yes | tool:lookup_order / tool_outage | tool:lookup_order / tool_outage | passed | ok | fixed via tool:lookup_order | 14 | 1 | $0.1095 |
| case-024 | embedding_mismatch | forced |  | retriever / retrieval_failure | retriever / config_change | passed | ok | fixed via retriever (control also right) | 15 | 1 | $0.0766 |
| case-025 | guardrail_misfire | yes |  | guardrail / guardrail_error | guardrail / guardrail_error | passed | ok | none | 11 | 1 | $0.0399 |
| case-026 | stale_tool_data | forced |  | tool:check_inventory / stale_data | abstained (top: tool:check_inventory) | failed x1 | ok | none | 15 | 2 | $0.0717 |
| case-027 | threshold_too_strict | yes | yes | retriever / config_change | abstained (top: retriever) | failed x1 | ok | none | 15 | 1 | $0.0857 |
| case-028 | embedding_mismatch | yes | yes | retriever / retrieval_failure | abstained (top: retriever) | failed x1 | ok | none | 15 | 1 | $0.0919 |
| case-029 | threshold_too_strict | yes | yes | retriever / config_change | abstained (top: retriever) | failed x1 | ok | none | 15 | 0 | $0.0821 |
| case-030 | missing_document | yes |  | knowledge_base / doc_delete | knowledge_base / doc_delete | passed | ok | fixed via knowledge_base | 4 | 1 | $0.0163 |
| case-031 | silent_tool_failure | yes |  | tool:lookup_order / tool_outage | tool:lookup_order / tool_outage | passed | ok | fixed via tool:lookup_order | 14 | 1 | $0.0936 |
| case-033 | stale_tool_data | yes |  | tool:check_inventory / stale_data | abstained (top: tool:check_inventory) | failed x1 | ok | none | 15 | 2 | $0.0849 |
| case-034 | stale_tool_data | yes | yes | tool:check_inventory / stale_data | abstained (top: tool:check_inventory) | failed x2 | ok | none | 15 | 2 | $0.1026 |
| case-036 | threshold_too_strict | forced | yes | retriever / config_change | abstained (top: retriever) | failed x1 | ok | none | 15 | 1 | $0.0722 |
| case-037 | stale_tool_data | yes |  | tool:check_inventory / stale_data | abstained (top: tool:check_inventory) | failed x1 | ok | none | 15 | 2 | $0.0646 |
| case-038 | guardrail_misfire | forced | yes | guardrail / guardrail_error | guardrail / guardrail_error | passed | ok | fixed via guardrail | 10 | 1 | $0.0513 |
| case-040 | guardrail_misfire | yes | yes | guardrail / guardrail_error | guardrail / guardrail_error | passed | ok | fixed via guardrail | 8 | 1 | $0.0325 |
| case-041 | stale_tool_data | yes |  | tool:check_inventory / stale_data | tool:check_inventory / stale_data | passed | ok | fixed via tool:check_inventory | 8 | 1 | $0.0324 |
| case-042 | embedding_mismatch | yes |  | retriever / retrieval_failure | abstained (top: config) | failed x1 | ok | none | 15 | 0 | $0.0607 |
| case-044 | threshold_too_strict | yes |  | retriever / config_change | abstained (top: retriever) | failed x1 | ok | none | 15 | 0 | $0.0627 |
| case-045 | threshold_too_strict | forced | yes | retriever / config_change | abstained (top: retriever) | failed x1 | ok | none | 15 | 0 | $0.1071 |
| case-046 | guardrail_misfire | yes | yes | guardrail / guardrail_error | guardrail / guardrail_error | passed | ok | none | 14 | 0 | $0.1009 |
| case-047 | silent_tool_failure | yes | yes | tool:lookup_order / tool_outage | tool:lookup_order / tool_outage | passed | ok | fixed via tool:lookup_order | 15 | 1 | $0.0740 |
| case-048 | silent_tool_failure | yes |  | tool:lookup_order / tool_outage | tool:lookup_order / tool_outage | passed | ok | fixed via tool:lookup_order | 11 | 1 | $0.0839 |
| case-049 | silent_tool_failure | yes |  | tool:lookup_order / tool_outage | tool:lookup_order / tool_outage | passed | ok | fixed via tool:lookup_order | 14 | 1 | $0.1150 |
| case-050 | stale_tool_data | yes | yes | tool:check_inventory / stale_data | abstained (top: tool:check_inventory) | failed x1 | ok | none | 15 | 2 | $0.0742 |
| case-051 | chunking_regression | yes | yes | retriever / retrieval_failure | abstained (top: config) | failed x2 | ok | none | 15 | 1 | $0.1047 |
| case-052 | silent_tool_failure | yes |  | tool:lookup_order / tool_outage | tool:lookup_order / tool_outage | passed | ok | fixed via tool:lookup_order | 14 | 1 | $0.1016 |
