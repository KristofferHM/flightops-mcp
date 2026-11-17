"""The committed baseline, and what counts as a regression against it.

The baseline is a file in the repository (`evals/baseline.json`): the scores of
one offline run, the result of every check of every scenario, and the digest of
every scenario it was recorded for. A run is compared with it and nothing else.

Three rules:

* **Worse fails, counted two ways.** A check, or the overall figure, more than
  `max_regression` points below the baseline fails the build. Points, not
  percent of percent: 95.0 against a baseline of 97.5 is 2.5 points down. And
  more than `max_regressed_scenarios` scenarios failing a check they passed in
  the baseline fails it too. Both are applied; whichever is stricter decides.
  With 54 scenarios one is worth 1.85 points on the wide axes, so the points
  rule alone would let a single scenario regress unseen. The offline run is
  deterministic, so the scenario limit is 0: any scenario that gets worse fails.
* **Better does not update.** A run that beats the baseline passes and says so.
  The baseline moves only when someone runs `--update-baseline` and commits the
  file, so it cannot ratchet up, or be lowered, by accident.
* **Different scenarios are not comparable.** If a scenario was added, removed
  or edited since the baseline was recorded, the comparison is refused and the
  build fails until a new baseline is recorded. Otherwise deleting a failing
  scenario would be the cheapest way to raise a score.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from .scoring import CHECKS

SCHEMA: Final = "flightops.evals.baseline/v1"
DEFAULT_PATH: Final = Path(__file__).parent / "baseline.json"
MAX_REGRESSION: Final = 2.0
MAX_REGRESSED_SCENARIOS: Final = 0


class BaselineInvalid(Exception):
    """A baseline file that is missing, unreadable or not a baseline."""


@dataclass(slots=True)
class Comparison:
    # Each one is a reason the build fails.
    failures: list[str] = field(default_factory=list)
    # Worth saying, and not a failure.
    notes: list[str] = field(default_factory=list)
    improved: bool = False


def record(run: Mapping[str, Any], recorded_on: str) -> dict[str, Any]:
    """The baseline document for `run`."""
    return {
        "schema": SCHEMA,
        "recorded_on": recorded_on,
        "backend": run["backend"],
        "model": run["model"],
        "summary": run["summary"],
        "scenarios": {
            result["id"]: {
                "sha256": result["sha256"],
                "checks": {name: result["checks"][name]["passed"] for name in CHECKS},
            }
            for result in run["results"]
        },
    }


def dump(document: Mapping[str, Any]) -> bytes:
    """The file. Sorted and LF-terminated, so recording it twice changes nothing."""
    return (json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode()


def load(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_bytes())
    except OSError:
        raise BaselineInvalid(f"there is no baseline at {path}") from None
    except ValueError:
        raise BaselineInvalid(f"{path} is not JSON") from None
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise BaselineInvalid(f"{path} is not a {SCHEMA} document")
    for name in ("recorded_on", "backend", "model", "summary", "scenarios"):
        if name not in document:
            raise BaselineInvalid(f"{path} has no {name}")
    return document


def compare(
    baseline: Mapping[str, Any],
    run: Mapping[str, Any],
    *,
    max_regression: float = MAX_REGRESSION,
    max_regressed_scenarios: int = MAX_REGRESSED_SCENARIOS,
) -> Comparison:
    """What `run` is, measured against `baseline`."""
    verdict = Comparison()
    recorded: Mapping[str, Any] = baseline["scenarios"]
    current = {result["id"]: result for result in run["results"]}

    added = sorted(set(current) - set(recorded))
    removed = sorted(set(recorded) - set(current))
    edited = sorted(
        name
        for name in set(current) & set(recorded)
        if current[name]["sha256"] != recorded[name]["sha256"]
    )
    if added or removed or edited:
        changes = [
            f"{label}: {', '.join(names)}"
            for label, names in (("added", added), ("removed", removed), ("edited", edited))
            if names
        ]
        verdict.failures.append(
            "the scenarios are not the ones the baseline was recorded for "
            f"({'; '.join(changes)}). Record a new baseline with --update-baseline."
        )

    scores = [(name, name) for name in CHECKS]
    for name, label in [*scores, ("overall", "overall")]:
        before = _score(baseline["summary"], name)
        after = _score(run["summary"], name)
        if before is None or after is None:
            continue
        delta = round(after - before, 2)
        if delta < -max_regression:
            verdict.failures.append(
                f"{label}: {after:.2f} is {-delta:.2f} points below the baseline's {before:.2f} "
                f"(more than the {max_regression:g} allowed)"
            )
        elif delta < 0:
            verdict.notes.append(
                f"{label}: {after:.2f} is {-delta:.2f} points below the baseline's {before:.2f}, "
                f"inside the {max_regression:g} allowed"
            )
        elif delta > 0:
            verdict.improved = True
            verdict.notes.append(f"{label}: {after:.2f}, up {delta:.2f} from {before:.2f}")

    regressed: list[str] = []
    for name in sorted(set(current) & set(recorded)):
        was, now = recorded[name]["checks"], current[name]["checks"]
        broke = [c for c in CHECKS if was.get(c) is True and now[c]["passed"] is False]
        fixed = [c for c in CHECKS if was.get(c) is False and now[c]["passed"] is True]
        if broke:
            regressed.append(name)
            verdict.notes.append(f"{name} now fails {', '.join(broke)}")
        if fixed:
            verdict.improved = True
            verdict.notes.append(f"{name} now passes {', '.join(fixed)}")
    if len(regressed) > max_regressed_scenarios:
        verdict.failures.append(
            f"scenarios: {len(regressed)} now fail a check they passed in the baseline "
            f"({', '.join(regressed)}); more than the {max_regressed_scenarios} allowed"
        )

    if verdict.improved and not verdict.failures:
        verdict.notes.append(
            "this run is better than the baseline. The baseline does not move by itself: "
            "record it with --update-baseline and commit the file."
        )
    return verdict


def _score(summary: Mapping[str, Any], name: str) -> float | None:
    tally = summary["overall"] if name == "overall" else summary["checks"].get(name)
    value = tally.get("score") if isinstance(tally, dict) else None
    return float(value) if isinstance(value, int | float) else None
