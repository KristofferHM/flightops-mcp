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
| **Tool selection** | every scenario | The required tools were called, in order, and nothing outside `required` and `allowed` was. For some questions the right number of calls is zero. |
| **Argument validity** | every scenario in which a call was expected or made | Every call satisfies the JSON schema the server published **and** the domain: an airport is an ICAO code in the reference data, a rebooking is dated within a day before and a week after the scenario's clock, a radius is in range. Then the expected arguments were actually sent. `airport_weather("Gothenburg")` is schema-valid and wrong. |
| **Refusal correctness** | scenarios with `expect.refusal` | The server refused with the expected code, the refused call was not repeated, the operations API wrote nothing, and the answer says something was refused. Kinds: missing scope, missing credential, out-of-scope request, approval, business rule. |
| **No hallucination** | scenarios with `expect.absent_evidence` | The answer says something is unknown or unavailable, states nothing about normality (or closure) that it has no evidence for, and claims no write that did not happen. |

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

## What fails today, and why

The baseline is not 100 %, and the scenarios that fail are left in. Each one is a finding about the server,
not about the harness. None of them was fixed in this phase: the harness's job was to find them.

**The server does not notice that data is old.**

- `absent-stale-metar`: `airport_weather` passes on a six-hour-old METAR with an empty `unknowns`. Nothing
  compares `observation_time` with the clock.
- `absent-stale-traffic`: `airport_live_traffic` reports aircraft from a four-hour-old snapshot as current.
  `snapshot_time_epoch` is returned and never checked.

**The server drops what it cannot read, and counts the rest.**

- `absent-malformed-rows`: five aircraft in the payload, three of them not arrays. `states_in_bbox` skips
  them silently and the tool answers "2 aircraft" with an empty `unknowns`.

In all three the agent did what it was told: `server_flagged_a_gap` is `false` in the results, meaning the
server said nothing was missing. A careful model might catch the first two from the timestamps. The
contract, "each payload lists `unknowns`", does not hold for them.

**The server cannot read OpenSky's movements response.**

- `happy-movements-arrivals`, `happy-movements-both`: OpenSky documents `/flights/arrival` and
  `/flights/departure` as returning a JSON **array** of flights. `OpenSkyClient.movements` requires an object
  with an `arrival` or `departure` key and answers `upstream_unavailable` for anything else. The unit tests
  feed it the object it expects, so they pass. Against the documented shape `airport_movements` cannot
  succeed.
- `happy-triage-elevated`, `happy-triage-composite`: `disruption_triage` calls `airport_movements`, so it
  always has one source down and always reports severity `unknown`.

These four fail **Outcome** only: the right tool was called with the right arguments and the answer honestly
said "unknown". The system failed safe, which is the design working, and it is still a tool that cannot do
its job. This path has never run against the live API, because it needs OpenSky credentials.

Three smaller things the runs show that no scenario fails on:

- A dead connection to OpenSky or NOAA surfaces as `internal_error`, not `upstream_unavailable`
  (`absent-upstream-dead-opensky`). The operations API client catches `httpx.HTTPError`; the other two do not.
- An HTML error page from NOAA with status 200 is reported as "NOAA holds no current observation"
  (`absent-malformed-weather`). The answer is rightly "unknown", for a reason that is not the true one.
- `absent-partial-triage-traffic-down` passes with two sources down where the fixture takes down one, because
  of the movements defect above.

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
- **Staleness is relative to the scenario's clock, and the server runs on the wall clock.** Every fixture
  timestamp is "old" to the server. When the server learns to flag stale data, the stack will have to run the
  server on the scenario's clock, or the fresh fixtures will start failing instead.

So the offline numbers are a regression gate on the server's contract. They are not a claim about how good
an agent built on a model is, and the README says so beside the table.

## The gate

`--offline` exits non-zero when any of these holds:

| Failure | Why it is a failure |
|---|---|
| An axis, the outcome check or the overall figure is **more than 2 points** below the baseline | A regression. Points, not percent of percent: 95.0 against 97.5 is 2.5 down. `--max-regression` changes the limit. |
| The scenarios are not the ones the baseline was recorded for | An added, removed or edited scenario makes the two runs incomparable. Without this, deleting a failing scenario would raise the score. |
| The README block is not what the baseline renders to | The table is generated. A hand-edited number is a different block. |
| The run took longer than 60 s | `--max-seconds`. The duration is printed and written to the results. |

A run that is **better** than the baseline passes, says which scenarios now pass, and changes nothing. The
baseline moves only when someone runs `--update-baseline` and commits `evals/baseline.json` and the README
together. It cannot ratchet up by accident, and it cannot be lowered by accident either. CI never passes that
flag.

**What 2 points means in scenarios.** One scenario is worth `100 / N` points on an axis that applies to `N`
scenarios. So the limit bites differently per line of the table:

| Scenarios an axis applies to | One scenario is worth | One regression |
|---|---|---|
| fewer than 50 (refusal correctness, no hallucination) | more than 2 points | fails the build |
| 50 or more (tool selection, argument validity, outcome, overall) | 2 points or less | **passes**, printed as a `note:` line |

That is the limit working as specified, and it is weaker than it sounds on the wide axes: a single scenario
can regress there without failing the build, and two cannot. The note names the scenario and the check, so
it is visible in the job log, and it does not stop a merge. `--max-regression 0` makes any drop fail; whether
CI should run with it is an open decision, not something this phase changed.

A drop of exactly 2 points passes and is printed as a note. A fix and a break that cancel out leave the
score unchanged, so the build passes; both are still printed.

## Adding a scenario

1. Copy the nearest existing file in `evals/scenarios/` and name it after its `id`. The prefix is the group:
   `happy-`, `refusal-` or `absent-`.
2. Write `why` first: one sentence saying what the scenario is for. If it is the same sentence as an existing
   scenario's, it is the same scenario.
3. Describe the world in `fixture`, in the shape the upstream documents. An endpoint you leave out answers
   with nothing (204, no aircraft, no movements). A passenger row carries operational fields only; a name is
   refused by the loader.
4. Write `expect` from the question, before you look at what the system does: the tools a right answer needs
   (`required`, in order), the ones it may also use (`allowed`), the arguments that must be sent, the
   acceptable answer class, and `absent_evidence` or `refusal` if that is the point.
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
- **Outcome, with the seven known failures set aside.** No planner can pass the four movement and triage
  scenarios until the server can read the response. The three silent-gap scenarios are passable only by a
  model that reads the timestamps itself, which makes them the most interesting three in the set.
- **A person's reading of the answers.** The lexicon is a heuristic for prose. Read every failure on refusal
  correctness, no hallucination and outcome before believing it, and a sample of the passes.
