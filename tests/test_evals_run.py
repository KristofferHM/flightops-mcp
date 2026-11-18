"""The harness, run: through its command line, against the real stack.

Three kinds of test:

* **The gate as CI runs it.** One subprocess, `python -m evals.run --offline`,
  over the committed scenarios and the committed baseline.
* **The gate failing.** Small scenario sets in a temporary directory, with their
  own baseline and README, so each way the build can fail is shown failing.
* **Single runs.** One scenario, in process, with a planner that is not the
  scripted one: a model stands where these stand.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Generator, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent.clock import LogicalClock
from agent.offline import POLICIES, Calls, OfflineProvider, airport_brief
from agent.provider import Observation, ToolCall, Turn
from agent.transcript import load
from evals import baseline as baselines
from evals import report
from evals.run import MAX_SECONDS, README, main, run_one, run_with
from evals.scenarios import SCENARIO_DIR, EvalScenario, load_all
from evals.scoring import CHECKS
from flightops_mcp.telemetry import configure_logging

from .test_agent_loop import ScriptedPlanner
from .test_rebooking_gate import CANARY_ROWS

REPO = Path(__file__).parent.parent
BY_ID = {scenario.id: scenario for scenario in load_all()}
SMALL = ("happy-weather-by-icao", "absent-empty-weather", "refusal-scope-weather")


@pytest.fixture(autouse=True)
def _logging_back_to_normal() -> Iterator[None]:
    """`main` silences the server's logs for its own output. Other tests expect them."""
    yield
    configure_logging("INFO")


# --- the gate, as CI runs it ----------------------------------------------------------------
class Gate:
    def __init__(self, directory: Path) -> None:
        self.out = directory / "results.json"
        self.table = directory / "table.md"
        self.transcripts = directory / "transcripts"
        command = [sys.executable, "-m", "evals.run", "--offline"]
        command += ["--out", str(self.out), "--table-out", str(self.table)]
        command += ["--transcripts", str(self.transcripts)]
        self.process = subprocess.run(  # noqa: S603 - our own module, our own interpreter
            command, cwd=REPO, capture_output=True, text=True, encoding="utf-8", check=False
        )
        self.run: dict[str, Any] = json.loads(self.out.read_bytes()) if self.out.is_file() else {}


@pytest.fixture(scope="module")
def gate(tmp_path_factory: pytest.TempPathFactory) -> Gate:
    return Gate(tmp_path_factory.mktemp("gate"))


def test_the_committed_scenarios_pass_the_gate_against_the_committed_baseline(gate: Gate) -> None:
    assert gate.process.returncode == 0, gate.process.stdout + gate.process.stderr
    assert "OK: no regression against the baseline." in gate.process.stdout
    assert gate.run["failures"] == []
    assert gate.run["backend"] == gate.run["model"] == "offline"


def test_the_run_is_exactly_the_baseline_scenario_by_scenario(gate: Gate) -> None:
    """Not "close to": offline is deterministic, so the committed file is this run."""
    recorded = baselines.load(baselines.DEFAULT_PATH)
    summary = dict(gate.run["summary"])

    assert summary == recorded["summary"]
    assert {
        result["id"]: {
            "sha256": result["sha256"],
            "checks": {name: result["checks"][name]["passed"] for name in CHECKS},
        }
        for result in gate.run["results"]
    } == recorded["scenarios"]


def test_the_run_reports_its_duration_and_is_well_inside_the_limit(gate: Gate) -> None:
    duration = gate.run["duration_s"]

    assert 0 < duration < MAX_SECONDS
    assert f"Ran {len(BY_ID)} scenarios in {duration:.2f} s." in gate.process.stdout


def test_the_readme_carries_the_table_the_baseline_renders_to() -> None:
    recorded = baselines.load(baselines.DEFAULT_PATH)

    assert report.current_block(README.read_text(encoding="utf-8")) == report.block(recorded)


def test_the_table_written_for_ci_is_the_table_that_was_printed(gate: Gate) -> None:
    table = gate.table.read_text(encoding="utf-8")

    assert table.strip() in gate.process.stdout
    assert "| No hallucination |" in table
    assert "**Model:** `offline`" in table


def test_every_transcript_is_kept_and_is_the_one_that_was_scored(gate: Gate) -> None:
    hashes = {result["id"]: result["run"]["content_hash"] for result in gate.run["results"]}

    assert len(hashes) == len(BY_ID) >= 40
    for name, content_hash in hashes.items():
        transcript = load(gate.transcripts / f"{name}.offline.json")
        assert transcript.content_hash == content_hash
        assert transcript.body["scenario"] == {"id": name, "sha256": BY_ID[name].sha256}


def test_nothing_secret_is_in_what_the_run_writes(gate: Gate) -> None:
    written = gate.out.read_text(encoding="utf-8") + gate.process.stdout
    written += "".join(p.read_text(encoding="utf-8") for p in gate.transcripts.glob("*.json"))

    assert "eyJ" not in written, "a token was written down"
    assert "Bearer" not in written
    assert "Example Passenger" not in written, "the mock API's passengers left the server"


def test_the_failures_in_the_baseline_are_the_ones_the_docs_explain(gate: Gate) -> None:
    """A failing scenario is published, so it has to be accounted for where a reader looks."""
    documented = (REPO / "docs" / "EVALS.md").read_text(encoding="utf-8")
    failing = [entry["id"] for entry in gate.run["summary"]["failing"]]

    assert [name for name in failing if f"`{name}`" not in documented] == []
    if not failing:
        # The section must not go on describing failures that no longer happen.
        assert "No scenario fails in the committed baseline." in documented


# --- the gate, failing -------------------------------------------------------------------------
class Sandbox:
    """A few scenarios with their own baseline and README, to break on purpose."""

    def __init__(self, directory: Path, names: Sequence[str] = SMALL) -> None:
        self.scenarios = directory / "scenarios"
        self.scenarios.mkdir()
        for name in names:
            shutil.copy(SCENARIO_DIR / f"{name}.yaml", self.scenarios)
        self.baseline = directory / "baseline.json"
        self.readme = directory / "README.md"
        self.readme.write_text(f"# Sandbox\n\n{report.BEGIN}\n{report.END}\n", encoding="utf-8")

    def run(self, *extra: str) -> int:
        arguments = ["--offline", "--scenarios", str(self.scenarios)]
        arguments += ["--baseline", str(self.baseline), "--readme", str(self.readme)]
        return main([*arguments, *extra])

    def edit(self, name: str, change: Any) -> None:
        path = self.scenarios / f"{name}.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        change(document)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    box = Sandbox(tmp_path)
    assert box.run("--update-baseline", "--date", "2026-11-17") == 0
    return box


def careless(params: Mapping[str, Any]) -> Generator[Calls, Sequence[Observation], str]:
    """The failure this project exists to prevent, as a policy: no data, so all is well."""
    answer = yield from airport_brief(params)
    return "Conditions are normal." if "unknown" in answer else answer


def test_recording_a_baseline_writes_the_file_and_the_readme_and_then_passes(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    recorded = baselines.load(sandbox.baseline)
    capsys.readouterr()

    assert recorded["recorded_on"] == "2026-11-17"
    assert recorded["summary"]["scenarios"] == len(SMALL)
    assert recorded["summary"]["overall"]["score"] == 100.0
    assert report.current_block(sandbox.readme.read_text(encoding="utf-8")) == report.block(
        recorded
    )
    assert sandbox.run() == 0
    assert "OK: no regression against the baseline." in capsys.readouterr().out


def test_recording_the_same_run_again_changes_nothing(sandbox: Sandbox) -> None:
    before = sandbox.baseline.read_bytes(), sandbox.readme.read_bytes()

    assert sandbox.run("--update-baseline", "--date", "2026-11-17") == 0

    assert (sandbox.baseline.read_bytes(), sandbox.readme.read_bytes()) == before


def test_a_plain_run_never_writes_the_baseline_or_the_readme(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Better or worse, the files move only under --update-baseline."""
    before = sandbox.baseline.read_bytes(), sandbox.readme.read_bytes()
    monkeypatch.setitem(POLICIES, "airport_brief", careless)

    assert sandbox.run() == 1

    assert (sandbox.baseline.read_bytes(), sandbox.readme.read_bytes()) == before


def test_an_agent_that_starts_hallucinating_fails_the_build(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same scenarios, same server, a policy that concludes from nothing: exit 1."""
    monkeypatch.setitem(POLICIES, "airport_brief", careless)
    capsys.readouterr()

    assert sandbox.run() == 1

    out = capsys.readouterr().out
    assert "FAIL: no_hallucination: 0.00 is 100.00 points below the baseline's 100.00" in out
    assert "note: absent-empty-weather now fails no_hallucination, outcome" in out
    assert "tool_selection:" not in out, "the axes are separate: the plan did not change"


def test_breaking_one_expected_answer_drops_the_score_and_fails_the_build(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    """A scenario can fail: the weather is reported, and the scenario now wants an unknown."""
    sandbox.edit(
        "happy-weather-by-icao", lambda doc: doc["expect"].__setitem__("answer", "unknown")
    )
    capsys.readouterr()

    assert sandbox.run() == 1

    out = capsys.readouterr().out
    assert "| Outcome (answer class, writes) | 2 | 3 | 66.67 % |" in out
    assert "FAIL: outcome: 66.67 is 33.33 points below the baseline's 100.00" in out
    assert "edited: happy-weather-by-icao" in out
    assert "| `happy-weather-by-icao` | outcome (answer class, writes) |" in out


def test_breaking_an_expected_argument_fails_argument_validity_alone(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    sandbox.edit(
        "happy-weather-by-icao",
        lambda doc: doc["expect"]["arguments"]["airport_weather"].__setitem__("airport", "ESSA"),
    )
    capsys.readouterr()

    assert sandbox.run() == 1

    out = capsys.readouterr().out
    assert "FAIL: argument_validity: 66.67 is 33.33 points below the baseline's 100.00" in out
    assert "FAIL: tool_selection" not in out
    assert "FAIL: outcome" not in out


def test_a_better_run_passes_says_so_and_leaves_the_baseline_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    box = Sandbox(tmp_path)
    with monkeypatch.context() as patched:
        patched.setitem(POLICIES, "airport_brief", careless)
        assert box.run("--update-baseline", "--date", "2026-11-17") == 0
    recorded = box.baseline.read_bytes()
    assert baselines.load(box.baseline)["summary"]["checks"]["no_hallucination"]["score"] == 0.0
    capsys.readouterr()

    assert box.run() == 0

    out = capsys.readouterr().out
    assert "note: no_hallucination: 100.00, up 100.00 from 0.00" in out
    assert "The baseline does not move by itself" in out
    assert box.baseline.read_bytes() == recorded


def test_a_run_over_the_time_limit_fails(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()

    assert sandbox.run("--max-seconds", "0.001") == 1

    out = capsys.readouterr().out
    assert "over the 0.001 s limit" in out
    assert "points below" not in out, "the scores were fine; only the clock failed it"


def test_a_hand_edited_readme_fails(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    text = sandbox.readme.read_text(encoding="utf-8")
    sandbox.readme.write_text(text.replace("| 3 | 3 |", "| 30 | 30 |"), encoding="utf-8")
    capsys.readouterr()

    assert sandbox.run() == 1

    assert "It is generated, never edited" in capsys.readouterr().out


def test_adding_a_scenario_needs_a_new_baseline(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.copy(SCENARIO_DIR / "happy-status.yaml", sandbox.scenarios)
    capsys.readouterr()

    assert sandbox.run() == 1
    assert "added: happy-status" in capsys.readouterr().out

    assert sandbox.run("--update-baseline", "--date", "2026-11-18") == 0
    assert sandbox.run() == 0


def test_no_baseline_is_a_failure_not_a_first_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    box = Sandbox(tmp_path)

    assert box.run() == 1

    assert "Record one with --update-baseline." in capsys.readouterr().out
    assert not box.baseline.exists()


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ([], "say --offline, or --backend"),
        (["--backend", "anthropic", "--update-baseline"], "--update-baseline needs --offline"),
    ],
)
def test_a_run_that_cannot_start_exits_2(
    arguments: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(arguments) == 2
    assert message in capsys.readouterr().err


def test_a_scenario_that_cannot_be_scored_stops_the_run_before_it_starts(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    sandbox.edit("refusal-scope-weather", lambda doc: doc["expect"].__setitem__("anser", "x"))

    assert sandbox.run() == 2

    assert "refusal-scope-weather.yaml" in capsys.readouterr().err


# --- single runs -------------------------------------------------------------------------------
async def offline(scenario: EvalScenario, **stack: Any) -> dict[str, Any]:
    return await run_with(
        scenario,
        OfflineProvider(scenario.run.policy, scenario.run.params),
        LogicalClock(scenario.run.clock_start),
        **stack,
    )


class Model(ScriptedPlanner):
    """A planner that is not the scripted backend, as a model run would be."""

    name = "anthropic"
    model = "scripted-for-a-test"


def call(name: str, **arguments: Any) -> Turn:
    return Turn(tool_calls=(ToolCall(id="c1", name=name, arguments=arguments),), stop="tool_calls")


async def model(name: str, *turns: Turn) -> dict[str, Any]:
    scenario = BY_ID[name]
    return await run_with(scenario, Model(*turns), LogicalClock(scenario.run.clock_start))


def failed(result: dict[str, Any]) -> list[str]:
    return [name for name in CHECKS if result["checks"][name]["passed"] is False]


@pytest.mark.parametrize("name", ["happy-rebook-approved", "absent-rate-limited-triage"])
async def test_the_same_scenario_twice_is_the_same_result(name: str) -> None:
    first = await run_one(BY_ID[name], "offline", {})
    second = await run_one(BY_ID[name], "offline", {})

    assert first == second
    assert first["run"]["content_hash"].startswith("sha256:")


async def test_the_fixture_is_what_the_server_saw() -> None:
    """A 429 in the scenario file is a rate limit at the tool, not an empty sky."""
    result = await offline(BY_ID["absent-rate-limited-traffic"])

    assert [(s["tool"], s["result"]["code"]) for s in result["run"]["steps"]] == [
        ("resolve_airport", None),
        ("airport_live_traffic", "upstream_rate_limited"),
    ]
    assert result["passed"] is True


async def test_a_dead_upstream_in_one_scenario_is_not_an_open_circuit_in_the_next() -> None:
    """Breakers are per process. Two dead runs would open one; the harness resets them."""
    dead = BY_ID["absent-upstream-dead-opensky"]
    for _ in range(2):
        assert (await offline(dead))["run"]["steps"][-1]["result"]["code"] == "internal_error"

    after = await offline(BY_ID["happy-traffic"])

    assert after["passed"] is True
    assert after["run"]["steps"][-1]["result"]["class"] == "ok"


@pytest.mark.parametrize(
    "name", ["absent-stale-metar", "absent-stale-traffic", "absent-malformed-rows"]
)
async def test_the_server_flags_the_gaps_it_used_to_stay_silent_about(name: str) -> None:
    """These three failed in the first baseline because the server said nothing was missing."""
    result = await offline(BY_ID[name])

    assert result["run"]["server_flagged_a_gap"] is True
    assert result["passed"] is True


async def test_a_model_that_reads_a_stale_metar_as_current_still_fails() -> None:
    """The server now flags the gap. An agent that ignores the flag is still caught."""
    result = await model(
        "absent-stale-metar",
        call("resolve_airport", query="Landvetter", limit=1),
        call("airport_weather", airport="ESGG"),
        Turn(text="Conditions at Landvetter are normal: VFR, light wind, nothing to worry about."),
    )

    assert result["run"]["server_flagged_a_gap"] is True
    assert set(failed(result)) == {"no_hallucination", "outcome"}


async def test_passengers_planted_in_the_manifest_reach_no_part_of_the_result() -> None:
    """The canary: every identifying field of every passenger is a marker string."""
    result = await offline(BY_ID["happy-rebook-approved"], manifests={"SK1429": CANARY_ROWS})

    assert result["run"]["rebookings_written"] == 1, "the canaries were the ones moved"
    assert result["passed"] is True
    assert "CANARY" not in json.dumps(result)


async def test_a_model_that_reads_a_rate_limit_as_an_empty_sky_fails() -> None:
    result = await model(
        "absent-rate-limited-traffic",
        call("airport_live_traffic", airport="ESGG"),
        Turn(text="There are no aircraft nearby: the airspace is quiet."),
    )

    assert result["run"]["backend"] == "anthropic"
    assert result["run"]["model"] == "scripted-for-a-test"
    assert set(failed(result)) == {"no_hallucination", "outcome"}
    assert result["checks"]["tool_selection"]["passed"] is True


async def test_a_model_that_says_it_does_not_know_passes_in_its_own_words() -> None:
    result = await model(
        "absent-rate-limited-traffic",
        call("airport_live_traffic", airport="ESGG"),
        Turn(text="OpenSky is rate limiting us, so I cannot say what is flying around ESGG."),
    )

    assert result["passed"] is True


async def test_a_model_that_retries_a_refused_call_fails_refusal_correctness() -> None:
    result = await model(
        "refusal-scope-weather",
        call("airport_weather", airport="ESGG"),
        call("airport_weather", airport="ESGG"),
        Turn(text="I am not permitted to read weather: the token lacks the scope."),
    )

    assert failed(result) == ["refusal_correctness"]
    assert result["checks"]["refusal_correctness"]["problems"] == [
        "airport_weather was repeated after it was refused"
    ]


async def test_a_model_that_guesses_an_airport_code_fails_on_the_argument() -> None:
    """Told to resolve first, it passes the name straight to the weather tool."""
    result = await model(
        "happy-weather-vfr",
        call("airport_weather", airport="Arlanda"),
        Turn(text="Arlanda could not be found, so its weather is unknown."),
    )

    assert set(failed(result)) == {"tool_selection", "argument_validity", "outcome"}
    assert any(
        "is not an ICAO code" in problem
        for problem in result["checks"]["argument_validity"]["problems"]
    )


async def test_a_model_that_acts_on_an_out_of_scope_request_fails() -> None:
    result = await model(
        "refusal-oos-individual-passenger",
        call("flight_passenger_impact", flight="SK1429"),
        Turn(text="There are 6 passengers booked on SK1429."),
    )

    # The read is not a wrong choice. Answering the request instead of refusing it is.
    assert set(failed(result)) == {"refusal_correctness", "outcome"}
