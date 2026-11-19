"""Repeated runs: the arithmetic, the table, and the command line.

The model is a mock `/chat/completions` behind the real backend, and it answers
differently from one run to the next. No test here reaches a real model.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent.offline import POLICIES
from agent.openai_backend import OpenAICompatibleProvider
from evals import report
from evals import run as harness
from evals.aggregate import aggregate, spread
from evals.run import main
from evals.scoring import CHECKS
from flightops_mcp.telemetry import configure_logging

from .test_evals_run import SMALL, Sandbox, careless

HONEST = "The weather is unknown: the data is unavailable."
CARELESS = "Conditions are normal."
RATE_LIMITED = 429


@pytest.fixture(autouse=True)
def _logging_back_to_normal() -> Iterator[None]:
    yield
    configure_logging("INFO")


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    box = Sandbox(tmp_path)
    assert box.run("--update-baseline", "--date", "2026-11-17") == 0
    return box


# --- the arithmetic ----------------------------------------------------------------------------
def test_an_even_count_takes_the_mean_of_the_middle_two() -> None:
    assert spread([10.0, 20.0, 40.0, 80.0])["median"] == 30.0


def test_a_single_run_collapses_the_range_to_its_value() -> None:
    assert spread([54.55]) == {"median": 54.55, "min": 54.55, "max": 54.55}


def test_the_median_is_not_the_mean_when_one_run_is_far_away() -> None:
    """The mean of these is 63.33. A collapsed run must not drag the middle down."""
    assert spread([0.0, 90.0, 100.0])["median"] == 90.0


def test_the_order_of_the_runs_does_not_matter() -> None:
    assert spread([68.18, 54.55, 60.0]) == {"median": 60.0, "min": 54.55, "max": 68.18}


def tally(passed: int, applicable: int) -> dict[str, Any]:
    score = round(100.0 * passed / applicable, 2) if applicable else None
    return {"passed": passed, "applicable": applicable, "score": score}


def fake_run(
    outcomes: dict[str, bool],
    *,
    axis: dict[str, Any] | None = None,
    reason: str = "answered",
) -> dict[str, Any]:
    passed = sum(outcomes.values())
    return {
        "summary": {
            "scenarios": len(outcomes),
            "checks": {name: axis or tally(passed, len(outcomes)) for name in CHECKS},
            "overall": tally(passed, len(outcomes)),
        },
        "results": [
            {"id": name, "passed": verdict, "run": {"reason": reason}}
            for name, verdict in outcomes.items()
        ],
    }


def test_a_run_where_an_axis_did_not_apply_is_left_out_not_counted_as_zero() -> None:
    runs = [fake_run({"a": True}, axis=tally(n, m)) for n, m in [(1, 2), (0, 0), (2, 2)]]

    axis = aggregate(runs)["checks"]["no_hallucination"]

    assert axis["score"] == {"median": 75.0, "min": 50.0, "max": 100.0}
    assert axis["runs_scored"] == 2
    assert axis["applicable"] == {"min": 0, "max": 2}


def test_an_axis_that_never_applied_has_no_score() -> None:
    axis = aggregate([fake_run({"a": True}, axis=tally(0, 0))])["checks"]["tool_selection"]

    assert (axis["score"], axis["runs_scored"]) == (None, 0)


def test_scenarios_are_grouped_by_how_often_they_passed() -> None:
    runs = [
        fake_run({"steady": True, "flaky": True, "broken": False}),
        fake_run({"steady": True, "flaky": False, "broken": False}),
    ]

    assert aggregate(runs)["verdicts"] == {
        "every_run": 1,
        "no_run": 1,
        "some_runs": [{"id": "flaky", "passed": 1}],
    }


@pytest.mark.parametrize("reason", ["provider_error", "server_unavailable"])
def test_a_stop_that_is_not_the_models_doing_is_counted(reason: str) -> None:
    runs = [fake_run({"a": True}), fake_run({"a": False, "b": False}, reason=reason)]

    assert aggregate(runs)["not_the_model"] == {
        "scenario_runs": 2,
        "runs_affected": 1,
        "per_run": [0, 2],
    }


def test_a_stop_that_is_the_models_doing_is_not_excused() -> None:
    runs = [fake_run({"a": False}, reason="budget_exhausted")]

    assert aggregate(runs)["not_the_model"]["scenario_runs"] == 0


# --- the table ---------------------------------------------------------------------------------
def document(runs: Sequence[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "model": "some-model",
        "backend": "openai-compatible",
        "recorded_on": "2026-11-18",
        "sampling": {"temperature": 0.0, "seed": None},
        "aggregate": aggregate(runs),
        "runs": list(runs),
        **extra,
    }


def test_the_table_shows_median_and_range_with_n_model_and_date() -> None:
    runs = [fake_run({"a": True, "b": v}) for v in (True, False, False)]

    table = report.repeat_table(document(runs))

    assert "**Model:** `some-model`" in table
    assert "**Date:** 2026-11-18" in table
    assert "**Runs (N):** 3" in table
    assert "| No hallucination | 50.00 % (50.00\N{EN DASH}100.00) | 2 |" in table
    assert "| `b` | 1 of 3 |" in table


def test_one_run_prints_a_collapsed_range() -> None:
    table = report.repeat_table(document([fake_run({"a": True})]))

    assert "| Tool selection | 100.00 % (100.00\N{EN DASH}100.00) | 1 |" in table
    assert "**Runs (N):** 1" in table


def test_the_table_does_not_claim_the_server_honoured_the_settings() -> None:
    pinned = document([fake_run({"a": True})], sampling={"temperature": 0.5, "seed": 7})
    unpinned = document([fake_run({"a": True})], sampling=None)

    assert "temperature 0.5, seed 7. The server may ignore either" in report.repeat_table(pinned)
    assert "**Sampling:** not pinned by this backend." in report.repeat_table(unpinned)


def test_an_axis_scored_in_only_some_runs_says_so() -> None:
    runs = [fake_run({"a": True}, axis=tally(n, m)) for n, m in [(1, 1), (0, 0)]]

    table = report.repeat_table(document(runs))

    assert "100.00 % (100.00\N{EN DASH}100.00), scored in 1 of 2 runs | 0\N{EN DASH}1 |" in table


def test_an_axis_that_never_applied_prints_na() -> None:
    table = report.repeat_table(document([fake_run({"a": True}, axis=tally(0, 0))]))

    assert "| Tool selection | n/a | 0 |" in table


# --- the command line, offline -----------------------------------------------------------------
def test_offline_repeats_agree_and_every_run_is_written_down(
    sandbox: Sandbox, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = sandbox.baseline.read_bytes(), sandbox.readme.read_bytes()
    out, table = tmp_path / "out" / "runs.json", tmp_path / "out" / "table.md"
    transcripts = tmp_path / "out" / "transcripts"
    capsys.readouterr()

    code = sandbox.run(
        "--repeat",
        "2",
        "--date",
        "2026-11-18",
        "--out",
        str(out),
        "--table-out",
        str(table),
        "--transcripts",
        str(transcripts),
    )

    assert code == 0
    written = json.loads(out.read_bytes())
    assert written["schema"] == "flightops.evals.repeat/v1"
    assert (written["repeat"], written["model"], written["sampling"]) == (2, "offline", None)
    assert [run["summary"]["scenarios"] for run in written["runs"]] == [len(SMALL)] * 2
    assert written["aggregate"]["overall"]["score"] == {
        "median": 100.0,
        "min": 100.0,
        "max": 100.0,
    }
    assert "| Tool selection | 100.00 % (100.00\N{EN DASH}100.00) | 3 |" in table.read_text(
        encoding="utf-8"
    )
    assert "OK: no regression against the baseline." in capsys.readouterr().out
    assert (sandbox.baseline.read_bytes(), sandbox.readme.read_bytes()) == before
    for run in ("run-01", "run-02"):
        assert len(list((transcripts / run).glob("*.json"))) == len(SMALL)


def test_what_is_printed_survives_a_console_without_the_dash(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    """A code page without the en dash would crash the print, after the money is spent."""
    capsys.readouterr()

    assert sandbox.run("--repeat", "1") == 0

    out = capsys.readouterr().out
    assert out.isascii()
    assert "| Tool selection | 100.00 % (100.00-100.00) | 3 |" in out


def test_every_offline_repeat_has_to_pass_the_gate(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(POLICIES, "airport_brief", careless)
    capsys.readouterr()

    assert sandbox.run("--repeat", "2") == 1

    out = capsys.readouterr().out
    assert "FAIL: run 1: no_hallucination:" in out
    assert "FAIL: run 2: no_hallucination:" in out


def test_the_time_limit_applies_to_each_run_not_to_their_sum(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = harness._one_run

    def one_second(*args: Any) -> dict[str, Any]:
        return {**real(*args), "duration_s": 1.0}

    monkeypatch.setattr(harness, "_one_run", one_second)

    assert sandbox.run("--repeat", "3", "--max-seconds", "1.5") == 0
    assert sandbox.run("--repeat", "3", "--max-seconds", "0.5") == 1


# --- nothing a repeat does can move the baseline -----------------------------------------------
@pytest.mark.parametrize(
    "arguments",
    [
        ["--offline", "--repeat", "2", "--update-baseline"],
        ["--offline", "--repeat", "1", "--update-baseline"],
        ["--backend", "openai-compatible", "--repeat", "2", "--update-baseline"],
    ],
)
def test_repeat_with_update_baseline_is_refused_and_says_why(
    sandbox: Sandbox, arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    before = sandbox.baseline.read_bytes(), sandbox.readme.read_bytes()
    files = ["--baseline", str(sandbox.baseline), "--readme", str(sandbox.readme)]
    capsys.readouterr()

    assert main([*arguments, *files]) == 2

    assert "--repeat cannot be used with --update-baseline" in capsys.readouterr().err
    assert (sandbox.baseline.read_bytes(), sandbox.readme.read_bytes()) == before


@pytest.mark.parametrize(
    ("backend", "repeat"), [("openai-compatible", None), ("offline", 2), ("anthropic", 1)]
)
def test_the_writer_itself_refuses_anything_but_one_offline_run(
    tmp_path: Path, backend: str, repeat: int | None
) -> None:
    """Not left to the argument checks: this is the only code that writes the two files."""
    baseline, readme = tmp_path / "baseline.json", tmp_path / "README.md"
    args = argparse.Namespace(repeat=repeat, baseline=baseline, readme=readme)

    failures = harness._record({"backend": backend}, args, "")

    assert failures == ["only a single offline run can be recorded as the baseline"]
    assert not baseline.exists()
    assert not readme.exists()


@pytest.mark.parametrize("value", ["0", "-1", "two"])
def test_a_repeat_count_that_is_not_positive_is_refused(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["--offline", "--repeat", value])

    assert stopped.value.code == 2
    assert "at least 1" in capsys.readouterr().err


# --- the command line, against a model that changes its mind -----------------------------------
class ChangingModel:
    """Answers by run. Each scenario here takes one request, so three requests are a run."""

    def __init__(self, *runs: str | int) -> None:
        self.runs = runs
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        answer = self.runs[(len(self.bodies) - 1) // len(SMALL)]
        if isinstance(answer, int):
            return httpx.Response(answer, json={})
        message = {"role": "assistant", "content": answer}
        choice = {"index": 0, "message": message, "finish_reason": "stop"}
        return httpx.Response(200, json={"choices": [choice]})

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def build(_backend: str, _scenario: Any, _env: Any) -> OpenAICompatibleProvider:
            return OpenAICompatibleProvider(
                base_url="http://models.test/v1",
                model="changing-model",
                transport=httpx.MockTransport(self),
            )

        monkeypatch.setattr(harness, "build_provider", build)


def model_run(sandbox: Sandbox, *extra: str) -> int:
    arguments = ["--backend", "openai-compatible", "--scenarios", str(sandbox.scenarios)]
    arguments += ["--baseline", str(sandbox.baseline), "--readme", str(sandbox.readme)]
    return main([*arguments, *extra])


def test_a_model_that_changes_its_mind_shows_up_as_a_range(
    sandbox: Sandbox, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = sandbox.baseline.read_bytes(), sandbox.readme.read_bytes()
    ChangingModel(HONEST, CARELESS, HONEST).install(monkeypatch)
    out, table = tmp_path / "runs.json", tmp_path / "table.md"

    code = model_run(sandbox, "--repeat", "3", "--out", str(out), "--table-out", str(table))

    assert code == 0
    written = json.loads(out.read_bytes())
    scores = [r["summary"]["checks"]["no_hallucination"]["score"] for r in written["runs"]]
    assert scores == [100.0, 0.0, 100.0]
    assert written["aggregate"]["checks"]["no_hallucination"]["score"] == {
        "median": 100.0,
        "min": 0.0,
        "max": 100.0,
    }
    assert written["sampling"] == {"temperature": 0.0, "seed": None}
    rendered = table.read_text(encoding="utf-8")
    assert "| No hallucination | 100.00 % (0.00\N{EN DASH}100.00) | 1 |" in rendered
    assert "**Model:** `changing-model` (backend `openai-compatible`)" in rendered
    assert (sandbox.baseline.read_bytes(), sandbox.readme.read_bytes()) == before


def test_a_rate_limited_run_is_reported_rather_than_read_as_the_model_failing(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ChangingModel(HONEST, RATE_LIMITED).install(monkeypatch)
    capsys.readouterr()

    assert model_run(sandbox, "--repeat", "2") == 0

    out = capsys.readouterr().out
    assert "**Warning:** 3 scenario runs, across 1 of 2 runs, stopped on a provider" in out


def test_a_setting_that_cannot_be_used_stops_the_run_before_any_request(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api = ChangingModel(HONEST)
    api.install(monkeypatch)
    monkeypatch.setenv("FLIGHTOPS_AGENT_OPENAI_SEED", "not-a-number")
    capsys.readouterr()

    assert model_run(sandbox, "--repeat", "2") == 2

    assert "FLIGHTOPS_AGENT_OPENAI_SEED must be an integer" in capsys.readouterr().err
    assert api.bodies == []
