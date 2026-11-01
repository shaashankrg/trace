# investigator-day5: investigator vs. B1

Investigator model `gemini-3.8-flash`, 20 cases, total cost $0.6879 ($0.0344 per case), mean wall time 315s. B1 makes no model calls.

| method | cases | n | top-1 | category | cause | top-1 with decoy | top-1 without decoy | cost |
|---|---|---|---|---|---|---|---|---|
| investigator | valid | 12 | 58% | 58% | 50% | 50% | 67% | $0.6879 |
| B1 | valid | 12 | 0% | 0% | 0% | 0% | 0% | $0 |
| investigator | all recorded | 20 | 70% | 70% | 45% | 70% | 70% | $0.6879 |
| B1 | all recorded | 20 | 0% | 0% | 0% | 0% | 0% | $0 |

valid = the fault made the answer wrong when it was recorded; all recorded adds the cases recorded with `run_case --force` (fault applied, answer not shown to be wrong).

## Per case

| case | fault | valid | decoy | truth | investigator | B1 | tools | cost | refs not in pool |
|---|---|---|---|---|---|---|---|---|---|
| case-001 | prompt_regression | forced |  | prompt / prompt_edit | prompt / prompt_edit | model / model_change | 4 | $0.0136 | 0 |
| case-002 | threshold_too_strict | yes |  | retriever / config_change | retriever / config_change | model / model_change | 11 | $0.0445 | 0 |
| case-003 | stale_document | yes | yes | knowledge_base / doc_update | knowledge_base / doc_update | model / model_change | 6 | $0.0210 | 0 |
| case-004 | stale_document | forced |  | knowledge_base / doc_update | model / model_change | model / model_change | 14 | $0.0600 | 0 |
| case-005 | stale_tool_data | forced | yes | tool:check_inventory / stale_data | tool:check_inventory / stale_data | model / model_change | 11 | $0.0476 | 0 |
| case-006 | missing_document | yes |  | knowledge_base / doc_delete | knowledge_base / doc_delete | model / model_change | 6 | $0.0205 | 0 |
| case-007 | silent_tool_failure | yes |  | tool:lookup_order / tool_outage | config / config_change | model / model_change | 10 | $0.0470 | 0 |
| case-008 | silent_tool_failure | yes | yes | tool:lookup_order / tool_outage | config / config_change | model / model_change | 9 | $0.0495 | 0 |
| case-009 | missing_document | yes | yes | knowledge_base / doc_delete | knowledge_base / doc_delete | model / model_change | 6 | $0.0195 | 0 |
| case-010 | threshold_too_strict | yes |  | retriever / config_change | retriever / config_change | model / model_change | 10 | $0.0313 | 0 |
| case-011 | prompt_regression | forced | yes | prompt / prompt_edit | prompt / prompt_edit | model / model_change | 12 | $0.0561 | 0 |
| case-012 | stale_tool_data | forced | yes | tool:check_inventory / stale_data | tool:check_inventory / stale_data | model / model_change | 10 | $0.0334 | 0 |
| case-013 | silent_tool_failure | yes | yes | tool:lookup_order / tool_outage | config / config_change | model / model_change | 13 | $0.0603 | 0 |
| case-014 | threshold_too_strict | yes | yes | retriever / config_change | None / None | model / model_change | 0 | $0.0000 | 0 |
| case-015 | missing_document | yes | yes | knowledge_base / doc_delete | knowledge_base / doc_delete | model / model_change | 5 | $0.0169 | 0 |
| case-016 | silent_tool_failure | yes |  | tool:lookup_order / tool_outage | config / config_change | model / model_change | 10 | $0.0445 | 0 |
| case-017 | stale_document | forced | yes | knowledge_base / doc_update | knowledge_base / doc_update | model / model_change | 10 | $0.0411 | 0 |
| case-018 | prompt_regression | yes |  | prompt / prompt_edit | prompt / prompt_edit | model / model_change | 4 | $0.0141 | 0 |
| case-019 | stale_tool_data | forced |  | tool:check_inventory / stale_data | tool:check_inventory / stale_data | model / model_change | 9 | $0.0310 | 0 |
| case-020 | stale_tool_data | forced |  | tool:check_inventory / stale_data | tool:check_inventory / stale_data | model / model_change | 10 | $0.0362 | 0 |
