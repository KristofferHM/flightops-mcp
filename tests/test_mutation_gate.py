"""The mutation gate's verdict. mutmut itself runs only in CI; its judgement is tested here."""

from __future__ import annotations

from pathlib import Path

from scripts.mutation_gate import ceiling_from, judge, parse_results, render

RESULTS = """
    flightops_mcp.credits.x_CreditLedger__charge__mutmut_1: killed
    flightops_mcp.credits.x_CreditLedger__charge__mutmut_2: survived
    flightops_mcp.auth.scopes.x_Scopes__missing__mutmut_1: no tests
    flightops_mcp.auth.scopes.x_Scopes__missing__mutmut_2: timeout
    flightops_mcp.auth.obo.x__key__mutmut_7: suspicious
noise line without a mutant
"""


def test_results_are_parsed_by_name() -> None:
    statuses = parse_results(RESULTS)
    assert len(statuses) == 5
    assert statuses["flightops_mcp.credits.x_CreditLedger__charge__mutmut_2"] == "survived"


def test_survived_no_tests_and_suspicious_count_against_the_ceiling() -> None:
    verdict = judge(parse_results(RESULTS), ceiling=3)
    assert len(verdict.survivors) == 3
    assert verdict.failures == []


def test_one_over_the_ceiling_fails() -> None:
    verdict = judge(parse_results(RESULTS), ceiling=2)
    assert verdict.failures == ["3 mutants survived; the ceiling is 2"]


def test_an_unfinished_run_is_not_a_pass() -> None:
    """A run cut off by its budget leaves mutants unchecked: fewer survivors, not a better suite."""
    statuses = parse_results(RESULTS + "    flightops_mcp.credits.x_f__mutmut_9: not checked\n")
    verdict = judge(statuses, ceiling=100)
    assert len(verdict.failures) == 1
    assert "not checked" in verdict.failures[0]


def test_no_mutants_at_all_is_a_configuration_error() -> None:
    assert judge({}, ceiling=100).failures == [
        "no mutants were generated: the configuration is wrong"
    ]


def test_the_report_names_every_survivor_with_its_diff() -> None:
    verdict = judge(parse_results(RESULTS), ceiling=0)
    name = "flightops_mcp.credits.x_CreditLedger__charge__mutmut_2"
    report = render(verdict, {name: "-    a > b\n+    a >= b\n"})
    assert f"## `{name}`" in report
    assert "+    a >= b" in report
    assert "FAIL: 3 mutants survived; the ceiling is 0" in report


def test_the_ceiling_is_read_from_pyproject(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.mutation-gate]\nceiling = 7\n", encoding="utf-8")
    assert ceiling_from(pyproject) == 7
