# Results: investigator-day8-train-r1_b1_all

top-1 = root component named correctly; category = kind of cause named correctly; cause = the exact change or outage named (n/a when the method does not name one).

### Overall

| | n | top-1 | category | cause |
|---|---|---|---|---|
| all | 17 | 0% | 0% | 0% |

### With vs. without a decoy

| | n | top-1 | category | cause |
|---|---|---|---|---|
| with decoy | 8 | 0% | 0% | 0% |
| without decoy | 9 | 0% | 0% | 0% |

### Per fault type

| | n | top-1 | category | cause |
|---|---|---|---|---|
| chunking_regression | 2 | 0% | 0% | 0% |
| missing_document | 3 | 0% | 0% | 0% |
| prompt_regression | 2 | 0% | 0% | 0% |
| silent_tool_failure | 3 | 0% | 0% | 0% |
| stale_document | 2 | 0% | 0% | 0% |
| stale_tool_data | 3 | 0% | 0% | 0% |
| threshold_too_strict | 2 | 0% | 0% | 0% |

### Misses

| case | fault | truth | predicted |
|---|---|---|---|
| case-001 | prompt_regression | prompt | model |
| case-002 | threshold_too_strict | retriever | model |
| case-004 | stale_document | knowledge_base | model |
| case-005 | stale_tool_data | tool:check_inventory | model |
| case-006 | missing_document | knowledge_base | model |
| case-008 | silent_tool_failure | tool:lookup_order | model |
| case-009 | missing_document | knowledge_base | model |
| case-010 | threshold_too_strict | retriever | model |
| case-012 | stale_tool_data | tool:check_inventory | model |
| case-013 | silent_tool_failure | tool:lookup_order | model |
| case-015 | missing_document | knowledge_base | model |
| case-016 | silent_tool_failure | tool:lookup_order | model |
| case-017 | stale_document | knowledge_base | model |
| case-018 | prompt_regression | prompt | model |
| case-020 | stale_tool_data | tool:check_inventory | model |
| case-035 | chunking_regression | retriever | model |
| case-043 | chunking_regression | retriever | model |
