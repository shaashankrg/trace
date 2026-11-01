# Results: investigator-day8-train-r1_earlier_all

top-1 = root component named correctly; category = kind of cause named correctly; cause = the exact change or outage named (n/a when the method does not name one).

### Overall

| | n | top-1 | category | cause |
|---|---|---|---|---|
| all | 17 | 47% | 47% | 47% |

### With vs. without a decoy

| | n | top-1 | category | cause |
|---|---|---|---|---|
| with decoy | 8 | 38% | 38% | 38% |
| without decoy | 9 | 56% | 56% | 56% |

### Per fault type

| | n | top-1 | category | cause |
|---|---|---|---|---|
| chunking_regression | 2 | 0% | 0% | 0% |
| missing_document | 3 | 100% | 100% | 100% |
| prompt_regression | 2 | 100% | 100% | 100% |
| silent_tool_failure | 3 | 0% | 0% | 0% |
| stale_document | 2 | 50% | 50% | 50% |
| stale_tool_data | 3 | 0% | 0% | 0% |
| threshold_too_strict | 2 | 100% | 100% | 100% |

### Misses

| case | fault | truth | predicted |
|---|---|---|---|
| case-004 | stale_document | knowledge_base | model |
| case-005 | stale_tool_data | tool:check_inventory | tool:check_inventory |
| case-008 | silent_tool_failure | tool:lookup_order | config |
| case-012 | stale_tool_data | tool:check_inventory | tool:check_inventory |
| case-013 | silent_tool_failure | tool:lookup_order | config |
| case-016 | silent_tool_failure | tool:lookup_order | config |
| case-020 | stale_tool_data | tool:check_inventory | tool:check_inventory |
| case-035 | chunking_regression | retriever | None |
| case-043 | chunking_regression | retriever | None |
