"""The `anthropic` backend: Claude plans, through the official SDK.

The loop owns the tool round trip, so this is a manual conversation and not the
SDK's tool runner: the runner would execute tools itself, and tool execution here
has an approval gate and a transcript that belong to the loop.

Two deliberate choices:

* **A refusal is an outcome, not something to route around.** No server-side
  fallback model is configured. The transcript names one model, and an
  evaluation of that model must not silently contain another model's plan.
* **Assistant turns are replayed whole.** `response.content` goes back into the
  history unchanged, thinking blocks included, as the API requires.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Final, Literal, get_args

import anthropic
from anthropic.types import MessageParam, OutputConfigParam, ToolParam, ToolResultBlockParam

from .provider import Observation, ProviderFailure, Task, ToolCall, Turn, Usage

DEFAULT_MODEL: Final = "claude-opus-5-5"
# Room to think and answer, and small enough for a non-streaming request.
MAX_TOKENS: Final = 16_000
Effort = Literal["low", "medium", "high", "xhigh", "max"]
EFFORTS: Final[tuple[str, ...]] = get_args(Effort)


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        client: anthropic.AsyncAnthropic | None = None,
        effort: Effort | None = None,
    ) -> None:
        self.model = model
        # The zero-argument client resolves credentials from the environment or
        # an `ant auth login` profile. No key is read or held by this module.
        self._client = client or anthropic.AsyncAnthropic()
        # Unset leaves the model's own default effort in force.
        self._output: OutputConfigParam | anthropic.Omit = (
            {"effort": effort} if effort else anthropic.omit
        )
        self._system = ""
        self._tools: list[ToolParam] = []
        self._messages: list[MessageParam] = []

    async def begin(self, task: Task) -> None:
        self._system = task.system
        self._tools = [
            {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
            for tool in task.tools
        ]
        self._messages = [{"role": "user", "content": task.question}]

    async def turn(self, observations: Sequence[Observation]) -> Turn:
        if observations:
            # Every result of the previous turn, in one user message.
            results: list[ToolResultBlockParam] = [
                {
                    "type": "tool_result",
                    "tool_use_id": seen.call.id,
                    "content": json.dumps(seen.payload, sort_keys=True),
                    "is_error": not seen.ok,
                }
                for seen in observations
            ]
            self._messages.append({"role": "user", "content": results})
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=self._system,
                tools=self._tools,
                messages=self._messages,
                thinking={"type": "adaptive"},
                # The system prompt and tool list are identical on every turn.
                cache_control={"type": "ephemeral"},
                output_config=self._output,
            )
        except anthropic.RateLimitError as exc:
            raise ProviderFailure("rate_limited", "the model API rate limit was reached") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderFailure(
                "api_error", f"the model API answered HTTP {exc.status_code}"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderFailure("unreachable", "the model API could not be reached") from exc

        self._messages.append({"role": "assistant", "content": response.content})
        usage = response.usage
        spent = Usage(
            # Cached and cache-writing input is input this run caused to be read.
            input_tokens=usage.input_tokens
            + (usage.cache_creation_input_tokens or 0)
            + (usage.cache_read_input_tokens or 0),
            output_tokens=usage.output_tokens,
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        if response.stop_reason == "refusal":
            return Turn(text=text, usage=spent, stop="refusal")
        if response.stop_reason == "max_tokens":
            return Turn(text=text, usage=spent, stop="truncated")
        calls = tuple(
            ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
            for block in response.content
            if block.type == "tool_use"
        )
        return Turn(
            text=text, tool_calls=calls, usage=spent, stop="tool_calls" if calls else "answer"
        )
