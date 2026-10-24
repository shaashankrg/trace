# investigator-day5-v2: investigator vs. B1

Investigator model `gemini-3.8-flash`, 20 cases, total cost $0.8086 ($0.0404 per case), mean wall time 29s. B1 makes no model calls.

| method | cases | n | top-1 | category | cause | top-1 with decoy | top-1 without decoy | cost |
|---|---|---|---|---|---|---|---|---|
| investigator | valid | 12 | 67% | 67% | 58% | 67% | 67% | $0.8086 |
| B1 | valid | 12 | 0% | 0% | 0% | 0% | 0% | $0 |
| investigator | all recorded | 20 | 75% | 75% | 50% | 80% | 70% | $0.8086 |
| B1 | all recorded | 20 | 0% | 0% | 0% | 0% | 0% | $0 |

valid = the fault made the answer wrong when it was recorded; all recorded adds the cases recorded with `run_case --force` (fault applied, answer not shown to be wrong).

## Per case

| case | fault | valid | decoy | truth | investigator | B1 | tools | cost | refs not in pool |
|---|---|---|---|---|---|---|---|---|---|
| case-001 | prompt_regression | forced |  | prompt / prompt_edit | prompt / prompt_edit | model / model_change | 5 | $0.0166 | 0 |
| case-002 | threshold_too_strict | yes |  | retriever / config_change | retriever / config_change | model / model_change | 15 | $0.0479 | 0 |
| case-003 | stale_document | yes | yes | knowledge_base / doc_update | knowledge_base / doc_update | model / model_change | 10 | $0.0406 | 0 |
| case-004 | stale_document | forced |  | knowledge_base / doc_update | model / model_change | model / model_change | 12 | $0.0516 | 0 |
| case-005 | stale_tool_data | forced | yes | tool:check_inventory / stale_data | tool:check_inventory / stale_data | model / model_change | 8 | $0.0333 | 0 |
| case-006 | missing_document | yes |  | knowledge_base / doc_delete | knowledge_base / doc_delete | model / model_change | 6 | $0.0187 | 0 |
| case-007 | silent_tool_failure | yes |  | tool:lookup_order / tool_outage | config / config_change | model / model_change | 8 | $0.0326 | 0 |
| case-008 | silent_tool_failure | yes | yes | tool:lookup_order / tool_outage | config / config_change | model / model_change | 12 | $0.0710 | 0 |
| case-009 | missing_document | yes | yes | knowledge_base / doc_delete | knowledge_base / doc_delete | model / model_change | 6 | $0.0202 | 0 |
| case-010 | threshold_too_strict | yes |  | retriever / config_change | retriever / config_change | model / model_change | 14 | $0.0719 | 0 |
| case-011 | prompt_regression | forced | yes | prompt / prompt_edit | prompt / prompt_edit | model / model_change | 4 | $0.0146 | 0 |
| case-012 | stale_tool_data | forced | yes | tool:check_inventory / stale_data | tool:check_inventory / stale_data | model / model_change | 9 | $0.0375 | 0 |
| case-013 | silent_tool_failure | yes | yes | tool:lookup_order / tool_outage | config / config_change | model / model_change | 11 | $0.0699 | 0 |
| case-014 | threshold_too_strict | yes | yes | retriever / config_change | retriever / config_change | model / model_change | 13 | $0.0640 | 0 |
| case-015 | missing_document | yes | yes | knowledge_base / doc_delete | knowledge_base / doc_delete | model / model_change | 5 | $0.0144 | 0 |
| case-016 | silent_tool_failure | yes |  | tool:lookup_order / tool_outage | config / config_change | model / model_change | 14 | $0.0684 | 0 |
| case-017 | stale_document | forced | yes | knowledge_base / doc_update | knowledge_base / doc_update | model / model_change | 13 | $0.0615 | 0 |
| case-018 | prompt_regression | yes |  | prompt / prompt_edit | prompt / prompt_edit | model / model_change | 5 | $0.0173 | 0 |
| case-019 | stale_tool_data | forced |  | tool:check_inventory / stale_data | tool:check_inventory / stale_data | model / model_change | 8 | $0.0270 | 0 |
| case-020 | stale_tool_data | forced |  | tool:check_inventory / stale_data | tool:check_inventory / stale_data | model / model_change | 9 | $0.0297 | 0 |
