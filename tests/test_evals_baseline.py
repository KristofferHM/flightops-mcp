"""The baseline gate and the table: what fails a build, what does not, and what is written.

Runs here are built by hand from a list of pass/fail results, so the arithmetic
of "more than two points" is tested on numbers chosen to sit on either side of it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from evals import baseline as baselines
from evals import report
from evals.scoring import CHECKS, summarise


def result(name: str, *, fails: tuple[str, ...] = (), sha: str = "a") -> dict[str, Any]:
    """One scenario's result, with every check applicable and the named ones failing."""
    return {
        "id": name,
        "sha256": sha * 64,
        "absent_evidence": "empty_result" if name.startswith("absent") else None,
        "passed": not fails,
        "checks": {
            check: {"applicable": True, "passed": check not in fails, "problems": []}
            for check in CHECKS
        },
    }


def run_of(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "backend": "offline",
        "model": "offline",
        "recorded_on": "2026-11-17",
        "summary": summarise(results),
        "results": results,
    }


def scenarios(count: int, *, failing: int = 0, check: str = "no_hallucination") -> list[Any]:
    return [
        result(f"absent-{number:03d}", fails=(check,) if number < failing else ())
        for number in range(count)
    ]


# --- regression -------------------------------------------------------------------------------
def test_the_same_run_is_not_a_regression() -> None:
    run = run_of(scenarios(50, failing=3))

    verdict = baselines.compare(baselines.record(run, "2026-11-17"), run)

    assert verdict.failures == []
    assert verdict.notes == []
    assert verdict.improved is False


def test_a_drop_of_more_than_two_points_fails_and_names_the_axis() -> None:
    """50 scenarios: each is 2 points. Two more failures is 4 points down."""
    recorded = baselines.record(run_of(scenarios(50, failing=3)), "2026-11-17")
    worse = run_of(scenarios(50, failing=5))

    verdict = baselines.compare(recorded, worse)

    assert verdict.failures == [
        "no_hallucination: 90.00 is 4.00 points below the baseline's 94.00 "
        "(more than the 2 allowed)",
        "overall: 90.00 is 4.00 points below the baseline's 94.00 (more than the 2 allowed)",
    ]
    assert "absent-003 now fails no_hallucination" in verdict.notes
    assert "absent-004 now fails no_hallucination" in verdict.notes


def test_a_drop_of_exactly_two_points_is_inside_the_limit_and_is_still_said() -> None:
    """The criterion is "more than 2 points". Two points is the boundary, and passes."""
    recorded = baselines.record(run_of(scenarios(50, failing=3)), "2026-11-17")
    slightly_worse = run_of(scenarios(50, failing=4))

    verdict = baselines.compare(recorded, slightly_worse)

    assert verdict.failures == []
    assert (
        "no_hallucination: 92.00 is 2.00 points below the baseline's 94.00, inside the 2 allowed"
        in verdict.notes
    )


def test_one_more_failure_among_forty_is_already_over_the_limit() -> None:
    """At the roadmap's minimum of forty, one scenario is worth more than two points."""
    recorded = baselines.record(run_of(scenarios(40)), "2026-11-17")

    verdict = baselines.compare(recorded, run_of(scenarios(40, failing=1)))

    assert verdict.failures[0].startswith("no_hallucination: 97.50 is 2.50 points below")


def test_one_more_failure_among_fifty_four_is_inside_the_limit_and_two_are_not() -> None:
    """The arithmetic of the committed set, pinned so nobody has to discover it.

    An axis that applies to 54 scenarios moves 1.85 points per scenario. One
    regression there is a note, not a failure. That is the limit as specified,
    and docs/EVALS.md says what it costs.
    """
    recorded = baselines.record(run_of(scenarios(54)), "2026-11-17")

    one = baselines.compare(recorded, run_of(scenarios(54, failing=1, check="outcome")))
    two = baselines.compare(recorded, run_of(scenarios(54, failing=2, check="outcome")))

    assert one.failures == []
    assert "absent-000 now fails outcome" in one.notes
    assert "outcome: 98.15 is 1.85 points below the baseline's 100.00, inside the 2 allowed" in (
        one.notes
    )
    assert [failure.split(":")[0] for failure in two.failures] == ["outcome", "overall"]


def test_only_the_axis_that_dropped_is_reported() -> None:
    recorded = baselines.record(run_of(scenarios(40)), "2026-11-17")
    worse = run_of(scenarios(40, failing=2, check="tool_selection"))

    failures = baselines.compare(recorded, worse).failures

    assert [failure.split(":")[0] for failure in failures] == ["tool_selection", "overall"]


def test_the_limit_is_a_setting_and_zero_means_any_drop() -> None:
    recorded = baselines.record(run_of(scenarios(100)), "2026-11-17")
    one_down = run_of(scenarios(100, failing=1))

    assert baselines.compare(recorded, one_down).failures == []
    assert len(baselines.compare(recorded, one_down, max_regression=0).failures) == 2


# --- improvement ------------------------------------------------------------------------------
def test_a_better_run_passes_and_does_not_move_the_baseline() -> None:
    recorded = baselines.record(run_of(scenarios(50, failing=3)), "2026-11-17")
    before = baselines.dump(recorded)

    verdict = baselines.compare(recorded, run_of(scenarios(50, failing=1)))

    assert verdict.failures == []
    assert verdict.improved is True
    assert "absent-001 now passes no_hallucination" in verdict.notes
    assert verdict.notes[-1].startswith("this run is better than the baseline")
    assert "--update-baseline" in verdict.notes[-1]
    assert baselines.dump(recorded) == before, "comparing does not write"


def test_a_fix_and_a_break_that_cancel_out_are_both_named() -> None:
    """The score is unchanged, so the build passes. The swap is still printed."""
    recorded = baselines.record(
        run_of([result("absent-a", fails=("outcome",)), result("absent-b")]), "2026-11-17"
    )
    swapped = run_of([result("absent-a"), result("absent-b", fails=("outcome",))])

    verdict = baselines.compare(recorded, swapped)

    assert verdict.failures == []
    assert "absent-a now passes outcome" in verdict.notes
    assert "absent-b now fails outcome" in verdict.notes


# --- a different set of scenarios -------------------------------------------------------------
def test_removing_a_failing_scenario_is_not_a_way_to_raise_the_score() -> None:
    recorded = baselines.record(run_of(scenarios(10, failing=2)), "2026-11-17")
    trimmed = run_of(scenarios(10, failing=2)[2:])

    verdict = baselines.compare(recorded, trimmed)

    assert len(verdict.failures) == 1
    assert "removed: absent-000, absent-001" in verdict.failures[0]
    assert "--update-baseline" in verdict.failures[0]


def test_an_added_or_edited_scenario_needs_a_new_baseline() -> None:
    recorded = baselines.record(run_of([result("absent-a"), result("absent-b")]), "2026-11-17")
    changed = run_of([result("absent-a", sha="b"), result("absent-b"), result("absent-c")])

    (failure,) = baselines.compare(recorded, changed).failures

    assert "added: absent-c" in failure
    assert "edited: absent-a" in failure


# --- the file ---------------------------------------------------------------------------------
def test_the_baseline_file_is_the_same_bytes_every_time(tmp_path: Any) -> None:
    run = run_of(scenarios(5, failing=1))
    path = tmp_path / "baseline.json"

    path.write_bytes(baselines.dump(baselines.record(run, "2026-11-17")))
    loaded = baselines.load(path)

    assert baselines.dump(loaded) == path.read_bytes()
    assert path.read_bytes().endswith(b"}\n")
    assert b"\r" not in path.read_bytes()
    assert loaded["recorded_on"] == "2026-11-17"
    assert loaded["scenarios"]["absent-000"]["checks"]["no_hallucination"] is False


@pytest.mark.parametrize(
    "content",
    [None, b"not json", b"[]", b'{"schema": "something/else"}'],
)
def test_a_missing_or_foreign_baseline_is_refused(tmp_path: Any, content: bytes | None) -> None:
    path = tmp_path / "baseline.json"
    if content is not None:
        path.write_bytes(content)

    with pytest.raises(baselines.BaselineInvalid):
        baselines.load(path)


def test_a_baseline_missing_a_field_is_refused(tmp_path: Any) -> None:
    document = baselines.record(run_of(scenarios(2)), "2026-11-17")
    del document["scenarios"]
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(baselines.BaselineInvalid, match="has no scenarios"):
        baselines.load(path)


# --- the table --------------------------------------------------------------------------------
def test_the_table_names_the_model_the_date_and_every_axis_separately() -> None:
    document = baselines.record(run_of(scenarios(40, failing=2)), "2026-11-17")

    table = report.table(document)

    assert "**Model:** `offline` (a scripted policy, not a model)" in table
    assert "**Date:** 2026-11-17" in table
    assert "**Scenarios:** 40" in table
    for label in report.LABELS.values():
        assert f"| {label} |" in table
    assert "| No hallucination | 38 | 40 | 95.00 % |" in table
    assert "| Tool selection | 40 | 40 | 100.00 % |" in table
    assert "| **Scenarios passing every check that applies** | 38 | 40 | 95.00 % |" in table
    assert "**40 scenarios** over 1 kinds: `empty_result` 40." in table
    assert "| `absent-000` | no hallucination |" in table


def test_an_axis_nothing_applies_to_is_not_shown_as_a_hundred_percent() -> None:
    lonely = result("happy-a")
    lonely["checks"]["refusal_correctness"] = {"applicable": False, "passed": None, "problems": []}

    table = report.table(baselines.record(run_of([lonely]), "2026-11-17"))

    assert "| Refusal correctness | 0 | 0 | n/a |" in table
    assert "No scenario fails in this run." in table


def test_a_model_run_is_labelled_with_its_backend() -> None:
    run = {**run_of(scenarios(2)), "backend": "anthropic", "model": "claude-sonnet-5-5"}

    assert "**Model:** `claude-sonnet-5-5` (backend `anthropic`)" in report.table(run)


def test_the_table_is_a_function_of_the_document() -> None:
    document = baselines.record(run_of(scenarios(12, failing=1)), "2026-11-17")

    assert report.table(document) == report.table(json.loads(baselines.dump(document)))


# --- the README block -------------------------------------------------------------------------
README = f"# Title\n\nBefore.\n\n{report.BEGIN}\nold table\n{report.END}\n\nAfter.\n"


def test_the_block_is_replaced_and_nothing_around_it_is_touched() -> None:
    document = baselines.record(run_of(scenarios(4)), "2026-11-17")
    new = report.block(document)

    updated = report.with_block(README, new)

    assert updated.startswith("# Title\n\nBefore.\n\n")
    assert updated.endswith("\n\nAfter.\n")
    assert "old table" not in updated
    assert report.current_block(updated) == new
    assert report.with_block(updated, new) == updated, "writing it twice changes nothing"
    assert report.REGENERATE in new


def test_a_hand_edited_number_is_a_different_block() -> None:
    document = baselines.record(run_of(scenarios(40, failing=2)), "2026-11-17")
    honest = report.with_block(README, report.block(document))

    flattering = honest.replace("95.00 %", "100.00 %")

    assert report.current_block(flattering) != report.block(document)


def test_a_readme_without_markers_is_not_silently_appended_to() -> None:
    assert report.current_block("# No markers here\n") is None
    with pytest.raises(ValueError, match="no <!-- evals:begin -->"):
        report.with_block("# No markers here\n", "anything")
