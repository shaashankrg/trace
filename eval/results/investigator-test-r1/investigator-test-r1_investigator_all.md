# Results: investigator-test-r1_investigator_all

top-1 = root component named correctly; category = kind of cause named correctly; cause = the exact change or outage named (n/a when the method does not name one).

### Headline

| method | n | top-1 | top-3 | category | hallucinated evidence | fix success (replay) | decoy resistance | abstained | $/case | s/case |
|---|---|---|---|---|---|---|---|---|---|---|
| investigator-test-r1_investigator_all | 28 | 50% | 96% | 46% | 0% | 86% | 64% | 50% | $0.0779 | 64.3 |

### Overall

| | n | top-1 | category | cause |
|---|---|---|---|---|
| all | 28 | 50% | 46% | 18% |

### With vs. without a decoy

| | n | top-1 | category | cause |
|---|---|---|---|---|
| with decoy | 13 | 38% | 38% | 15% |
| without decoy | 15 | 60% | 53% | 20% |

### Per fault type

| | n | top-1 | category | cause |
|---|---|---|---|---|
| chunking_regression | 1 | 0% | 0% | 0% |
| embedding_mismatch | 3 | 33% | 0% | 33% |
| guardrail_misfire | 5 | 100% | 100% | 60% |
| missing_document | 1 | 100% | 100% | 100% |
| prompt_regression | 1 | 0% | 0% | 0% |
| silent_tool_failure | 6 | 100% | 100% | 0% |
| stale_tool_data | 6 | 17% | 17% | 0% |
| threshold_too_strict | 5 | 0% | 0% | 0% |

### Misses

| case | fault | truth | predicted |
|---|---|---|---|
| case-022 | prompt_regression | prompt | None |
| case-026 | stale_tool_data | tool:check_inventory | None |
| case-027 | threshold_too_strict | retriever | None |
| case-028 | embedding_mismatch | retriever | None |
| case-029 | threshold_too_strict | retriever | None |
| case-033 | stale_tool_data | tool:check_inventory | None |
| case-034 | stale_tool_data | tool:check_inventory | None |
| case-036 | threshold_too_strict | retriever | None |
| case-037 | stale_tool_data | tool:check_inventory | None |
| case-042 | embedding_mismatch | retriever | None |
| case-044 | threshold_too_strict | retriever | None |
| case-045 | threshold_too_strict | retriever | None |
| case-050 | stale_tool_data | tool:check_inventory | None |
| case-051 | chunking_regression | retriever | None |
