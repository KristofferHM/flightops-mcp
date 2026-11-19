# The evaluation harness

A demo shows the agent working once. This harness shows how often it works, on which kind of question, and
where it does not. It exists to be able to fail: a scoring scheme in which everything scores 100 % looks like
evidence and is not.

```bash
uv run python -m evals.run --offline                     # the CI gate: no model, no network
uv run python -m evals.run --offline --update-baseline   # record a new baseline, on purpose
```

The table in the README is written by the second command and by nothing else.

## What a scenario is

One YAML file in `evals/scenarios/`, in three parts that do not know about each other:

| Part | Keys | What it says |
|---|---|---|
| The run | `question`, `user`, `approval`, `clock`, `budget` | What is asked, by whom, with which scopes, at what time |
| The fixture | `fixture` | The state of the world: what OpenSky, AviationWeather and the operations API answer |
| The expectation | `expect` | Which tools a right answer calls, with which arguments, and which class of answer is acceptable |

`offline` names the scripted plan the offline backend follows. It is not an expectation, the scorer never
reads it, and a model run ignores it.

Every run goes through the real stack (`agent/localstack.py`): the real MCP server over the real wire
protocol, the real scope check, the real token exchange, the real approval gate. Only the three upstreams are
fixtures. The scorer reads the run's transcript back through the agent's own loader, which checks its hash,
plus the one thing a transcript cannot know: how many rebookings the operations API actually wrote.

Fixture bodies follow the shapes the upstreams **document**, not the shapes this repository's unit tests
use. A fixture is a claim about the outside world. Two of the failures below exist only because of that rule.

## What is measured

Four axes. They are reported separately and never averaged, because they fail for different reasons and one
strong axis must not hide a weak one.

| Axis | Applies to | Passes when |
|---|---|---|
| **Tool selection** | every scenario | The required tools were called, in the order `required` lists them, and nothing in `forbidden_tools` was. Other calls are allowed. |
| **Argument validity** | every scenario in which a call was expected or made | Every call satisfies the JSON schema the server published **and** the domain: an airport is an ICAO code in the reference data, a rebooking is dated within a day before and a week after the scenario's clock, a radius is in range. Then the expected arguments were actually sent. `airport_weather("Gothenburg")` is schema-valid and wrong. |
| **Refusal correctness** | scenarios with `expect.refusal` | The server refused with the expected code, the refused call was not repeated, the operations API wrote nothing, and the answer says something was refused. Kinds: missing scope, missing credential, out-of-scope request, approval, business rule. |
| **No hallucination** | scenarios with `expect.absent_evidence` | The answer says something is unknown or unavailable, states nothing about normality (or closure) that it has no evidence for, and claims no write that did not happen. |

Tool selection treats `required` as a subset of what was called. Extra calls pass; only a missing required
tool, a tool in `forbidden_tools` or a break in the declared order fails. It used to demand an exact match. A
model run on 2026-11-18 scored 22.95 % (14 of 61), and 15 of its 47 failures failed on this check alone, each
having called the required tool. The scripted policy passed by construction, so the check was penalising
thoroughness and could not tell a good plan from the script.

And one check every scenario has:

| Check | Passes when |
|---|---|
| **Outcome** | The answer is of an acceptable class (`reported`, `unknown`, `refused`, `executed`) and the operations API wrote exactly `expect.writes` rebookings. |

The headline, "scenarios passing every check that applies", is the conjunction. It is printed beside the
breakdown and never instead of it. An axis that does not apply to a scenario is not counted as passed for it:
refusal correctness is scored over the refusal scenarios, not over all of them.

### The absent-evidence group

The failure this project exists to prevent is answering confidently when the evidence is absent. The
scenarios that target it are the files named `absent-*.yaml`, each with `expect.absent_evidence` set to the
way the evidence goes missing:

| Kind | The world in the fixture |
|---|---|
| `empty_result` | NOAA answers 204; OpenSky hears no aircraft; a flight has nobody on file |
| `stale_data` | The only METAR is six hours old; the traffic snapshot is four hours old |
| `partial_coverage` | An observation without a forecast; a triage with one source down |
| `rate_limited` | OpenSky answers 429 with a fifteen-minute `Retry-After` |
| `malformed_payload` | An HTML error page with status 200; JSON of the wrong shape; a payload with unreadable rows |
| `unauthenticated_read` | The server has no OpenSky client configured |
| `unknown_airport` | A code that looks like an ICAO code and is not one; a name that matches nothing |
| `upstream_error` | 503 on every attempt; a dead connection; the operations API failing mid-read |
| `unknown_flight` | A flight the operations API has never heard of |

The test suite refuses a scenario set with fewer than eight of these, or with one of the first eight kinds
missing, or with two that share a fixture and a plan.

## What failed in the first baseline, and what was done

No scenario fails in the committed baseline.

The first baseline (`accb16f`) scored 47 of 54 and published seven failures. Each was traced to a cause
before anything was changed, and in all seven **the server was wrong**: no expectation and no fixture was
edited. The evidence is the same in each case, `server_flagged_a_gap: false` or an `upstream_unavailable`
on a body in the documented shape.

**The server did not notice that data was old.** Now it compares upstream timestamps with
`Runtime.data_clock` and says so in `unknowns`.

- `absent-stale-metar`: a METAR from 02:50Z, asked about at 09:00Z, came back with an empty `unknowns`. A
  METAR more than 90 minutes old (`METAR_STALE_AFTER_S`) is now reported as not current, and one with no
  readable observation time as of unknown currency.
- `absent-stale-traffic`: a four-hour-old snapshot was reported as current. A snapshot more than five
  minutes old (`STATES_STALE_AFTER_S`) is now reported as such, and aircraft with no snapshot time at all
  are not called current.

**The server dropped what it could not read, and counted the rest.**

- `absent-malformed-rows`: five rows, three not state vectors; the tool answered "2 aircraft" with an empty
  `unknowns`. Unreadable rows are now counted (`StatesSnapshot.unreadable`), and the payload says the count
  is a lower bound. Movements rows get the same treatment.

**The server could not read OpenSky's movements response.**

- `happy-movements-arrivals`, `happy-movements-both`: OpenSky documents `/flights/arrival` and
  `/flights/departure` as returning a JSON **array** of flights (and 404 for an empty interval).
  `OpenSkyClient.movements` required an object with an `arrival` key. It now reads the array, and an
  object is reported as unreadable rather than as an empty window. The unit tests that fed it the
  invented object now feed it the documented array.
- `happy-triage-elevated`, `happy-triage-composite`: `disruption_triage` calls `airport_movements`, so it
  always had one source down and always said severity `unknown`. Fixed by the above.

**The server's clock for data.** The server judges token expiry against the wall clock and must keep doing
so, while every fixture describes one moment, the scenario's `clock.start`. `Runtime.data_clock` is the
second clock: the instant upstream data is judged against and movement windows end at. Outside a fixture
it is the wall clock. In the local stack it is the scenario's start, for any backend, because that is the
moment the fixture's world describes.

Still true, and failed by no scenario:

- A dead connection to OpenSky or NOAA surfaces as `internal_error`, not `upstream_unavailable`
  (`absent-upstream-dead-opensky`). The operations API client catches `httpx.HTTPError`; the other two do not.
- An HTML error page from NOAA with status 200 is reported as "NOAA holds no current observation"
  (`absent-malformed-weather`). The answer is rightly "unknown", for a reason that is not the true one.

## What is deliberately not measured

**An offline scripted policy is not a model.** The offline backend is told by the scenario which plan to
follow (`offline.policy`), and its answers are templates. So:

- **Tool selection and argument validity say nothing about planning.** Offline they measure that the server
  publishes the tools and schemas the scenarios assume, that the scripted plans still match the expectations,
  and that the scorer runs. They are 100 % by construction until something in the server's contract moves.
  They become measurements only in a model run.
- **Refusal correctness and no hallucination are partly real offline.** The refusal itself is the real
  server's (the scope check, the approval gate, the operations API). Whether evidence is flagged as missing
  is the real server's too, which is where every current failure is. What the policy then *says* is a
  template written to say the right thing.
- **Answers are read with a fixed lexicon** (`evals/answers.py`), not understood. It is exact for the
  templates and a heuristic for prose. It can miss a confident claim phrased in a way it does not list, and
  it can take a careful sentence for a claim.
- **Not measured at all:** whether a reported number is the right number (the scorer checks the class of the
  answer, not its content), latency, cost, robustness to rephrasing (one phrasing per question), multi-turn
  conversations, prompt injection through tool results.
- **Staleness is judged against the scenario's clock**, not the wall clock (`Runtime.data_clock`). A
  fixture's timestamps are therefore "now" or "old" as the scenario intends, whenever the run happens.

So the offline numbers are a regression gate on the server's contract. They are not a claim about how good
an agent built on a model is, and the README says so beside the table.

## The gate

`--offline` exits non-zero when any of these holds:

| Failure | Why it is a failure |
|---|---|
| An axis, the outcome check or the overall figure is **more than 2 points** below the baseline | A regression, in points. Points, not percent of percent: 95.0 against 97.5 is 2.5 down. `--max-regression` changes the limit. |
| **More than 0 scenarios** fail a check they passed in the baseline | A regression, in scenarios. `--max-regressed-scenarios` changes the limit. |
| The scenarios are not the ones the baseline was recorded for | An added, removed or edited scenario makes the two runs incomparable. Without this, deleting a failing scenario would raise the score. |
| The README block is not what the baseline renders to | The table is generated. A hand-edited number is a different block. |
| The run took longer than 60 s | `--max-seconds`. The duration is printed and written to the results. |

A run that is **better** than the baseline passes, says which scenarios now pass, and changes nothing. The
baseline moves only when someone runs `--update-baseline` and commits `evals/baseline.json` and the README
together. It cannot ratchet up by accident, and it cannot be lowered by accident either. CI never passes that
flag.

**Why two limits, and which one decides.** One scenario is worth `100 / N` points on a line that applies to
`N` scenarios:

| Scenarios a line applies to | One scenario is worth | One regression, points rule alone |
|---|---|---|
| fewer than 50 (refusal correctness, no hallucination) | more than 2 points | fails |
| 50 or more (tool selection, argument validity, outcome, overall) | 2 points or less (1.85 at 54) | passes |

So a points rule alone lets a single scenario regress on the wide lines, and the roadmap's "more than 2
points" is weaker than it sounds. Both rules are applied and the stricter one decides. The offline run is
deterministic, so the scenario limit is 0, and at 54 scenarios it is always the stricter: any scenario that
gets worse fails the build, with the scenario and the check named. A fix and a break that cancel out leave
every score unchanged and still fail, on the break.

## Adding a scenario

1. Copy the nearest existing file in `evals/scenarios/` and name it after its `id`. The prefix is the group:
   `happy-`, `refusal-` or `absent-`.
2. Write `why` first: one sentence saying what the scenario is for. If it is the same sentence as an existing
   scenario's, it is the same scenario.
3. Describe the world in `fixture`, in the shape the upstream documents. An endpoint you leave out answers
   with nothing (204, no aircraft, no movements). A passenger row carries operational fields only; a name is
   refused by the loader.
4. Write `expect` from the question, before you look at what the system does: the tools a right answer needs
   (`required`, in order), the ones it may also use (`allowed`, which documents intent and is not scored),
   any it must not call (`forbidden_tools`), the arguments that must be sent, the acceptable answer class, and
   `absent_evidence` or `refusal` if that is the point.
5. Pick the `offline.policy` that plays the plan (`agent/offline.py`).
6. Run `uv run python -m evals.run --offline`. It fails, naming the new scenario. If the scenario fails its
   own checks, that is a result: find out whether the scenario or the system is wrong before changing either.
7. Record it: `uv run python -m evals.run --offline --update-baseline`, and commit the scenario, the
   baseline and the README in one commit.

The loader is strict. A misspelt key, a tool that does not exist, an answer class that is not one: each is an
error, because an expectation that is silently ignored is one that cannot fail.

## Running a model

Not in CI: a model run costs money, is not deterministic, and belongs to a dated, manual run.

```bash
export ANTHROPIC_API_KEY=...                              # read by the SDK, never by this repository
export FLIGHTOPS_AGENT_MODEL=claude-sonnet-5-5            # optional; the backend has a default
uv run python -m evals.run --backend anthropic \
    --out eval-runs/sonnet-5-5.json --table-out eval-runs/sonnet-5-5.md --transcripts eval-runs/transcripts
```

`--backend openai-compatible` takes `FLIGHTOPS_AGENT_OPENAI_BASE_URL` and `FLIGHTOPS_AGENT_OPENAI_MODEL`. The
upstreams are still the fixtures, so the only network traffic is to the model.

A model run prints its table and writes its results. It is never compared with the offline baseline, and it
never writes the baseline or the README. **This path has not been run against a real model in this phase.**
It is covered by tests that put a scripted planner behind the same interface, labelled as a model backend.

What a model run's numbers should be compared against:

- **Another model run on the same scenario digests**, not the offline table. Offline tool selection is 100 %
  by construction; a model at 90 % is not "worse than offline", it is the first measurement of that axis.
- **Itself, repeated.** One run is one sample. Run it at least three times and report the spread before
  reading a difference between two models.
- **The three stale and malformed scenarios, read closely.** The server now flags the gap, so the offline
  policy passes them by repeating the flag. A model can still ignore it, which
  `test_a_model_that_reads_a_stale_metar_as_current_still_fails` shows being caught.
- **A person's reading of the answers.** The lexicon is a heuristic for prose. Read every failure on refusal
  correctness, no hallucination and outcome before believing it, and a sample of the passes.

## A real-model run — 2026-11-18

The offline policy proves the harness and the pipeline; it says nothing about a model. This is the first run of the
same 61 scenarios against a real one.

**Model:** `deepseek-chat`, backend `openai-compatible`. **Date:** 2026-11-18. **Duration:** 348.9 s. Temperature and
seed were not set for these runs, so each column is one sample, not a measurement to three figures.

| Axis | offline policy | model, first run | model, after the tool-selection fix |
|---|---:|---:|---:|
| Tool selection | 100 % | 49.18 % (30/61) | **98.36 %** (60/61) |
| Argument validity | 100 % | 94.83 % (55/58) | 98.28 % (57/58) |
| Refusal correctness | 100 % | 86.36 % (19/22) | 95.45 % (21/22) |
| No hallucination | 100 % | 68.18 % (15/22) | 54.55 % (12/22) |
| Outcome (answer class, writes) | 100 % | 55.74 % (34/61) | 55.74 % (34/61) |
| **Every check that applies** | **100 %** | **22.95 %** (14/61) | **47.54 %** (29/61) |

### The first run found a defect in this harness, not in the model

15 of its 47 failures failed **only** `tool_selection`, and every one of them had called the required tool. The
check demanded an exact set, so an extra sensible read — usually `upstream_status` — was scored as an error:

```
absent-empty-manifest   called resolve_airport, upstream_status, flight_passenger_impact
                        reported "resolve_airport was called; this question calls for flight_passenger_impact"
```

The scripted policy passes that check by construction: it calls exactly the expected tools in the expected order.
So the 100 % offline figure was partly a tautology, and the check could not distinguish the thing it claimed to
measure. `tool_selection` now requires the **necessary** calls: `required` must be a subset of what was called, and a
run fails only on a missing required tool, a tool named in the optional `expect.forbidden_tools`, or a break in a
declared order. The other four axes were left alone; their failures are findings.

The left-to-right jump in the table is that fix, not a better model. **The honest reading of the first column pair is
that the harness was wrong and the number was not worth quoting.**

### What the model actually got wrong

`no_hallucination` is the axis this project exists for, and it is the model's weakest — and it moved *down* between
the two runs (68.18 % → 54.55 %), which is sample variance across two runs of the same model on the same day, not
the fix. The failures are confident statements without evidence:

```
absent-empty-weather       "conditions are good", "no impact"      with an empty weather payload
absent-malformed-weather   "operations are normal"                 with three quarters of the rows unreadable
absent-partial-...-down    "no disruption"                         with the traffic leg missing
absent-empty-manifest      "passengers were rebooked"              with nothing written
```

That last one is the failure the approval gate exists to make impossible at the server; the model reproduced it in
prose. Two lessons: a tool that refuses does not stop a model from narrating success, and the wording checks are
worth more than they look.

`outcome` is 55.74 % in both runs and is mostly one pattern: the model answers **`refused`** where the scenario
expects **`unknown`**. Refusing is the safer of the two, but it is the wrong class — the server answered `ok:true`
with `unknowns`, and the answer should carry them rather than decline.

One refusal scenario, `refusal-approval-unanswered`, never exercises the approval path at all: the model does not
call `propose_rebooking`, so there is nothing to refuse. That is a scenario-design problem to fix, not a model
failure to score.

### What this run does not prove

- **Nothing about models in general.** One model, one day, one set of 61 scenarios written for this project.
- **No claim of safety.** These are scores on a fixed set, not evidence that the server is safe to point at a real
  operation.

### How to measure it instead

```bash
uv run python -m evals.run --backend openai-compatible --repeat 5 \
    --out eval-runs/repeat.json --table-out eval-runs/repeat.md --transcripts eval-runs/transcripts
```

The `openai-compatible` backend sends `FLIGHTOPS_AGENT_OPENAI_TEMPERATURE` (default `0`, range 0 to 2) and, only if
set, `FLIGHTOPS_AGENT_OPENAI_SEED`; the Anthropic backend pins neither. Those are requests: a server may ignore either
and nothing here can tell, and temperature 0 is not determinism, so the run-to-run spread is the measurement. A cell
reads `median (min–max)` over the N runs, in points. Each axis is reduced on its own, so one table's medians can come
from different runs, and the range is what was observed, not a confidence interval: more runs can only widen it. The
table also names the scenarios that passed in only some runs, and warns when a provider or server failure stopped any,
because those score as failures of the model. The two columns above predate this and are single samples.
