"""Run one scenario against the in-process stack. Shared by the CLI and the tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from .approval import Approver
from .clock import LogicalClock
from .localstack import Upstreams, local_stack
from .loop import SYSTEM, run_agent
from .offline import OfflineProvider
from .provider import Provider, ToolSpec
from .scenarios import Scenario
from .tools import ToolsUnavailable
from .transcript import BACKENDS, Transcript


class BackendUnavailable(Exception):
    """A backend that cannot be built from this environment. The message says what is missing."""


@dataclass(frozen=True, slots=True)
class RunResult:
    transcript: Transcript
    # Counted by the mock operations API, not by anything on the agent's side.
    ops_api: dict[str, int]
    # The tools the server offered this user, with the schemas it published.
    # The evaluation harness checks the run's arguments against these.
    offered: tuple[ToolSpec, ...] = ()


def build_provider(backend: str, scenario: Scenario, env: Mapping[str, str]) -> Provider:
    """The planner for `backend`. Only the chosen backend's dependencies are imported."""
    if backend == "offline":
        return OfflineProvider(scenario.policy, scenario.params)
    if backend == "anthropic":
        # Imported here so an offline run needs no model SDK installed.
        from .anthropic_backend import (  # noqa: PLC0415
            DEFAULT_MODEL,
            EFFORTS,
            AnthropicProvider,
            Effort,
        )

        effort = env.get("FLIGHTOPS_AGENT_EFFORT") or None
        if effort is not None and effort not in EFFORTS:
            raise BackendUnavailable(
                f"FLIGHTOPS_AGENT_EFFORT must be one of {', '.join(EFFORTS)}, not {effort!r}"
            )
        return AnthropicProvider(
            env.get("FLIGHTOPS_AGENT_MODEL") or DEFAULT_MODEL, effort=cast("Effort | None", effort)
        )
    if backend == "openai-compatible":
        from .openai_backend import OpenAICompatibleProvider  # noqa: PLC0415

        base_url = env.get("FLIGHTOPS_AGENT_OPENAI_BASE_URL")
        model = env.get("FLIGHTOPS_AGENT_OPENAI_MODEL")
        if not base_url or not model:
            raise BackendUnavailable(
                "the openai-compatible backend needs FLIGHTOPS_AGENT_OPENAI_BASE_URL and "
                "FLIGHTOPS_AGENT_OPENAI_MODEL"
            )
        return OpenAICompatibleProvider(
            base_url=base_url,
            model=model,
            api_key=env.get("FLIGHTOPS_AGENT_OPENAI_API_KEY") or None,
        )
    raise BackendUnavailable(f"unknown backend {backend!r}; have {', '.join(BACKENDS)}")


def run_clock(
    backend: str, scenario: Scenario, wall_clock: Callable[[], float]
) -> Callable[[], float]:
    """The scenario's logical clock offline. A model run happened when it happened."""
    return LogicalClock(scenario.clock_start) if backend == "offline" else wall_clock


async def run_scenario(
    scenario: Scenario,
    provider: Provider,
    *,
    clock: Callable[[], float],
    manifests: dict[str, list[dict[str, Any]]] | None = None,
    approver: Approver | None = None,
    upstreams: Upstreams | None = None,
) -> RunResult:
    async with local_stack(
        scenario, clock=clock, manifests=manifests, approver=approver, upstreams=upstreams
    ) as stack:
        # The server's own instructions go to the planner with the agent's.
        instructions = stack.tools.instructions
        transcript = await run_agent(
            scenario_id=scenario.id,
            scenario_sha256=scenario.sha256,
            question=scenario.question,
            provider=provider,
            tools=stack.tools,
            budget=scenario.budget,
            clock=clock,
            system=f"{SYSTEM}\n{instructions}" if instructions else SYSTEM,
        )
        try:
            offered = await stack.tools.list_tools()
        except ToolsUnavailable:
            offered = ()
        return RunResult(transcript=transcript, ops_api=stack.ops_api_counts(), offered=offered)
