"""Golden scenarios: a question, a state of the world, and what a right answer looks like.

One YAML file per scenario. A scenario has three parts that must not know about
each other:

* **the run**: the question, who asks, with which scopes, on which clock. The
  same fields an agent scenario has, and loaded by the agent's own parser.
* **the fixture**: what the upstreams answer (`fixtures.py`).
* **the expectation**: which tools a right answer calls, with which arguments,
  which class the answer falls in, and whether a refusal or an admission of
  ignorance is the point.

The `offline` block names the scripted plan the offline backend follows. It is
not an expectation and the scorer never reads it: a model run ignores it.

Loading is strict. A key this module does not know is an error, because a
misspelt expectation that is silently ignored is an expectation that cannot fail.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

import yaml

from agent.offline import POLICIES
from agent.scenarios import Scenario, ScenarioInvalid, parse_scenario
from agent.transcript import digest
from flightops_mcp.auth import Scopes
from flightops_mcp.auth.scopes import UnknownToolError

SCENARIO_DIR: Final = Path(__file__).parent / "scenarios"

GROUPS: Final = ("happy-path", "refusal", "absent-evidence")
ANSWER_CLASSES: Final = ("reported", "unknown", "refused", "executed")
REFUSAL_KINDS: Final = (
    "missing_scope",
    "missing_credential",
    "out_of_scope",
    "approval",
    "business_rule",
)
# The ways evidence can be absent. The first eight are the ones the roadmap names;
# a set of scenarios that leaves one of them out is refused by the test suite.
REQUIRED_ABSENT_KINDS: Final = (
    "empty_result",
    "stale_data",
    "partial_coverage",
    "rate_limited",
    "malformed_payload",
    "unauthenticated_read",
    "unknown_airport",
    "upstream_error",
)
ABSENT_KINDS: Final = (*REQUIRED_ABSENT_KINDS, "unknown_flight")

_TOP_KEYS: Final = frozenset(
    {
        "id",
        "group",
        "why",
        "question",
        "user",
        "approval",
        "clock",
        "budget",
        "fixture",
        "offline",
        "expect",
    }
)
_EXPECT_KEYS: Final = frozenset(
    {"tools", "forbidden_tools", "arguments", "answer", "absent_evidence", "refusal", "writes"}
)


@dataclass(frozen=True, slots=True)
class Refusal:
    """A refusal the agent must meet, respect and report."""

    kind: str
    # The call the server refuses, and the code it refuses with. Both None for a
    # request that is out of scope: there, the agent is the one that refuses.
    tool: str | None
    code: str | None


@dataclass(frozen=True, slots=True)
class Expectation:
    # Tools that must be called, in this order. Other calls may come between.
    required: tuple[str, ...]
    # Tools a right answer may also use. Documentation: the scorer allows any other call.
    allowed: frozenset[str]
    # Tools that must not be called, whatever else the run did.
    forbidden: frozenset[str]
    # Per tool, arguments that some call of it must carry.
    arguments: Mapping[str, Mapping[str, Any]]
    # The classes of answer that are acceptable (`answers.py`).
    answer: tuple[str, ...]
    # Set when the scenario is about evidence that is not there: which kind.
    absent_evidence: str | None
    refusal: Refusal | None
    # Rebookings the operations API must have written by the end of the run.
    writes: int


@dataclass(frozen=True, slots=True)
class EvalScenario:
    id: str
    group: str
    why: str
    run: Scenario
    fixture: Mapping[str, Any]
    expect: Expectation
    # Of the whole file's content: a transcript and a baseline both say which
    # version of the scenario, fixture and expectation included, they belong to.
    sha256: str

    @property
    def question(self) -> str:
        return self.run.question


def load_all(directory: Path = SCENARIO_DIR) -> list[EvalScenario]:
    """Every scenario in `directory`, sorted by id. Raises `ScenarioInvalid`."""
    paths = sorted(directory.glob("*.yaml"))
    if not paths:
        raise ScenarioInvalid(f"no scenarios in {directory}")
    scenarios = [load(path) for path in paths]
    return sorted(scenarios, key=lambda scenario: scenario.id)


def load(path: Path) -> EvalScenario:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ScenarioInvalid(f"{path.name} is not YAML: {exc}") from None
    try:
        scenario = parse(raw)
    except ScenarioInvalid as exc:
        raise ScenarioInvalid(f"{path.name}: {exc}") from None
    if scenario.id != path.stem:
        raise ScenarioInvalid(f"{path.name}: the id {scenario.id!r} is not the file's name")
    return scenario


def parse(raw: Any) -> EvalScenario:
    if not isinstance(raw, dict):
        raise ScenarioInvalid("a scenario is a mapping")
    _only(raw, _TOP_KEYS, "the scenario")
    for name in ("id", "group", "why", "question", "user", "clock", "offline", "expect"):
        if name not in raw:
            raise ScenarioInvalid(f"{name} is missing")
    if raw["group"] not in GROUPS:
        raise ScenarioInvalid(f"group must be one of {', '.join(GROUPS)}")
    if not isinstance(raw["why"], str) or not raw["why"].strip():
        raise ScenarioInvalid("why must say what the scenario is for")
    fixture = raw.get("fixture") or {}
    if not isinstance(fixture, dict):
        raise ScenarioInvalid("fixture must be a mapping")

    # The agent's own parser reads the run, so an eval scenario cannot drift from
    # what the loop accepts.
    run = parse_scenario(
        {
            "id": raw["id"],
            "question": raw["question"],
            "user": raw["user"],
            "approval": raw.get("approval"),
            "clock": raw["clock"],
            "budget": raw.get("budget", {}),
            "offline": raw["offline"],
        }
    )
    if run.policy not in POLICIES:
        raise ScenarioInvalid(f"offline.policy {run.policy!r} is not an offline policy")
    sha256 = digest(raw)
    expect = _expectation(raw["expect"])
    if (raw["group"] == "absent-evidence") != (expect.absent_evidence is not None):
        raise ScenarioInvalid(
            "the absent-evidence group is exactly the scenarios with expect.absent_evidence"
        )
    if raw["group"] == "refusal" and expect.refusal is None:
        raise ScenarioInvalid("a scenario in the refusal group needs expect.refusal")
    return EvalScenario(
        id=run.id,
        group=raw["group"],
        why=raw["why"].strip(),
        run=replace(run, sha256=sha256),
        fixture=fixture,
        expect=expect,
        sha256=sha256,
    )


def _expectation(raw: Any) -> Expectation:
    if not isinstance(raw, dict):
        raise ScenarioInvalid("expect must be a mapping")
    _only(raw, _EXPECT_KEYS, "expect")
    tools = raw.get("tools")
    if not isinstance(tools, dict):
        raise ScenarioInvalid("expect.tools is missing")
    _only(tools, frozenset({"required", "allowed"}), "expect.tools")
    required = _tool_names(tools.get("required", []), "expect.tools.required")
    allowed = _tool_names(tools.get("allowed", []), "expect.tools.allowed")
    forbidden = _tool_names(raw.get("forbidden_tools", []), "expect.forbidden_tools")
    for label, names in (("required", required), ("allowed", allowed)):
        both = sorted(set(names) & set(forbidden))
        if both:
            raise ScenarioInvalid(f"{', '.join(both)} is both {label} and forbidden")

    arguments = raw.get("arguments") or {}
    if not isinstance(arguments, dict) or not all(isinstance(v, dict) for v in arguments.values()):
        raise ScenarioInvalid("expect.arguments maps a tool to the arguments it must be given")
    for name in arguments:
        if name not in required:
            raise ScenarioInvalid(f"expect.arguments names {name}, which is not a required tool")

    answer = raw.get("answer")
    classes = [answer] if isinstance(answer, str) else answer
    if not isinstance(classes, list) or not classes or set(classes) - set(ANSWER_CLASSES):
        raise ScenarioInvalid(f"expect.answer must be among {', '.join(ANSWER_CLASSES)}")

    absent = raw.get("absent_evidence")
    if absent is not None and absent not in ABSENT_KINDS:
        raise ScenarioInvalid(f"expect.absent_evidence must be one of {', '.join(ABSENT_KINDS)}")
    if absent is not None and "unknown" not in classes and "refused" not in classes:
        raise ScenarioInvalid("an absent-evidence scenario must accept an unknown or a refusal")

    writes = raw.get("writes", 0)
    if not isinstance(writes, int) or isinstance(writes, bool) or writes < 0:
        raise ScenarioInvalid("expect.writes must be a count")
    return Expectation(
        required=required,
        allowed=frozenset(allowed),
        forbidden=frozenset(forbidden),
        arguments=arguments,
        answer=tuple(classes),
        absent_evidence=absent,
        refusal=_refusal(raw.get("refusal"), required),
        writes=writes,
    )


def _refusal(raw: Any, required: tuple[str, ...]) -> Refusal | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ScenarioInvalid("expect.refusal must be a mapping")
    _only(raw, frozenset({"kind", "tool", "code"}), "expect.refusal")
    kind, tool, code = raw.get("kind"), raw.get("tool"), raw.get("code")
    if kind not in REFUSAL_KINDS:
        raise ScenarioInvalid(f"expect.refusal.kind must be one of {', '.join(REFUSAL_KINDS)}")
    if kind == "out_of_scope":
        if tool is not None or code is not None:
            raise ScenarioInvalid("an out_of_scope refusal names no tool: nothing should be called")
        return Refusal(kind=kind, tool=None, code=None)
    if tool not in required or not isinstance(code, str) or not code:
        raise ScenarioInvalid(
            "expect.refusal needs a required tool and the code it is refused with"
        )
    return Refusal(kind=kind, tool=tool, code=code)


def _tool_names(raw: Any, where: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        raise ScenarioInvalid(f"{where} must be a list of tool names")
    for name in raw:
        try:
            Scopes.for_tool(name)
        except UnknownToolError:
            raise ScenarioInvalid(f"{where} names {name!r}, which is not a tool") from None
    return tuple(raw)


def _only(raw: Mapping[str, Any], known: frozenset[str], where: str) -> None:
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ScenarioInvalid(f"{where} has unknown key(s): {', '.join(unknown)}")
