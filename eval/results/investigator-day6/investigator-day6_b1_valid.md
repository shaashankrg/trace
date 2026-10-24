# Results: investigator-day6_b1_valid

top-1 = root component named correctly; category = kind of cause named correctly; cause = the exact change or outage named (n/a when the method does not name one).

### Overall

| | n | top-1 | category | cause |
|---|---|---|---|---|
| all | 12 | 0% | 0% | 0% |

### With vs. without a decoy

| | n | top-1 | category | cause |
|---|---|---|---|---|
| with decoy | 6 | 0% | 0% | 0% |
| without decoy | 6 | 0% | 0% | 0% |

### Per fault type

| | n | top-1 | category | cause |
|---|---|---|---|---|
| missing_document | 3 | 0% | 0% | 0% |
| prompt_regression | 1 | 0% | 0% | 0% |
| silent_tool_failure | 4 | 0% | 0% | 0% |
| stale_document | 1 | 0% | 0% | 0% |
| threshold_too_strict | 3 | 0% | 0% | 0% |

### Misses

| case | fault | truth | predicted |
|---|---|---|---|
| case-002 | threshold_too_strict | retriever | model |
| case-003 | stale_document | knowledge_base | model |
| case-006 | missing_document | knowledge_base | model |
| case-007 | silent_tool_failure | tool:lookup_order | model |
| case-008 | silent_tool_failure | tool:lookup_order | model |
| case-009 | missing_document | knowledge_base | model |
| case-010 | threshold_too_strict | retriever | model |
| case-013 | silent_tool_failure | tool:lookup_order | model |
| case-014 | threshold_too_strict | retriever | model |
| case-015 | missing_document | knowledge_base | model |
| case-016 | silent_tool_failure | tool:lookup_order | model |
| case-018 | prompt_regression | prompt | model |
