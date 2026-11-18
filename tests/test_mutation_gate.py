"""The mutation gate's verdict. mutmut itself runs only in CI; its judgement is tested here."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import mutation_gate
from scripts.mutation_gate import ceiling_from, judge, main, parse_results, render

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


@pytest.fixture
def mutmut_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def fake(*args: str) -> str:
        calls.append(args)
        return RESULTS if args[0] == "results" else f"diff of {args[1]}\n"

    monkeypatch.setattr(mutation_gate, "_mutmut", fake)
    return calls


def _pyproject(tmp_path: Path, ceiling: int) -> str:
    path = tmp_path / "ceiling.toml"
    path.write_text(f"[tool.mutation-gate]\nceiling = {ceiling}\n", encoding="utf-8")
    return str(path)


def test_the_self_test_reaches_a_verdict_without_reading_a_single_diff(
    tmp_path: Path, mutmut_calls: list[tuple[str, ...]], capsys: pytest.CaptureFixture[str]
) -> None:
    """The CI self-test runs this form, and greps for the line asserted here."""
    assert main(["--pyproject", _pyproject(tmp_path, 2)]) == 1
    assert "FAIL: 3 mutants survived; the ceiling is 2\n" in capsys.readouterr().out
    assert mutmut_calls == [("results", "--all", "true")]


def test_the_gate_passes_at_its_ceiling_without_a_report(
    tmp_path: Path, mutmut_calls: list[tuple[str, ...]]
) -> None:
    assert main(["--pyproject", _pyproject(tmp_path, 3)]) == 0


def test_a_report_holds_the_diff_of_every_survivor(
    tmp_path: Path, mutmut_calls: list[tuple[str, ...]]
) -> None:
    report = tmp_path / "report.md"

    assert main(["--pyproject", _pyproject(tmp_path, 3), "--report", str(report)]) == 0

    text = report.read_text(encoding="utf-8")
    for name in judge(parse_results(RESULTS), ceiling=3).survivors:
        assert f"diff of {name}" in text
    assert len(mutmut_calls) == 4
