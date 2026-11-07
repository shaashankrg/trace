# Results: investigator-test-r1_earlier_all

top-1 = root component named correctly; category = kind of cause named correctly; cause = the exact change or outage named (n/a when the method does not name one).

### Headline

| method | n | top-1 | top-3 | category | hallucinated evidence | fix success (replay) | decoy resistance | abstained | $/case | s/case |
|---|---|---|---|---|---|---|---|---|---|---|
| investigator-test-r1_earlier_all | 28 | 0% | 0% | 0% | n/a | n/a | n/a | 0% | n/a | n/a |

### Overall

| | n | top-1 | category | cause |
|---|---|---|---|---|
| all | 28 | 0% | 0% | 0% |

### With vs. without a decoy

| | n | top-1 | category | cause |
|---|---|---|---|---|
| with decoy | 13 | 0% | 0% | 0% |
| without decoy | 15 | 0% | 0% | 0% |

### Per fault type

| | n | top-1 | category | cause |
|---|---|---|---|---|
| chunking_regression | 1 | 0% | 0% | 0% |
| embedding_mismatch | 3 | 0% | 0% | 0% |
| guardrail_misfire | 5 | 0% | 0% | 0% |
| missing_document | 1 | 0% | 0% | 0% |
| prompt_regression | 1 | 0% | 0% | 0% |
| silent_tool_failure | 6 | 0% | 0% | 0% |
| stale_tool_data | 6 | 0% | 0% | 0% |
| threshold_too_strict | 5 | 0% | 0% | 0% |

### Misses

| case | fault | truth | predicted |
|---|---|---|---|
| case-021 | guardrail_misfire | guardrail | None |
| case-022 | prompt_regression | prompt | None |
| case-023 | silent_tool_failure | tool:lookup_order | None |
| case-024 | embedding_mismatch | retriever | None |
| case-025 | guardrail_misfire | guardrail | None |
| case-026 | stale_tool_data | tool:check_inventory | None |
| case-027 | threshold_too_strict | retriever | None |
| case-028 | embedding_mismatch | retriever | None |
| case-029 | threshold_too_strict | retriever | None |
| case-030 | missing_document | knowledge_base | None |
| case-031 | silent_tool_failure | tool:lookup_order | None |
| case-033 | stale_tool_data | tool:check_inventory | None |
| case-034 | stale_tool_data | tool:check_inventory | None |
| case-036 | threshold_too_strict | retriever | None |
| case-037 | stale_tool_data | tool:check_inventory | None |
| case-038 | guardrail_misfire | guardrail | None |
| case-040 | guardrail_misfire | guardrail | None |
| case-041 | stale_tool_data | tool:check_inventory | None |
| case-042 | embedding_mismatch | retriever | None |
| case-044 | threshold_too_strict | retriever | None |
| case-045 | threshold_too_strict | retriever | None |
| case-046 | guardrail_misfire | guardrail | None |
| case-047 | silent_tool_failure | tool:lookup_order | None |
| case-048 | silent_tool_failure | tool:lookup_order | None |
| case-049 | silent_tool_failure | tool:lookup_order | None |
| case-050 | stale_tool_data | tool:check_inventory | None |
| case-051 | chunking_regression | retriever | None |
| case-052 | silent_tool_failure | tool:lookup_order | None |
