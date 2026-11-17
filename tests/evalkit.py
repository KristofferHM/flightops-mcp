"""Helpers for the evaluation harness tests: scenarios and transcripts built by hand."""

from __future__ import annotations

from typing import Any

from agent.provider import ToolSpec
from agent.transcript import Transcript
from evals.scenarios import EvalScenario, parse
from evals.scoring import Evidence

SCOPES = ["flightops.reference.read", "flightops.weather.read"]

OFFERED = (
    ToolSpec(
        name="resolve_airport",
        description="",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    ),
    ToolSpec(
        name="airport_weather",
        description="",
        input_schema={
            "type": "object",
            "properties": {"airport": {"type": "string"}},
            "required": ["airport"],
        },
    ),
    ToolSpec(
        name="airport_live_traffic",
        description="",
        input_schema={
            "type": "object",
            "properties": {"airport": {"type": "string"}, "radius_nm": {"type": "number"}},
            "required": ["airport"],
        },
    ),
    ToolSpec(
        name="propose_rebooking",
        description="",
        input_schema={
            "type": "object",
            "properties": {
                "from_flight": {"type": "string"},
                "flight_date": {"type": "string"},
                "to_flight": {"type": "string"},
                "cohort": {"type": "string"},
                "reason_code": {"type": "string"},
                "passenger_count": {"type": "integer"},
            },
            "required": ["from_flight", "flight_date", "to_flight", "cohort", "reason_code"],
        },
    ),
)


def scenario(expect: dict[str, Any], **over: Any) -> EvalScenario:
    """A scenario with this expectation. The rest is filler the scorer does not read."""
    group = (
        "absent-evidence"
        if expect.get("absent_evidence")
        else "refusal"
        if expect.get("refusal")
        else "happy-path"
    )
    raw: dict[str, Any] = {
        "id": "made-by-hand",
        "group": group,
        "why": "A scenario built in a test.",
        "question": "What is the weather at ESGG?",
        "user": {"sub": "dispatcher-3", "scopes": SCOPES},
        "clock": {"start": "2026-11-16T09:00:00+00:00"},
        "offline": {"policy": "airport_brief", "params": {"airport": "ESGG"}},
        "expect": expect,
    }
    raw.update(over)
    return parse(raw)


def step(
    tool: str,
    arguments: dict[str, Any],
    *,
    code: str | None = None,
    unknowns: int = 0,
) -> dict[str, Any]:
    """One step of a transcript, before its index is known."""
    return {
        "tool": tool,
        "arguments": arguments,
        "result": {
            "class": "error" if code else "ok",
            "code": code,
            "unknowns": unknowns,
            "replayed": None,
        },
    }


def evidence(steps: list[dict[str, Any]], answer: str, *, written: int = 0) -> Evidence:
    """What a run with these steps and this answer would have left behind."""
    body = {
        "scenario": {"id": "made-by-hand", "sha256": "0" * 64},
        "backend": "offline",
        "model": "offline",
        "started_at": "2026-11-16T09:00:00+00:00",
        "steps": [{"index": index, "turn": 1, **entry} for index, entry in enumerate(steps)],
        "approvals": [],
        "budget": {
            "limits": {"turns": 8, "tool_calls": 12, "model_tokens": 60000, "upstream_credits": 40},
            "spent": {
                "turns": 1,
                "tool_calls": len(steps),
                "input_tokens": 0,
                "output_tokens": 0,
                "upstream_credits": 0,
            },
            "exhausted": None,
        },
        "outcome": {
            "status": "completed",
            "reason": "answered",
            "detail": "",
            "answer": answer,
            "unexecuted_tool_calls": 0,
        },
    }
    return Evidence(
        transcript=Transcript.seal(body),
        ops_api={"rebooking_requests": written, "rebookings_written": written},
        offered=OFFERED,
    )
