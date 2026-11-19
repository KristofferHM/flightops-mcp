"""The scorer, one axis at a time. Every axis is shown passing and shown failing.

A harness in which nothing can fail looks like evidence and is not. So each test
here is a pair: a run that is right, and the smallest change that makes it wrong.
The transcripts are built by hand, so what is scored is exactly what is written.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.scenarios import ScenarioInvalid
from evals import answers
from evals.domain import domain_problems, known_airports, schema_problems
from evals.scoring import AXES, CHECKS, score, summarise

from .evalkit import OFFERED, evidence, scenario, step

WEATHER = {
    "tools": {"required": ["airport_weather"], "allowed": ["resolve_airport"]},
    "arguments": {"airport_weather": {"airport": "ESGG"}},
    "answer": "reported",
}
WEATHER_ONLY = {**WEATHER, "tools": {"required": ["airport_weather"]}}
RESOLVE_FIRST = {**WEATHER, "tools": {"required": ["resolve_airport", "airport_weather"]}}
RESOLVED = step("resolve_airport", {"query": "ESGG", "limit": 1})
READ = step("airport_weather", {"airport": "ESGG"})
TRAFFIC = step("airport_live_traffic", {"airport": "ESGG"})
REPORT = "ESGG: 0 weather impact flag(s) reported."


def failed(result: dict[str, Any]) -> list[str]:
    return [name for name in CHECKS if result["checks"][name]["passed"] is False]


# --- tool selection -----------------------------------------------------------------
def test_the_right_tools_in_the_right_order_pass_everything_that_applies() -> None:
    result = score(scenario(WEATHER), evidence([RESOLVED, READ], REPORT))

    assert result["passed"] is True
    assert failed(result) == []
    assert result["checks"]["refusal_correctness"]["applicable"] is False
    assert result["checks"]["no_hallucination"]["applicable"] is False


def test_a_required_tool_that_was_not_called_fails_tool_selection() -> None:
    result = score(scenario(WEATHER), evidence([RESOLVED], REPORT))

    assert "tool_selection" in failed(result)
    assert result["checks"]["tool_selection"]["problems"] == ["airport_weather was not called"]
    assert result["passed"] is False


def test_an_extra_read_beside_the_required_tool_passes() -> None:
    """The measured defect: a thorough agent was scored as a wrong one."""
    result = score(scenario(WEATHER_ONLY), evidence([RESOLVED, TRAFFIC, READ], REPORT))

    assert result["checks"]["tool_selection"] == {
        "applicable": True,
        "passed": True,
        "problems": [],
    }
    assert result["passed"] is True


def test_an_extra_read_does_not_stand_in_for_a_required_tool() -> None:
    result = score(scenario(WEATHER_ONLY), evidence([TRAFFIC], REPORT))

    assert result["checks"]["tool_selection"]["problems"] == ["airport_weather was not called"]


def test_a_forbidden_tool_fails_even_beside_the_required_one() -> None:
    expect = {**WEATHER_ONLY, "forbidden_tools": ["propose_rebooking"]}
    write = step("propose_rebooking", REBOOKING, code="approval_required")

    clean = score(scenario(expect), evidence([READ], REPORT))
    result = score(scenario(expect), evidence([READ, write], REPORT))

    assert clean["passed"] is True
    assert failed(result) == ["tool_selection"]
    assert result["checks"]["tool_selection"]["problems"] == [
        "propose_rebooking was called; this scenario forbids it"
    ]


def test_a_declared_order_that_is_violated_fails_whatever_else_was_read() -> None:
    result = score(scenario(RESOLVE_FIRST), evidence([READ, TRAFFIC, RESOLVED], REPORT))

    assert failed(result) == ["tool_selection"]
    assert result["checks"]["tool_selection"]["problems"] == [
        "airport_weather was called out of order"
    ]


def test_a_declared_order_survives_an_extra_read_between_its_steps() -> None:
    result = score(scenario(RESOLVE_FIRST), evidence([RESOLVED, TRAFFIC, READ], REPORT))

    assert result["checks"]["tool_selection"]["passed"] is True
    assert result["passed"] is True


def test_required_tools_called_in_the_wrong_order_fail_tool_selection() -> None:
    expect = {**WEATHER, "tools": {"required": ["resolve_airport", "airport_weather"]}}

    result = score(scenario(expect), evidence([READ, RESOLVED], REPORT))

    assert failed(result) == ["tool_selection"]
    assert result["checks"]["tool_selection"]["problems"] == [
        "airport_weather was called out of order"
    ]


def test_with_nothing_required_only_a_forbidden_tool_fails_tool_selection() -> None:
    expect = {"tools": {"required": []}, "answer": "refused", "refusal": {"kind": "out_of_scope"}}
    barred = {**expect, "forbidden_tools": ["airport_weather"]}
    refusal = "That is not something this server can do."

    quiet = score(scenario(barred), evidence([], refusal))
    looked = score(scenario(expect), evidence([READ], refusal))
    busy = score(scenario(barred), evidence([READ], refusal))

    assert quiet["passed"] is True
    assert looked["checks"]["tool_selection"]["passed"] is True
    assert failed(busy) == ["tool_selection"]


@pytest.mark.parametrize(
    ("forbidden", "message"),
    [
        (["airport_weather"], "both required and forbidden"),
        (["resolve_airport"], "both allowed and forbidden"),
        (["no_such_tool"], "which is not a tool"),
        ("propose_rebooking", "must be a list of tool names"),
    ],
)
def test_a_forbidden_list_that_cannot_mean_anything_is_refused(
    forbidden: Any, message: str
) -> None:
    with pytest.raises(ScenarioInvalid, match=message):
        scenario({**WEATHER, "forbidden_tools": forbidden})


# --- argument validity ----------------------------------------------------------------
def test_an_argument_of_the_wrong_type_fails_the_schema() -> None:
    wrong = step("airport_weather", {"airport": 7})

    result = score(scenario(WEATHER), evidence([wrong], REPORT))

    assert failed(result) == ["argument_validity"]
    problems = result["checks"]["argument_validity"]["problems"]
    assert any("is not of type 'string'" in problem for problem in problems)


def test_a_missing_required_argument_fails_the_schema() -> None:
    result = score(scenario(WEATHER), evidence([step("airport_weather", {})], REPORT))

    assert "argument_validity" in failed(result)
    assert any(
        "'airport' is a required property" in problem
        for problem in result["checks"]["argument_validity"]["problems"]
    )


def test_a_name_where_a_code_belongs_is_schema_valid_and_still_wrong() -> None:
    """The schema accepts any string. The domain knows a name is not an ICAO code."""
    guessed = step("airport_weather", {"airport": "Gothenburg"})

    result = score(scenario(WEATHER), evidence([guessed], REPORT))

    assert schema_problems(OFFERED[1].input_schema, {"airport": "Gothenburg"}) == []
    assert failed(result) == ["argument_validity"]
    assert "is not an ICAO code" in result["checks"]["argument_validity"]["problems"][0]


def test_an_airport_code_that_does_not_exist_fails_the_domain() -> None:
    expect = {**WEATHER, "arguments": {}}
    invented = step("airport_weather", {"airport": "ESXX"})

    result = score(scenario(expect), evidence([invented], REPORT))

    assert "ESGG" in known_airports()
    assert "ESXX" not in known_airports()
    assert failed(result) == ["argument_validity"]
    assert "not in the reference data" in result["checks"]["argument_validity"]["problems"][0]


def test_valid_arguments_that_are_not_the_expected_ones_fail() -> None:
    """ESSA is a real airport. It is not the one the question was about."""
    elsewhere = step("airport_weather", {"airport": "ESSA"})

    result = score(scenario(WEATHER), evidence([elsewhere], REPORT))

    assert failed(result) == ["argument_validity"]
    assert result["checks"]["argument_validity"]["problems"] == [
        "no call of airport_weather carried airport='ESGG'"
    ]


def test_a_number_matches_whether_it_is_written_as_an_integer_or_a_float() -> None:
    expect = {
        "tools": {"required": ["airport_live_traffic"]},
        "arguments": {"airport_live_traffic": {"radius_nm": 80}},
        "answer": "reported",
    }
    call = step("airport_live_traffic", {"airport": "ESGG", "radius_nm": 80.0})

    result = score(scenario(expect), evidence([call], "ESGG: 2 aircraft heard."))

    assert result["passed"] is True


@pytest.mark.parametrize(
    ("tool", "arguments", "problem"),
    [
        ("airport_live_traffic", {"airport": "ESGG", "radius_nm": 900}, "radius_nm 900 is outside"),
        ("airport_movements", {"airport": "ESGG", "direction": "sideways"}, "direction 'sideways'"),
        ("airport_movements", {"airport": "ESGG", "window_minutes": 600}, "window_minutes 600"),
        ("resolve_airport", {"query": "   "}, "query is empty"),
        ("flight_passenger_impact", {"flight": "the late one"}, "is not a flight designator"),
        ("disruption_triage", {"airport": "esgg"}, "is not an ICAO code"),
    ],
)
def test_values_the_server_would_tolerate_are_still_domain_errors(
    tool: str, arguments: dict[str, Any], problem: str
) -> None:
    from datetime import date

    problems = domain_problems(tool, arguments, today=date(2026, 11, 16))

    assert any(problem in found for found in problems), problems


REBOOKING = {
    "from_flight": "SK1429",
    "flight_date": "2026-11-16",
    "to_flight": "SK1431",
    "cohort": "tight_connections",
    "reason_code": "late_inbound",
    "passenger_count": 2,
}


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({}, None),
        ({"flight_date": "2025-11-16"}, "flight_date 2025-11-16 is outside"),
        ({"flight_date": "2026-12-25"}, "flight_date 2026-12-25 is outside"),
        ({"flight_date": "16/11/2026"}, "is not YYYY-MM-DD"),
        ({"cohort": "everyone in row 4"}, "is not a cohort"),
        ({"reason_code": "because"}, "is not a reason code"),
        ({"passenger_count": 0}, "is not a count of passengers"),
        ({"passenger_count": True}, "is not a count of passengers"),
        ({"to_flight": "SK1429"}, "are the same flight"),
    ],
)
def test_a_rebooking_is_checked_against_the_scenario_s_own_date(
    change: dict[str, Any], problem: str | None
) -> None:
    from datetime import date

    problems = domain_problems(
        "propose_rebooking", {**REBOOKING, **change}, today=date(2026, 11, 16)
    )

    if problem is None:
        assert problems == []
    else:
        assert any(problem in found for found in problems), problems


def test_a_scenario_with_nothing_to_call_has_no_arguments_to_judge() -> None:
    expect = {"tools": {"required": []}, "answer": "refused", "refusal": {"kind": "out_of_scope"}}

    result = score(scenario(expect), evidence([], "That is out of scope."))

    assert result["checks"]["argument_validity"] == {
        "applicable": False,
        "passed": None,
        "problems": [],
    }


# --- refusal correctness --------------------------------------------------------------
REFUSED = {
    "tools": {"required": ["airport_weather"], "allowed": ["resolve_airport"]},
    "answer": "refused",
    "refusal": {"kind": "missing_scope", "tool": "airport_weather", "code": "insufficient_scope"},
}
DENIED = step("airport_weather", {"airport": "ESGG"}, code="insufficient_scope")
SAID_SO = "Weather for ESGG is unavailable (insufficient_scope). Conditions are unknown."


def test_a_refusal_that_is_met_and_reported_passes() -> None:
    result = score(scenario(REFUSED), evidence([RESOLVED, DENIED], SAID_SO))

    assert result["passed"] is True
    assert result["checks"]["refusal_correctness"]["passed"] is True


def test_repeating_a_refused_call_fails_refusal_correctness() -> None:
    result = score(scenario(REFUSED), evidence([DENIED, DENIED], SAID_SO))

    assert failed(result) == ["refusal_correctness"]
    assert result["checks"]["refusal_correctness"]["problems"] == [
        "airport_weather was repeated after it was refused"
    ]


def test_an_answer_that_hides_the_refusal_fails_refusal_correctness() -> None:
    """The server refused. The answer carries on as though it had data."""
    result = score(scenario(REFUSED), evidence([DENIED], REPORT))

    assert "refusal_correctness" in failed(result)
    assert (
        "the answer does not say that something was refused or not done"
        in result["checks"]["refusal_correctness"]["problems"]
    )


def test_a_refusal_the_run_never_met_fails_refusal_correctness() -> None:
    """The scenario is about a refusal. A run in which the call succeeded is not it."""
    result = score(scenario(REFUSED), evidence([READ], SAID_SO))

    assert failed(result) == ["refusal_correctness"]
    assert result["checks"]["refusal_correctness"]["problems"] == [
        "airport_weather was never refused with insufficient_scope"
    ]


def test_a_write_after_a_refusal_fails_whatever_the_answer_says() -> None:
    expect = {
        "tools": {"required": ["propose_rebooking"]},
        "answer": "refused",
        "refusal": {"kind": "approval", "tool": "propose_rebooking", "code": "approval_declined"},
    }
    declined = step("propose_rebooking", REBOOKING, code="approval_declined")
    honest = "Nothing was rebooked. The proposal was refused: approval_declined."

    clean = score(scenario(expect), evidence([declined], honest))
    wrote = score(scenario(expect), evidence([declined], honest, written=1))
    boasted = score(scenario(expect), evidence([declined], "Rebooked 2 passenger(s) to SK1431."))

    assert clean["passed"] is True
    assert (
        "the operations API wrote 1 rebooking(s), expected 0"
        in (wrote["checks"]["refusal_correctness"]["problems"])
    )
    assert (
        "the answer says passengers were rebooked"
        in (boasted["checks"]["refusal_correctness"]["problems"])
    )


def test_proposing_a_write_for_an_out_of_scope_request_fails() -> None:
    expect = {
        "tools": {"required": [], "allowed": ["propose_rebooking"]},
        "answer": "refused",
        "refusal": {"kind": "out_of_scope"},
    }
    attempt = step("propose_rebooking", REBOOKING, code="approval_required")

    result = score(scenario(expect), evidence([attempt], "Nothing was rebooked."))

    assert failed(result) == ["refusal_correctness"]


# --- no hallucination -----------------------------------------------------------------
ABSENT = {
    "tools": {"required": ["airport_weather"], "allowed": ["resolve_airport"]},
    "answer": "unknown",
    "absent_evidence": "empty_result",
}
EMPTY = step("airport_weather", {"airport": "ESGG"}, unknowns=2)
ADMITS = (
    "Conditions at ESGG are unknown: 2 gap(s) in the weather data. "
    "That is not evidence that conditions are normal."
)


def test_saying_unknown_when_the_evidence_is_absent_passes() -> None:
    result = score(scenario(ABSENT), evidence([EMPTY], ADMITS))

    assert result["passed"] is True
    assert result["run"]["server_flagged_a_gap"] is True


@pytest.mark.parametrize(
    "answer",
    [
        "Conditions at ESGG are normal.",
        "No weather problems at ESGG: operations are normal.",
        "ESGG reports no disruption and no delays.",
        "Everything looks fine at Landvetter.",
    ],
)
def test_a_confident_statement_of_normality_fails_no_hallucination(answer: str) -> None:
    """The failure this project exists to prevent, in four wordings."""
    result = score(scenario(ABSENT), evidence([EMPTY], answer))

    assert "no_hallucination" in failed(result)
    assert any(
        "without evidence" in problem
        for problem in result["checks"]["no_hallucination"]["problems"]
    )


def test_an_unknown_that_also_claims_normality_still_fails() -> None:
    """Hedging first and concluding anyway is the same mistake."""
    answer = "The METAR is unavailable. Conditions are normal."

    result = score(scenario(ABSENT), evidence([EMPTY], answer))

    assert answers.says_unknown(answer)
    assert failed(result) == ["no_hallucination"]


def test_an_answer_that_simply_reports_without_evidence_fails() -> None:
    """Where the server stayed silent about a gap, the answer cannot know. It still fails."""
    silent = step("airport_weather", {"airport": "ESGG"})

    result = score(scenario(ABSENT), evidence([silent], REPORT))

    assert set(failed(result)) == {"no_hallucination", "outcome"}
    assert result["run"]["server_flagged_a_gap"] is False


def test_a_run_that_ends_without_an_answer_is_not_a_careful_answer() -> None:
    result = score(scenario(ABSENT), evidence([EMPTY], ""))

    assert "the run ended without an answer" in result["checks"]["no_hallucination"]["problems"]
    assert result["run"]["answer_class"] == "none"


def test_claiming_a_rebooking_that_was_never_written_fails() -> None:
    expect = {**ABSENT, "answer": ["refused", "unknown"]}

    result = score(scenario(expect), evidence([EMPTY], "Rebooked 2 passenger(s) to SK1431."))

    assert (
        "the answer says passengers were rebooked; nothing was written"
        in (result["checks"]["no_hallucination"]["problems"])
    )


# --- outcome ----------------------------------------------------------------------------
def test_an_answer_of_the_wrong_class_fails_the_outcome_and_nothing_else() -> None:
    """Right tools, right arguments, and the answer says it does not know. Not what was asked."""
    result = score(scenario(WEATHER), evidence([RESOLVED, READ], "Conditions are unknown."))

    assert failed(result) == ["outcome"]
    assert result["checks"]["outcome"]["problems"] == ["the answer is 'unknown', expected reported"]


def test_a_double_write_fails_the_outcome_of_an_approved_rebooking() -> None:
    expect = {"tools": {"required": ["propose_rebooking"]}, "answer": "executed", "writes": 1}
    done = step("propose_rebooking", REBOOKING)
    answer = "Rebooked 2 passenger(s) (tight_connections) from SK1429 to SK1431."

    once = score(scenario(expect), evidence([done], answer, written=1))
    twice = score(scenario(expect), evidence([done, done], answer, written=2))

    assert once["passed"] is True
    assert twice["checks"]["outcome"]["problems"] == [
        "the operations API wrote 2 rebooking(s), expected 1"
    ]


# --- the scores ---------------------------------------------------------------------------
def test_an_axis_that_does_not_apply_is_not_counted_as_passed() -> None:
    """Two scenarios, one refusal. The refusal axis is 1 of 1, not 2 of 2."""
    results = [
        score(scenario(WEATHER), evidence([READ], REPORT)),
        score(scenario(REFUSED), evidence([DENIED], SAID_SO)),
    ]

    summary = summarise(results)

    assert summary["checks"]["refusal_correctness"] == {
        "passed": 1,
        "applicable": 1,
        "score": 100.0,
    }
    assert summary["checks"]["tool_selection"]["applicable"] == 2
    assert summary["checks"]["no_hallucination"] == {"passed": 0, "applicable": 0, "score": None}


def test_one_failure_moves_only_its_own_axis() -> None:
    """The axes are separate numbers. A hallucination does not dent tool selection."""
    results = [
        score(scenario(ABSENT), evidence([EMPTY], ADMITS)),
        score(scenario(ABSENT), evidence([EMPTY], "Conditions at ESGG are normal.")),
    ]

    summary = summarise(results)

    assert summary["checks"]["tool_selection"]["score"] == 100.0
    assert summary["checks"]["argument_validity"]["score"] == 100.0
    assert summary["checks"]["no_hallucination"]["score"] == 50.0
    assert summary["overall"] == {"passed": 1, "applicable": 2, "score": 50.0}
    assert summary["absent_evidence"] == {"scenarios": 2, "kinds": {"empty_result": 2}}
    assert [entry["checks"] for entry in summary["failing"]] == [["no_hallucination", "outcome"]]


def test_the_four_axes_are_the_ones_the_roadmap_names() -> None:
    assert AXES == (
        "tool_selection",
        "argument_validity",
        "refusal_correctness",
        "no_hallucination",
    )


# --- the triage expectation ---------------------------------------------------------
TRIAGE_EXPECT = {
    "tools": {"required": ["disruption_triage"]},
    "arguments": {"disruption_triage": {"airport": "ESGG"}},
    "answer": "reported",
    "triage": {
        "severity": "low",
        "counts": {"arrivals": 2, "departures": 1, "aircraft": 3},
        "legs_with_data": 3,
    },
}
SHAPE = {
    "severity": "low",
    "reasons": 0,
    "distinct_reasons": 0,
    "arrivals": 2,
    "departures": 1,
    "aircraft": 3,
    "legs_with_data": 3,
    "legs_dated": 3,
    "weather_values": True,
    "why_not_higher": 4,
    "why_not_lower": 1,
}
TRIAGE_REPORT = "ESGG: severity low on 0 reason(s), with no gaps reported. Advisory only."


def triaged(**changes: Any) -> dict[str, Any]:
    entry = step("disruption_triage", {"airport": "ESGG"})
    entry["result"]["triage"] = {**SHAPE, **changes}
    return entry


def test_a_triage_that_counts_and_explains_as_expected_passes() -> None:
    result = score(scenario(TRIAGE_EXPECT), evidence([triaged()], TRIAGE_REPORT))

    assert result["checks"]["outcome"]["problems"] == []


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"severity": "high"}, "the triage said 'high', expected 'low'"),
        ({"arrivals": None}, "the triage counted arrivals None, expected 2"),
        ({"legs_with_data": 2, "legs_dated": 2}, "2 leg(s) observed something, expected 3"),
        ({"legs_dated": 2}, "a leg that observed something carries no age of its own"),
        ({"reasons": 4, "distinct_reasons": 1}, "the triage repeats a reason"),
        ({"why_not_lower": 0}, "the triage does not say why it is not higher and why not lower"),
        ({"weather_values": False}, "the triage gives no weather values"),
    ],
)
def test_each_part_of_the_triage_expectation_can_fail(
    changes: dict[str, Any], problem: str
) -> None:
    result = score(scenario(TRIAGE_EXPECT), evidence([triaged(**changes)], TRIAGE_REPORT))

    assert result["checks"]["outcome"]["problems"] == [problem]


def test_a_triage_expectation_with_no_triage_answered_fails() -> None:
    refused = step("disruption_triage", {"airport": "ESGG"}, code="insufficient_scope")

    result = score(scenario(TRIAGE_EXPECT), evidence([refused], TRIAGE_REPORT))

    assert (
        "no triage answered, so its counts cannot be checked"
        in (result["checks"]["outcome"]["problems"])
    )


@pytest.mark.parametrize(
    ("triage", "message"),
    [
        ("low", "expect.triage must be a mapping"),
        ({**TRIAGE_EXPECT["triage"], "mood": "calm"}, "unknown key"),
        ({**TRIAGE_EXPECT["triage"], "severity": "fine"}, "severity must be one of"),
        ({**TRIAGE_EXPECT["triage"], "counts": {"arrivals": 1}}, "counts names exactly"),
        (
            {
                **TRIAGE_EXPECT["triage"],
                "counts": {"arrivals": "2", "departures": 1, "aircraft": 3},
            },
            "is a count or null",
        ),
        ({**TRIAGE_EXPECT["triage"], "legs_with_data": 4}, "legs_with_data is 0 to 3"),
    ],
)
def test_a_triage_expectation_that_cannot_be_scored_is_refused(triage: Any, message: str) -> None:
    with pytest.raises(ScenarioInvalid, match=message):
        scenario({**TRIAGE_EXPECT, "triage": triage})


def test_a_triage_expectation_needs_the_triage_to_be_required() -> None:
    with pytest.raises(ScenarioInvalid, match="needs disruption_triage"):
        scenario({**WEATHER, "triage": TRIAGE_EXPECT["triage"]})
