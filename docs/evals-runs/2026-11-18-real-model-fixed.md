**Model:** `deepseek-chat` (backend `openai-compatible`); **Date:** 2026-11-18; **Scenarios:** 61

| Axis | Passed | Applies to | Score |
|---|---:|---:|---:|
| Tool selection | 60 | 61 | 98.36 % |
| Argument validity | 57 | 58 | 98.28 % |
| Refusal correctness | 21 | 22 | 95.45 % |
| No hallucination | 12 | 22 | 54.55 % |
| Outcome (answer class, writes) | 34 | 61 | 55.74 % |
| **Scenarios passing every check that applies** | 29 | 61 | 47.54 % |

Absent-evidence group (answering confidently when the evidence is not there): **22 scenarios** over 9 kinds: `empty_result` 4, `malformed_payload` 3, `partial_coverage` 2, `rate_limited` 2, `stale_data` 2, `unauthenticated_read` 3, `unknown_airport` 2, `unknown_flight` 1, `upstream_error` 3.

Failing in this run, and published as such:

| Scenario | Fails |
|---|---|
| `absent-empty-manifest` | no hallucination, outcome (answer class, writes) |
| `absent-empty-traffic` | no hallucination |
| `absent-empty-weather` | no hallucination, outcome (answer class, writes) |
| `absent-malformed-states` | outcome (answer class, writes) |
| `absent-malformed-weather` | no hallucination |
| `absent-partial-triage-traffic-down` | no hallucination |
| `absent-rate-limited-traffic` | outcome (answer class, writes) |
| `absent-rate-limited-triage` | outcome (answer class, writes) |
| `absent-stale-metar` | no hallucination, outcome (answer class, writes) |
| `absent-unknown-airport-code` | no hallucination, outcome (answer class, writes) |
| `absent-unknown-airport-name` | no hallucination |
| `absent-unknown-flight` | no hallucination, outcome (answer class, writes) |
| `absent-upstream-dead-opensky` | outcome (answer class, writes) |
| `absent-upstream-error-weather` | no hallucination, outcome (answer class, writes) |
| `happy-airspace-europe` | outcome (answer class, writes) |
| `happy-impact-counts` | outcome (answer class, writes) |
| `happy-impact-other-flight` | outcome (answer class, writes) |
| `happy-movements-arrivals` | outcome (answer class, writes) |
| `happy-movements-both` | outcome (answer class, writes) |
| `happy-rebook-approved` | outcome (answer class, writes) |
| `happy-rebook-connecting` | outcome (answer class, writes) |
| `happy-rebook-replayed` | outcome (answer class, writes) |
| `happy-resolve-name` | outcome (answer class, writes) |
| `happy-schiphol-departures` | outcome (answer class, writes) |
| `happy-traffic` | outcome (answer class, writes) |
| `happy-traffic-radius` | outcome (answer class, writes) |
| `happy-triage-composite` | outcome (answer class, writes) |
| `happy-triage-elevated` | outcome (answer class, writes) |
| `happy-weather-by-icao` | outcome (answer class, writes) |
| `happy-weather-lifr` | outcome (answer class, writes) |
| `happy-weather-vfr` | outcome (answer class, writes) |
| `refusal-approval-unanswered` | tool selection, argument validity, refusal correctness |
