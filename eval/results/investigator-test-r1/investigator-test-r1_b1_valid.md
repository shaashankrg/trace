# Results: investigator-test-r1_b1_valid

top-1 = root component named correctly; category = kind of cause named correctly; cause = the exact change or outage named (n/a when the method does not name one).

### Headline

| method | n | top-1 | top-3 | category | hallucinated evidence | fix success (replay) | decoy resistance | abstained | $/case | s/case |
|---|---|---|---|---|---|---|---|---|---|---|
| investigator-test-r1_b1_valid | 23 | 0% | 0% | 0% | 0% | 0% | n/a | 0% | n/a | n/a |

### Overall

| | n | top-1 | category | cause |
|---|---|---|---|---|
| all | 23 | 0% | 0% | 0% |

### With vs. without a decoy

| | n | top-1 | category | cause |
|---|---|---|---|---|
| with decoy | 10 | 0% | 0% | 0% |
| without decoy | 13 | 0% | 0% | 0% |

### Per fault type

| | n | top-1 | category | cause |
|---|---|---|---|---|
| chunking_regression | 1 | 0% | 0% | 0% |
| embedding_mismatch | 2 | 0% | 0% | 0% |
| guardrail_misfire | 4 | 0% | 0% | 0% |
| missing_document | 1 | 0% | 0% | 0% |
| prompt_regression | 1 | 0% | 0% | 0% |
| silent_tool_failure | 6 | 0% | 0% | 0% |
| stale_tool_data | 5 | 0% | 0% | 0% |
| threshold_too_strict | 3 | 0% | 0% | 0% |

### Misses

| case | fault | truth | predicted |
|---|---|---|---|
| case-021 | guardrail_misfire | guardrail | model |
| case-022 | prompt_regression | prompt | model |
| case-023 | silent_tool_failure | tool:lookup_order | model |
| case-025 | guardrail_misfire | guardrail | model |
| case-027 | threshold_too_strict | retriever | model |
| case-028 | embedding_mismatch | retriever | model |
| case-029 | threshold_too_strict | retriever | model |
| case-030 | missing_document | knowledge_base | model |
| case-031 | silent_tool_failure | tool:lookup_order | model |
| case-033 | stale_tool_data | tool:check_inventory | model |
| case-034 | stale_tool_data | tool:check_inventory | model |
| case-037 | stale_tool_data | tool:check_inventory | model |
| case-040 | guardrail_misfire | guardrail | model |
| case-041 | stale_tool_data | tool:check_inventory | model |
| case-042 | embedding_mismatch | retriever | model |
| case-044 | threshold_too_strict | retriever | model |
| case-046 | guardrail_misfire | guardrail | model |
| case-047 | silent_tool_failure | tool:lookup_order | model |
| case-048 | silent_tool_failure | tool:lookup_order | model |
| case-049 | silent_tool_failure | tool:lookup_order | model |
| case-050 | stale_tool_data | tool:check_inventory | model |
| case-051 | chunking_regression | retriever | model |
| case-052 | silent_tool_failure | tool:lookup_order | model |
