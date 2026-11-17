#!/usr/bin/env python
"""Enforce the mutation-testing ceiling after `mutmut run`, and write the survivor report.

    uv run mutmut run
    uv run python scripts/mutation_gate.py --report mutation-report.md

Reads `mutmut results` and `mutmut show` for every mutant that was not killed, writes
them to a Markdown report with their diffs, and fails when:

* more mutants survived than `[tool.mutation-gate] ceiling` in pyproject.toml allows
  ("no tests" counts as surviving: no test ran the line, so none could fail);
* any mutant was not checked, which means the run was cut short by its time budget,
  so the count is a lower bound and not a pass;
* nothing was mutated at all, which means the configuration is wrong, not the code.

Mutation testing runs in CI on Linux only. mutmut forks per mutant, which Windows
does not have (docs/QUALITY.md).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# Statuses that mean a test failed when the code was broken.
KILLED = frozenset({"killed", "timeout", "caught by type check"})
# Statuses that mean no test failed. `suspicious` is a mutant whose run behaved
# unlike either outcome; it is counted against the ceiling rather than forgiven.
SURVIVING = frozenset({"survived", "no tests", "suspicious"})


@dataclass(frozen=True, slots=True)
class Verdict:
    counts: Counter[str]
    survivors: list[str]
    ceiling: int
    failures: list[str]


def parse_results(text: str) -> dict[str, str]:
    """`mutmut results --all true` lines, `    name: status`, as {name: status}."""
    statuses: dict[str, str] = {}
    for line in text.splitlines():
        name, sep, status = line.strip().rpartition(": ")
        if sep and name and "__mutmut_" in name:
            statuses[name] = status.strip()
    return statuses


def judge(statuses: dict[str, str], ceiling: int) -> Verdict:
    counts = Counter(statuses.values())
    survivors = sorted(name for name, status in statuses.items() if status in SURVIVING)
    failures: list[str] = []
    if not statuses:
        failures.append("no mutants were generated: the configuration is wrong")
    unchecked = sum(count for status, count in counts.items() if status not in KILLED | SURVIVING)
    if unchecked:
        failures.append(
            f"{unchecked} mutants were not checked ({_describe(counts)}): the run did not "
            "finish, so the survivor count is a lower bound"
        )
    if len(survivors) > ceiling:
        failures.append(f"{len(survivors)} mutants survived; the ceiling is {ceiling}")
    return Verdict(counts=counts, survivors=survivors, ceiling=ceiling, failures=failures)


def _describe(counts: Counter[str]) -> str:
    return ", ".join(f"{status}: {count}" for status, count in sorted(counts.items()))


def render(verdict: Verdict, diffs: dict[str, str]) -> str:
    lines = [
        "# Mutation survivors",
        "",
        f"Counts: {_describe(verdict.counts)}.",
        f"Surviving: {len(verdict.survivors)} (ceiling {verdict.ceiling}).",
        "",
    ]
    for name in verdict.survivors:
        lines += [f"## `{name}`", "", "```diff", diffs.get(name, "").rstrip(), "```", ""]
    if verdict.failures:
        lines += ["## Gate", ""] + [f"- FAIL: {failure}" for failure in verdict.failures]
    else:
        lines += ["## Gate", "", "- PASS"]
    return "\n".join(lines) + "\n"


def ceiling_from(pyproject: Path) -> int:
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return int(config["tool"]["mutation-gate"]["ceiling"])


def _mutmut(*args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "mutmut", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    args = parser.parse_args(argv)

    verdict = judge(
        parse_results(_mutmut("results", "--all", "true")), ceiling_from(args.pyproject)
    )
    diffs = {name: _mutmut("show", name) for name in verdict.survivors}
    args.report.write_text(render(verdict, diffs), encoding="utf-8")

    print(f"counts: {_describe(verdict.counts)}")
    print(f"surviving: {len(verdict.survivors)} (ceiling {verdict.ceiling})")
    for name in verdict.survivors:
        print(f"  {name}")
    for failure in verdict.failures:
        print(f"FAIL: {failure}")
    return 1 if verdict.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
