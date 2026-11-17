"""The `openai-compatible` backend: any server that speaks `/chat/completions`.

For a local model or another vendor: base URL and model come from the
environment. It is plain HTTP on the client this project already depends on,
because "compatible" servers agree on the wire format and on little else.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx

from flightops_mcp.auth import refuse_inbound_token

from .provider import Observation, ProviderFailure, Task, ToolCall, Turn, Usage

TIMEOUT_S = 120.0


class OpenAICompatibleProvider:
    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._transport = transport
        self._tools: list[dict[str, Any]] = []
        self._messages: list[dict[str, Any]] = []

    async def begin(self, task: Task) -> None:
        self._tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in task.tools
        ]
        self._messages = [
            {"role": "system", "content": task.system},
            {"role": "user", "content": task.question},
        ]

    async def turn(self, observations: Sequence[Observation]) -> Turn:
        self._messages.extend(
            {
                "role": "tool",
                "tool_call_id": seen.call.id,
                "content": json.dumps(seen.payload, sort_keys=True),
            }
            for seen in observations
        )
        body = {"model": self.model, "messages": self._messages, "tools": self._tools}
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=TIMEOUT_S,
                headers=self._headers,
                event_hooks={"request": [refuse_inbound_token]},
            ) as client:
                response = await client.post(f"{self._base_url}/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise ProviderFailure(
                "unreachable", f"the model API could not be reached ({type(exc).__name__})"
            ) from exc
        if response.status_code == 429:
            raise ProviderFailure("rate_limited", "the model API rate limit was reached")
        if response.status_code != 200:
            raise ProviderFailure(
                "api_error", f"the model API answered HTTP {response.status_code}"
            )
        try:
            answer = response.json()
            choice = answer["choices"][0]
            message = choice["message"]
            calls = tuple(_tool_call(item) for item in message.get("tool_calls") or [])
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderFailure(
                "malformed_response",
                f"the model API's answer was not usable ({type(exc).__name__})",
            ) from exc

        self._messages.append(message)
        reported = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
        spent = Usage(
            input_tokens=_count(reported.get("prompt_tokens")),
            output_tokens=_count(reported.get("completion_tokens")),
        )
        text = message.get("content") if isinstance(message.get("content"), str) else ""
        if choice.get("finish_reason") == "length":
            return Turn(text=text, usage=spent, stop="truncated")
        if choice.get("finish_reason") == "content_filter":
            return Turn(text=text, usage=spent, stop="refusal")
        return Turn(
            text=text, tool_calls=calls, usage=spent, stop="tool_calls" if calls else "answer"
        )


def _tool_call(item: dict[str, Any]) -> ToolCall:
    """One requested call. Arguments that are not a JSON object are a malformed response."""
    function = item["function"]
    arguments = json.loads(function.get("arguments") or "{}")
    if not isinstance(arguments, dict):
        raise TypeError("tool arguments are not an object")
    return ToolCall(id=str(item["id"]), name=str(function["name"]), arguments=arguments)


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0
