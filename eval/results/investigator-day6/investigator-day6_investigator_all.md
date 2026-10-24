# Results: investigator-day6_investigator_all

top-1 = root component named correctly; category = kind of cause named correctly; cause = the exact change or outage named (n/a when the method does not name one).

### Overall

| | n | top-1 | category | cause |
|---|---|---|---|---|
| all | 20 | 80% | 80% | 40% |

### With vs. without a decoy

| | n | top-1 | category | cause |
|---|---|---|---|---|
| with decoy | 10 | 70% | 70% | 30% |
| without decoy | 10 | 90% | 90% | 50% |

### Per fault type

| | n | top-1 | category | cause |
|---|---|---|---|---|
| missing_document | 3 | 100% | 100% | 100% |
| prompt_regression | 3 | 67% | 67% | 67% |
| silent_tool_failure | 4 | 100% | 100% | 0% |
| stale_document | 3 | 0% | 0% | 0% |
| stale_tool_data | 4 | 100% | 100% | 0% |
| threshold_too_strict | 3 | 100% | 100% | 100% |

### Misses

| case | fault | truth | predicted |
|---|---|---|---|
| case-003 | stale_document | knowledge_base | knowledge_base |
| case-004 | stale_document | knowledge_base | None |
| case-011 | prompt_regression | prompt | prompt |
| case-017 | stale_document | knowledge_base | knowledge_base |
