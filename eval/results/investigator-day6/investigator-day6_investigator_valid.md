# Results: investigator-day6_investigator_valid

top-1 = root component named correctly; category = kind of cause named correctly; cause = the exact change or outage named (n/a when the method does not name one).

### Overall

| | n | top-1 | category | cause |
|---|---|---|---|---|
| all | 12 | 92% | 92% | 58% |

### With vs. without a decoy

| | n | top-1 | category | cause |
|---|---|---|---|---|
| with decoy | 6 | 83% | 83% | 50% |
| without decoy | 6 | 100% | 100% | 67% |

### Per fault type

| | n | top-1 | category | cause |
|---|---|---|---|---|
| missing_document | 3 | 100% | 100% | 100% |
| prompt_regression | 1 | 100% | 100% | 100% |
| silent_tool_failure | 4 | 100% | 100% | 0% |
| stale_document | 1 | 0% | 0% | 0% |
| threshold_too_strict | 3 | 100% | 100% | 100% |

### Misses

| case | fault | truth | predicted |
|---|---|---|---|
| case-003 | stale_document | knowledge_base | knowledge_base |
