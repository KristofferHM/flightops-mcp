"""Scenarios: what a run is asked, as whom, with what budget, and who answers an approval.

A scenario is a JSON file, so a run is a function of a file. If a plan needs to
vary, it varies here: the offline policy takes its parameters from the scenario
and nothing else. The scenario's digest is written into the transcript, so a
transcript says which version of the question it answers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from .budget import Budget
from .transcript import digest

SCENARIO_DIR: Final = Path(__file__).parent / "scenarios"
APPROVAL_ACTIONS: Final = ("accept", "decline", "cancel", "unanswered")


class ScenarioInvalid(Exception):
    """A scenario file that cannot be run as written."""


@dataclass(frozen=True, slots=True)
class ApprovalScript:
    """How the approval channel answers in this scenario."""

    action: str
    approver: str


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    question: str
    # The signed-in user the agent acts for, and the scopes their token carries.
    user: str
    scopes: tuple[str, ...]
    # None means the agent has no approval channel at all.
    approval: ApprovalScript | None
    budget: Budget
    clock_start: float
    policy: str
    params: Mapping[str, Any]
    sha256: str


def scenario_ids() -> list[str]:
    return sorted(path.stem for path in SCENARIO_DIR.glob("*.json"))


def load_scenario(name: str) -> Scenario:
    """Load a bundled scenario by id, or any scenario by path."""
    path = Path(name) if name.endswith(".json") else SCENARIO_DIR / f"{name}.json"
    if not path.is_file():
        raise ScenarioInvalid(f"no scenario {name!r}; have {', '.join(scenario_ids())}")
    try:
        raw = json.loads(path.read_bytes())
    except ValueError as exc:
        raise ScenarioInvalid(f"{path.name} is not JSON: {exc}") from None
    return parse_scenario(raw)


def parse_scenario(raw: Any) -> Scenario:
    if not isinstance(raw, dict):
        raise ScenarioInvalid("a scenario is a JSON object")
    try:
        user = raw["user"]
        offline = raw["offline"]
        approval = raw.get("approval")
        if approval is not None and approval["action"] not in APPROVAL_ACTIONS:
            raise ScenarioInvalid(f"approval.action must be one of {', '.join(APPROVAL_ACTIONS)}")
        return Scenario(
            id=str(raw["id"]),
            question=str(raw["question"]),
            user=str(user["sub"]),
            scopes=tuple(str(scope) for scope in user["scopes"]),
            approval=None
            if approval is None
            else ApprovalScript(action=approval["action"], approver=str(approval["approver"])),
            budget=Budget(**raw.get("budget", {})),
            clock_start=_instant(raw["clock"]["start"]),
            policy=str(offline["policy"]),
            params=dict(offline.get("params", {})),
            # Of the parsed content, so reformatting the file does not change it.
            sha256=digest(raw),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ScenarioInvalid(f"malformed scenario: {type(exc).__name__}: {exc}") from None


def _instant(text: str) -> float:
    """An ISO instant with an offset. A naive one would mean the machine's time zone."""
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ScenarioInvalid("clock.start needs a UTC offset, e.g. 2026-11-16T09:00:00+00:00")
    return moment.timestamp()
