# Results: investigator-day5-v2_investigator_valid

top-1 = root component named correctly; category = kind of cause named correctly; cause = the exact change or outage named (n/a when the method does not name one).

### Overall

| | n | top-1 | category | cause |
|---|---|---|---|---|
| all | 12 | 67% | 67% | 58% |

### With vs. without a decoy

| | n | top-1 | category | cause |
|---|---|---|---|---|
| with decoy | 6 | 67% | 67% | 50% |
| without decoy | 6 | 67% | 67% | 67% |

### Per fault type

| | n | top-1 | category | cause |
|---|---|---|---|---|
| missing_document | 3 | 100% | 100% | 100% |
| prompt_regression | 1 | 100% | 100% | 100% |
| silent_tool_failure | 4 | 0% | 0% | 0% |
| stale_document | 1 | 100% | 100% | 0% |
| threshold_too_strict | 3 | 100% | 100% | 100% |

### Misses

| case | fault | truth | predicted |
|---|---|---|---|
| case-007 | silent_tool_failure | tool:lookup_order | config |
| case-008 | silent_tool_failure | tool:lookup_order | config |
| case-013 | silent_tool_failure | tool:lookup_order | config |
| case-016 | silent_tool_failure | tool:lookup_order | config |
