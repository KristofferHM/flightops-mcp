"""Several runs of the same scenarios, reduced to a median and an observed range.

Each axis is reduced on its own, so the medians in one table need not come from
the same run. The range is what was observed, not a confidence interval.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from statistics import median
from typing import Any, Final

from .scoring import CHECKS

# Stops that say nothing about the model. They still score as failures, because
# the axes do not change here, so they have to be reported beside the scores.
NOT_THE_MODEL: Final = frozenset({"provider_error", "server_unavailable"})


def spread(values: Sequence[float]) -> dict[str, float]:
    """The median and the range. An even count takes the mean of the middle two."""
    return {
        "median": round(median(values), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
    }


def aggregate(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The distribution over `runs`, each a run document with `summary` and `results`."""
    summaries = [run["summary"] for run in runs]
    return {
        "runs": len(runs),
        "checks": {name: _axis([s["checks"][name] for s in summaries]) for name in CHECKS},
        "overall": _axis([s["overall"] for s in summaries]),
        "verdicts": _verdicts(runs),
        "not_the_model": _not_the_model(runs),
    }


def _axis(tallies: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    # A run in which nothing applied has no score. Counting it as 0 would invent one.
    scores = [tally["score"] for tally in tallies if tally["score"] is not None]
    applicable = [tally["applicable"] for tally in tallies]
    return {
        "score": spread(scores) if scores else None,
        "runs_scored": len(scores),
        # Whether an axis applies can depend on what the run did, so this can move.
        "applicable": {"min": min(applicable), "max": max(applicable)},
    }


def _verdicts(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """How often each scenario passed. The scenarios that changed are named."""
    passes: dict[str, int] = {}
    for run in runs:
        for result in run["results"]:
            passes[result["id"]] = passes.get(result["id"], 0) + int(result["passed"])
    return {
        "every_run": sum(1 for count in passes.values() if count == len(runs)),
        "no_run": sum(1 for count in passes.values() if count == 0),
        "some_runs": [
            {"id": name, "passed": count}
            for name, count in sorted(passes.items())
            if 0 < count < len(runs)
        ],
    }


def _not_the_model(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    per_run = [
        sum(1 for result in run["results"] if result["run"]["reason"] in NOT_THE_MODEL)
        for run in runs
    ]
    return {
        "scenario_runs": sum(per_run),
        "runs_affected": sum(1 for count in per_run if count),
        "per_run": per_run,
    }
