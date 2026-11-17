"""Tool dispatch over MCP, and the approval round trip that belongs to it.

Two rules hold here, whatever plans the run:

* **A tool call ends in data.** A refusal, a protocol error and a dropped
  connection all come back as `{"ok": false, "error": {...}}`, so the loop can
  record what happened and the planner can react to it.
* **The approval round trip is not the planner's.** When the server answers
  `input_required`, this module takes the request to the `Approver` and retries
  with the answer. The planner sees one tool call and its final result. If
  nobody answers, the retry is sent with no approval at all, so that it is the
  server that states the refusal and not this client that invents one.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import httpx2
from mcp import Client
from mcp.client.session import ClientRequestContext
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolResult,
    ElicitRequestFormParams,
    ElicitRequestParams,
    ElicitResult,
    InputRequiredResult,
    TextContent,
)

from .approval import ApprovalDecision, ApprovalRequest, Approver
from .provider import ToolCall, ToolSpec

# The form field that carries the grant back (server.py asks for exactly this).
GRANT_FIELD = "approval_token"


class ToolsUnavailable(Exception):
    """The server could not be asked what tools it has. The run cannot start."""


@dataclass(frozen=True, slots=True)
class ApprovalNote:
    """What happened at the approval gate during one tool call. Never holds the grant."""

    tool: str
    proposal: dict[str, Any]
    action: str
    approver: str | None
    decided_at: float
    grant_presented: bool


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    payload: dict[str, Any]
    approval: ApprovalNote | None = None


class Tools(Protocol):
    async def list_tools(self) -> tuple[ToolSpec, ...]: ...

    async def call(self, call: ToolCall) -> ToolOutcome: ...


class McpTools:
    """The server's tools, over one authenticated MCP connection."""

    def __init__(
        self, client: Client, approver: Approver | None, clock: Callable[[], float]
    ) -> None:
        self._client = client
        self._approver = approver
        self._clock = clock

    @property
    def instructions(self) -> str | None:
        return self._client.instructions

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        try:
            listed = await self._client.list_tools()
        except Exception as exc:
            raise ToolsUnavailable(type(exc).__name__) from exc
        # Sorted: the order tools are offered in is part of what a planner sees,
        # and it must not depend on registration order.
        return tuple(
            ToolSpec(
                name=tool.name,
                description=tool.description or "",
                input_schema=dict(tool.input_schema),
            )
            for tool in sorted(listed.tools, key=lambda tool: tool.name)
        )

    async def call(self, call: ToolCall) -> ToolOutcome:
        try:
            return await self._call(call)
        except MCPError as exc:
            # A refusal at the HTTP layer (the 403 scope challenge) carries the
            # server's own failure payload as the error's data. That is the
            # answer; it is passed on unchanged so the code survives.
            refused = exc.error.data
            if (
                isinstance(refused, dict)
                and refused.get("ok") is False
                and isinstance(refused.get("error"), dict)
            ):
                return ToolOutcome(refused)
            return ToolOutcome(
                _failure("protocol_error", str(exc), "The server rejected the request itself.")
            )
        except Exception as exc:
            # A tool call must not end the run by raising. The class name is
            # enough to act on; the message may quote a header.
            return ToolOutcome(
                _failure(
                    "transport_error",
                    f"The call did not complete ({type(exc).__name__}).",
                    "Report this; do not assume the action did or did not happen.",
                )
            )

    async def _call(self, call: ToolCall) -> ToolOutcome:
        session = self._client.session
        first = await session.call_tool(call.name, call.arguments, allow_input_required=True)
        if not isinstance(first, InputRequiredResult):
            return ToolOutcome(_payload(first))

        request = _approval_request(call.name, first)
        if request is None:
            return ToolOutcome(
                _failure(
                    "unsupported_input_request",
                    f"{call.name} asked for input this agent has no channel for.",
                    "Nothing was done. Report it; do not answer the request yourself.",
                )
            )
        key, asked = request
        decision = await self._approver.decide(asked) if self._approver else None
        grant = decision.grant if decision and decision.action == "accept" else None
        note = ApprovalNote(
            tool=call.name,
            proposal=asked.proposal,
            action=decision.action if decision else "unanswered",
            approver=decision.approver if decision else None,
            decided_at=self._clock(),
            grant_presented=bool(grant),
        )
        second = await session.call_tool(
            call.name,
            call.arguments,
            input_responses=None if decision is None else {key: _answer(decision, grant)},
            request_state=first.request_state,
            allow_input_required=True,
        )
        if isinstance(second, InputRequiredResult):
            # One question, one answer. A server that asks again is not answered again.
            return ToolOutcome(
                _failure(
                    "approval_not_settled",
                    f"{call.name} asked for approval a second time.",
                    "Nothing was done. Report it and stop.",
                ),
                note,
            )
        return ToolOutcome(_payload(second), note)


async def _declares_elicitation(
    context: ClientRequestContext,  # noqa: ARG001 - the SDK's callback protocol fixes the names
    params: ElicitRequestParams,  # noqa: ARG001
) -> ElicitResult:
    """Declares that approval requests can be relayed. Rounds are driven by `McpTools`.

    Never the path an approval takes: `McpTools` asks for the raw `input_required`
    result and answers it itself. If the SDK ever did call this, it cancels.
    """
    return ElicitResult(action="cancel")


@asynccontextmanager
async def connect(
    url: str,
    http_client: httpx2.AsyncClient,
    *,
    approver: Approver | None,
    clock: Callable[[], float],
) -> AsyncIterator[McpTools]:
    """Connect to the server. With no approver, the client says so and is not asked."""
    async with Client(
        streamable_http_client(url, http_client=http_client),
        elicitation_callback=_declares_elicitation if approver is not None else None,
    ) as client:
        yield McpTools(client, approver, clock)


def _answer(decision: ApprovalDecision, grant: str | None) -> ElicitResult:
    if decision.action == "accept":
        # An accept with no grant is sent as it is. The server refuses it, which
        # is the behaviour ADR 0003 exists to guarantee.
        return ElicitResult(action="accept", content={GRANT_FIELD: grant} if grant else None)
    return ElicitResult(action=decision.action)


def _approval_request(tool: str, result: InputRequiredResult) -> tuple[str, ApprovalRequest] | None:
    """The one approval the server asked for, or None if it asked for anything else."""
    requests = result.input_requests or {}
    if len(requests) != 1:
        return None
    ((key, request),) = requests.items()
    params = getattr(request, "params", None)
    if not isinstance(params, ElicitRequestFormParams):
        return None
    properties = params.requested_schema.get("properties")
    if not isinstance(properties, dict) or GRANT_FIELD not in properties:
        return None
    described = properties.get("proposal")
    try:
        proposal = json.loads(described["default"]) if isinstance(described, dict) else None
    except (KeyError, TypeError, ValueError):
        proposal = None
    if not isinstance(proposal, dict) or not proposal:
        return None
    return key, ApprovalRequest(tool=tool, message=params.message, proposal=proposal)


def _payload(result: Any) -> dict[str, Any]:
    """The tool's structured payload. An unreadable result is said to be unreadable."""
    if isinstance(result, CallToolResult):
        structured = result.structured_content
        if isinstance(structured, dict) and structured:
            return structured
        text = " ".join(block.text for block in result.content if isinstance(block, TextContent))
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        if result.is_error:
            return _failure(
                "tool_error", text[:500] or "The tool failed.", "Check the arguments and retry."
            )
    return _failure(
        "unreadable_result",
        "The tool returned nothing this agent can read.",
        "Treat the outcome as unknown; do not infer a result.",
    )


def _failure(code: str, message: str, hint: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message, "hint": hint}}
