"""The table, and the block of the README it is written into.

The table is a function of a results document and nothing else: no clock, no
environment. The README carries the table of the committed baseline between two
markers, and the harness is the only thing that writes between them. A run
checks that what is there is what the baseline renders to, so a hand-edited
number fails the build.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from .scoring import CHECKS

BEGIN: Final = "<!-- evals:begin -->"
END: Final = "<!-- evals:end -->"
REGENERATE: Final = "uv run python -m evals.run --offline --update-baseline"

LABELS: Final = {
    "tool_selection": "Tool selection",
    "argument_validity": "Argument validity",
    "refusal_correctness": "Refusal correctness",
    "no_hallucination": "No hallucination",
    "outcome": "Outcome (answer class, writes)",
}


def table(document: Mapping[str, Any]) -> str:
    """The results table for a baseline (or a run recorded as one), as Markdown."""
    summary = document["summary"]
    model, backend = document["model"], document["backend"]
    what = "a scripted policy, not a model" if backend == "offline" else f"backend `{backend}`"
    lines = [
        # ASCII only: this is printed to a Windows console as well as written to a file.
        f"**Model:** `{model}` ({what}); **Date:** {document['recorded_on']}; "
        f"**Scenarios:** {summary['scenarios']}",
        "",
        "| Axis | Passed | Applies to | Score |",
        "|---|---:|---:|---:|",
    ]
    for name in CHECKS:
        lines.append(_row(LABELS[name], summary["checks"][name]))
    lines.append(_row("**Scenarios passing every check that applies**", summary["overall"]))

    absent = summary["absent_evidence"]
    kinds = ", ".join(f"`{kind}` {count}" for kind, count in absent["kinds"].items())
    lines += [
        "",
        f"Absent-evidence group (answering confidently when the evidence is not there): "
        f"**{absent['scenarios']} scenarios** over {len(absent['kinds'])} kinds: {kinds}.",
    ]

    failing = summary["failing"]
    if failing:
        lines += ["", "Failing in this run, and published as such:", ""]
        lines += ["| Scenario | Fails |", "|---|---|"]
        for entry in failing:
            checks = ", ".join(LABELS[name].lower() for name in entry["checks"])
            lines.append(f"| `{entry['id']}` | {checks} |")
    else:
        lines += ["", "No scenario fails in this run."]
    return "\n".join(lines)


def _row(label: str, tally: Mapping[str, Any]) -> str:
    score = tally["score"]
    shown = "n/a" if score is None else f"{score:.2f} %"
    return f"| {label} | {tally['passed']} | {tally['applicable']} | {shown} |"


def block(document: Mapping[str, Any]) -> str:
    """What stands between the markers in the README, markers included."""
    return "\n".join(
        [
            BEGIN,
            "<!-- Written by the harness from evals/baseline.json. Do not edit: CI fails if "
            "this block and the baseline disagree. -->",
            "",
            table(document),
            "",
            f"Regenerate: `{REGENERATE}`. What these numbers measure, and what they do not: "
            "[`docs/EVALS.md`](docs/EVALS.md).",
            END,
        ]
    )


def current_block(readme: str) -> str | None:
    """The block as it stands in `readme`, or None if the markers are not there."""
    start, end = readme.find(BEGIN), readme.find(END)
    if start < 0 or end < start:
        return None
    return readme[start : end + len(END)]


def with_block(readme: str, new: str) -> str:
    """`readme` with its block replaced by `new`. Raises `ValueError` without markers."""
    old = current_block(readme)
    if old is None:
        raise ValueError(f"the README has no {BEGIN} ... {END} block to write into")
    return readme.replace(old, new, 1)
