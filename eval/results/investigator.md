# Results: investigator

top-1 = root component named correctly; category = kind of cause named correctly; cause = the exact change or outage named (n/a when the method does not name one).

### Headline

| method | n | top-1 | top-3 | category | hallucinated evidence | fix success (replay) | decoy resistance | abstained | $/case | s/case |
|---|---|---|---|---|---|---|---|---|---|---|
| investigator | 23 | 52% | 96% | 52% | 0% | 83% | 65% | 48% | $0.0784 | 65.1 |

### Overall

| | n | top-1 | category | cause |
|---|---|---|---|---|
| all | 23 | 52% | 52% | 13% |

### With vs. without a decoy

| | n | top-1 | category | cause |
|---|---|---|---|---|
| with decoy | 10 | 40% | 40% | 10% |
| without decoy | 13 | 62% | 62% | 15% |

### Per fault type

| | n | top-1 | category | cause |
|---|---|---|---|---|
| chunking_regression | 1 | 0% | 0% | 0% |
| embedding_mismatch | 2 | 0% | 0% | 0% |
| guardrail_misfire | 4 | 100% | 100% | 50% |
| missing_document | 1 | 100% | 100% | 100% |
| prompt_regression | 1 | 0% | 0% | 0% |
| silent_tool_failure | 6 | 100% | 100% | 0% |
| stale_tool_data | 5 | 20% | 20% | 0% |
| threshold_too_strict | 3 | 0% | 0% | 0% |

### Misses

| case | fault | truth | predicted |
|---|---|---|---|
| case-022 | prompt_regression | prompt | None |
| case-027 | threshold_too_strict | retriever | None |
| case-028 | embedding_mismatch | retriever | None |
| case-029 | threshold_too_strict | retriever | None |
| case-033 | stale_tool_data | tool:check_inventory | None |
| case-034 | stale_tool_data | tool:check_inventory | None |
| case-037 | stale_tool_data | tool:check_inventory | None |
| case-042 | embedding_mismatch | retriever | None |
| case-044 | threshold_too_strict | retriever | None |
| case-050 | stale_tool_data | tool:check_inventory | None |
| case-051 | chunking_regression | retriever | None |
