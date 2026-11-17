"""The committed scenarios, and the loader that refuses a scenario it cannot score.

The first half counts what is in `evals/scenarios/`: these are Phase 4's
acceptance criteria as assertions. The second half is the loader's strictness,
which is what stops an expectation from being silently ignored.
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from agent.offline import POLICIES
from agent.scenarios import ScenarioInvalid
from evals.fixtures import build
from evals.scenarios import (
    ANSWER_CLASSES,
    GROUPS,
    REQUIRED_ABSENT_KINDS,
    SCENARIO_DIR,
    EvalScenario,
    load,
    load_all,
    parse,
)
from flightops_mcp.auth.scopes import Scopes

EVALS_DIR = SCENARIO_DIR.parent
SCENARIOS = load_all()


def raw(name: str) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(
        (SCENARIO_DIR / f"{name}.yaml").read_text(encoding="utf-8")
    )
    return document


# --- what is committed ------------------------------------------------------------------
def test_there_are_at_least_forty_scenarios() -> None:
    assert len(SCENARIOS) >= 40
    assert len({scenario.id for scenario in SCENARIOS}) == len(SCENARIOS)
    assert len({scenario.sha256 for scenario in SCENARIOS}) == len(SCENARIOS)


def test_at_least_eight_scenarios_are_about_absent_evidence_and_no_kind_is_missing() -> None:
    """The acceptance criterion, and its fine print: eight that are genuinely distinct."""
    absent = [s for s in SCENARIOS if s.group == "absent-evidence"]
    kinds = {s.expect.absent_evidence for s in absent}

    assert len(absent) >= 8
    assert set(REQUIRED_ABSENT_KINDS) <= kinds, set(REQUIRED_ABSENT_KINDS) - kinds
    assert all(s.id.startswith("absent-") for s in absent), "the group is visible by file name"
    assert [s for s in SCENARIOS if s.id.startswith("absent-")] == absent


def test_absent_evidence_scenarios_differ_in_what_the_upstream_does() -> None:
    """Distinct means a different state of the world, not the same fixture with a new name."""
    absent = [s for s in SCENARIOS if s.group == "absent-evidence"]
    worlds = {yaml.safe_dump([s.fixture, s.run.policy, dict(s.run.params)]) for s in absent}

    assert len(worlds) == len(absent)


def test_every_group_and_every_refusal_the_roadmap_names_is_covered() -> None:
    assert {s.group for s in SCENARIOS} == set(GROUPS)
    refusals = {s.expect.refusal.kind for s in SCENARIOS if s.expect.refusal}
    assert {"missing_scope", "missing_credential", "out_of_scope"} <= refusals


def test_every_tool_is_the_right_answer_to_some_question() -> None:
    required = {tool for scenario in SCENARIOS for tool in scenario.expect.required}

    assert required == set(Scopes._BY_TOOL)  # noqa: SLF001 - the table is the list of tools


def test_some_question_is_answered_by_calling_nothing() -> None:
    silent = [s for s in SCENARIOS if not s.expect.required and not s.expect.allowed]

    assert len(silent) >= 3


def test_every_answer_class_is_expected_somewhere() -> None:
    expected = {klass for scenario in SCENARIOS for klass in scenario.expect.answer}

    assert expected == set(ANSWER_CLASSES)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.id)
def test_each_scenario_can_be_run_as_written(scenario: EvalScenario) -> None:
    assert scenario.run.policy in POLICIES
    assert scenario.run.sha256 == scenario.sha256, "the transcript names this version of the file"
    upstreams, manifests = build(scenario.fixture)
    assert upstreams is not None
    for rows in (manifests or {}).values():
        for row in rows:
            assert set(row) == {
                "checked_in",
                "connecting_flight",
                "connection_minutes",
                "needs_assistance",
            }


def test_no_scenario_file_names_a_passenger_or_carries_a_credential() -> None:
    """Rule 5, for the new directory: nothing secret and nobody who flies."""
    forbidden = (
        "passenger_name",
        "email:",
        "phone:",
        "booking_ref",
        "date_of_birth",
        "bearer ",
        "eyj",
        "client_secret",
        "password",
        "@",
    )
    for path in sorted(SCENARIO_DIR.glob("*.yaml")):
        text = path.read_text(encoding="utf-8").lower()
        assert not [word for word in forbidden if word in text], path.name


def test_only_the_runner_reads_a_clock() -> None:
    """Scoring, the baseline and the table are functions of their input. Only `run.py`
    measures a duration and stamps a date."""
    found: list[str] = []
    scanned = 0
    for path in sorted(EVALS_DIR.glob("*.py")):
        if path.name == "run.py":
            continue
        scanned += 1
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [(node.module or "").split(".")[0]]
            elif isinstance(node, ast.Attribute):
                names = [ast.unparse(node)]
            found.extend(
                f"{path.name}:{node.lineno} {name}"
                for name in names
                if name in {"time", "random", "secrets", "uuid", "datetime.now", "date.today"}
            )

    assert scanned >= 7, "the scan found fewer modules than exist; fix the scan"
    assert found == []


# --- the loader ---------------------------------------------------------------------------
def test_a_misspelt_expectation_is_refused_not_ignored() -> None:
    """`anwser: unknown` that loads is a check that never runs."""
    document = raw("absent-empty-weather")
    document["expect"]["anwser"] = document["expect"].pop("answer")

    with pytest.raises(ScenarioInvalid, match="unknown key.*anwser"):
        parse(document)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("expect", "tools", "required"), ["airport_wether"], "is not a tool"),
        (("expect", "answer"), "probably fine", "expect.answer must be among"),
        (("expect", "absent_evidence"), "vibes", "expect.absent_evidence must be one of"),
        (("expect", "arguments"), {"upstream_status": {}}, "is not a required tool"),
        (("expect", "writes"), -1, "expect.writes must be a count"),
        (("group",), "misc", "group must be one of"),
        (("why",), "  ", "why must say what the scenario is for"),
        (("surprise",), True, "unknown key"),
        (("offline", "policy"), "improvise", "is not an offline policy"),
    ],
)
def test_a_scenario_that_cannot_be_scored_is_refused(
    path: tuple[str, ...], value: Any, message: str
) -> None:
    document = copy.deepcopy(raw("absent-empty-weather"))
    target = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(ScenarioInvalid, match=message):
        parse(document)


def test_the_absent_evidence_group_is_exactly_the_scenarios_that_say_which_kind() -> None:
    hidden = copy.deepcopy(raw("absent-empty-weather"))
    hidden["group"] = "happy-path"
    unlabelled = copy.deepcopy(raw("absent-empty-weather"))
    del unlabelled["expect"]["absent_evidence"]

    for document in (hidden, unlabelled):
        with pytest.raises(ScenarioInvalid, match="absent-evidence group is exactly"):
            parse(document)


def test_an_absent_evidence_scenario_cannot_accept_a_confident_answer() -> None:
    document = copy.deepcopy(raw("absent-empty-weather"))
    document["expect"]["answer"] = "reported"

    with pytest.raises(ScenarioInvalid, match="must accept an unknown or a refusal"):
        parse(document)


def test_a_refusal_names_the_call_and_the_code_unless_nothing_should_be_called() -> None:
    document = copy.deepcopy(raw("refusal-scope-weather"))
    del document["expect"]["refusal"]["code"]
    with pytest.raises(ScenarioInvalid, match="needs a required tool and the code"):
        parse(document)

    silent = copy.deepcopy(raw("refusal-oos-hotel"))
    silent["expect"]["refusal"]["tool"] = "airport_weather"
    with pytest.raises(ScenarioInvalid, match="names no tool"):
        parse(silent)


def test_a_file_whose_name_is_not_its_id_is_refused(tmp_path: Path) -> None:
    source = SCENARIO_DIR / "happy-status.yaml"
    renamed = tmp_path / "something-else.yaml"
    renamed.write_bytes(source.read_bytes())

    with pytest.raises(ScenarioInvalid, match="is not the file's name"):
        load(renamed)


def test_an_empty_directory_is_not_a_passing_run(tmp_path: Path) -> None:
    with pytest.raises(ScenarioInvalid, match="no scenarios"):
        load_all(tmp_path)


def test_editing_any_part_of_a_scenario_changes_its_digest() -> None:
    """The baseline is recorded for these digests. An edit must be visible to it."""
    original = raw("absent-empty-weather")
    expectation = copy.deepcopy(original)
    expectation["expect"]["answer"] = ["unknown", "refused"]
    fixture = copy.deepcopy(original)
    fixture["fixture"]["weather"]["taf"] = {"status": 503}

    digests = {parse(document).sha256 for document in (original, expectation, fixture)}

    assert len(digests) == 3


# --- fixtures -------------------------------------------------------------------------------
def test_a_passenger_with_a_name_cannot_be_put_in_a_fixture() -> None:
    fixture = {"ops": {"manifests": {"SK1429": [{"passenger_name": "A Person"}]}}}

    with pytest.raises(ScenarioInvalid, match="may only hold"):
        build(fixture)


@pytest.mark.parametrize(
    "fixture",
    [
        {"wether": {}},
        {"weather": {"metars": {"status": 204}}},
        {"weather": {"metar": {"status": 200, "body": "x"}}},
        {"weather": {"metar": {"status": 200, "json": [], "text": "x"}}},
        {"opensky": {"states": {"error": "meteor"}}},
        {"opensky": {"states": {"error": "connect", "status": 200}}},
        {"opensky": {"credentials": "no"}},
        {"ops": {"seats": {"SK1431": "four"}}},
    ],
)
def test_a_fixture_that_says_something_unknown_is_refused(fixture: dict[str, Any]) -> None:
    with pytest.raises(ScenarioInvalid):
        build(fixture)
