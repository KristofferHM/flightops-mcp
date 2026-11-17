"""The provider interface: the one seam between the loop and whatever plans.

A provider is asked for a turn and answers with tool calls or with a final
answer. It sees tool results and nothing else: no access token, no approval
request, no grant. Three backends implement it (`offline`, `anthropic`,
`openai-compatible`) and the loop cannot tell them apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

StopReason = Literal["tool_calls", "answer", "refusal", "truncated"]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Observation:
    """What a tool call came back with, as the planner is shown it."""

    call: ToolCall
    payload: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.payload.get("ok", True) is True

    @property
    def code(self) -> str | None:
        error = self.payload.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        return code if isinstance(code, str) else None


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class Turn:
    """One answer from the planner: tool calls to make, or the final text."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)
    stop: StopReason = "answer"


@dataclass(frozen=True, slots=True)
class Task:
    system: str
    question: str
    tools: tuple[ToolSpec, ...]


class ProviderFailure(Exception):
    """The planner could not produce a turn. `code` is stable; the run stops with it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Provider(Protocol):
    # `name` is the backend, `model` what answered: a model id, or "offline".
    name: str
    model: str

    async def begin(self, task: Task) -> None:
        """Start a run. Called once, before the first turn."""

    async def turn(self, observations: Sequence[Observation]) -> Turn:
        """Plan the first turn (no observations), or reflect on the last tool results."""
