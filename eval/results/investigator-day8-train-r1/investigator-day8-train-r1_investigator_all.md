# Results: investigator-day8-train-r1_investigator_all

top-1 = root component named correctly; category = kind of cause named correctly; cause = the exact change or outage named (n/a when the method does not name one).

### Overall

| | n | top-1 | category | cause |
|---|---|---|---|---|
| all | 17 | 100% | 88% | 59% |

### With vs. without a decoy

| | n | top-1 | category | cause |
|---|---|---|---|---|
| with decoy | 8 | 100% | 88% | 50% |
| without decoy | 9 | 100% | 89% | 67% |

### Per fault type

| | n | top-1 | category | cause |
|---|---|---|---|---|
| chunking_regression | 2 | 100% | 0% | 100% |
| missing_document | 3 | 100% | 100% | 100% |
| prompt_regression | 2 | 100% | 100% | 100% |
| silent_tool_failure | 3 | 100% | 100% | 0% |
| stale_document | 2 | 100% | 100% | 50% |
| stale_tool_data | 3 | 100% | 100% | 0% |
| threshold_too_strict | 2 | 100% | 100% | 100% |

### Misses

| case | fault | truth | predicted |
|---|---|---|---|
