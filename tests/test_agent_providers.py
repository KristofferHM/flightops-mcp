"""The model backends, without a model: what they send, and what they make of the answer.

Each backend is driven through its real HTTP client against a mock transport, so
the request it builds and the response it parses are the real ones. Neither has
been run against a live API here: that needs a credential and spends money, and
is the operator's call.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx
import httpx2
import pytest
from mcp.types import ElicitRequest, ElicitRequestFormParams, InputRequiredResult

from agent.anthropic_backend import DEFAULT_MODEL, AnthropicProvider
from agent.budget import Budget
from agent.loop import SYSTEM, run_agent
from agent.offline import OfflineProvider
from agent.openai_backend import OpenAICompatibleProvider
from agent.provider import Observation, ProviderFailure, Task, ToolCall, ToolSpec
from agent.runner import BackendUnavailable, build_provider, requested_sampling, run_clock
from agent.scenarios import load_scenario
from agent.tools import _approval_request

from .test_agent_loop import ScriptedTools

IMPACT = ToolSpec(
    name="flight_passenger_impact",
    description="Count who a disrupted flight affects.",
    input_schema={
        "type": "object",
        "properties": {"flight": {"type": "string"}},
        "required": ["flight"],
    },
)
TASK = Task(system="Be exact.", question="Who is affected by SK1429?", tools=(IMPACT,))
COUNTS = {"ok": True, "passengers": {"tight_connections": 2}, "unknowns": []}


def seen(call_id: str, payload: dict[str, Any]) -> Observation:
    return Observation(
        call=ToolCall(id=call_id, name="flight_passenger_impact", arguments={"flight": "SK1429"}),
        payload=payload,
    )


# --- anthropic -------------------------------------------------------------------
def claude_message(content: list[dict[str, Any]], stop_reason: str, **usage: int) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": DEFAULT_MODEL,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 20, **usage},
    }


TOOL_USE = {
    "type": "tool_use",
    "id": "toolu_01",
    "name": "flight_passenger_impact",
    "input": {"flight": "SK1429"},
}


class ClaudeApi:
    """The Messages endpoint as a mock transport: records requests, plays back responses."""

    def __init__(self, *responses: dict[str, Any] | httpx2.Response) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.headers: list[httpx2.Headers] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        self.headers.append(request.headers)
        answer = self._responses.pop(0)
        return answer if isinstance(answer, httpx2.Response) else httpx2.Response(200, json=answer)

    def provider(self, **kwargs: Any) -> AnthropicProvider:
        client = anthropic.AsyncAnthropic(
            api_key="test-key-not-a-credential",
            max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self)),
        )
        return AnthropicProvider(client=client, **kwargs)


async def test_claude_is_asked_with_the_tools_and_answers_with_a_tool_call() -> None:
    api = ClaudeApi(claude_message([TOOL_USE], "tool_use", cache_read_input_tokens=50))
    provider = api.provider()
    await provider.begin(TASK)

    turn = await provider.turn([])

    (request,) = api.requests
    assert request["model"] == "claude-opus-5-5"
    assert request["system"] == "Be exact."
    assert request["messages"] == [{"role": "user", "content": "Who is affected by SK1429?"}]
    assert request["tools"] == [
        {
            "name": "flight_passenger_impact",
            "description": IMPACT.description,
            "input_schema": IMPACT.input_schema,
        }
    ]
    assert request["thinking"] == {"type": "adaptive"}
    assert "output_config" not in request, "no effort is sent unless one was asked for"
    assert "fallbacks" not in request, "a refusal is an outcome; no other model is substituted"
    assert turn.stop == "tool_calls"
    assert turn.tool_calls == (
        ToolCall(id="toolu_01", name="flight_passenger_impact", arguments={"flight": "SK1429"}),
    )
    assert (turn.usage.input_tokens, turn.usage.output_tokens) == (150, 20), "cache reads count"


async def test_claude_is_shown_every_tool_result_in_one_message_with_its_turn_replayed() -> None:
    api = ClaudeApi(
        claude_message([{"type": "text", "text": "Checking."}, TOOL_USE], "tool_use"),
        claude_message([{"type": "text", "text": "Two passengers."}], "end_turn"),
    )
    provider = api.provider(effort="low")
    await provider.begin(TASK)
    await provider.turn([])

    turn = await provider.turn([seen("toolu_01", COUNTS)])

    second = api.requests[1]
    assert second["output_config"] == {"effort": "low"}
    assert [message["role"] for message in second["messages"]] == ["user", "assistant", "user"]
    replayed = second["messages"][1]["content"]
    assert [block["type"] for block in replayed] == ["text", "tool_use"]
    assert replayed[1]["id"] == "toolu_01"
    (result,) = second["messages"][2]["content"]
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "toolu_01"
    assert json.loads(result["content"]) == COUNTS
    assert result["is_error"] is False
    assert (turn.stop, turn.text, turn.tool_calls) == ("answer", "Two passengers.", ())


async def test_a_failed_tool_is_reported_to_claude_as_an_error_result() -> None:
    api = ClaudeApi(
        claude_message([TOOL_USE], "tool_use"),
        claude_message([{"type": "text", "text": "It could not be read."}], "end_turn"),
    )
    provider = api.provider()
    await provider.begin(TASK)
    await provider.turn([])
    refused = {
        "ok": False,
        "error": {"code": "approval_declined", "message": "Nothing was rebooked"},
    }

    await provider.turn([seen("toolu_01", refused)])

    (result,) = api.requests[1]["messages"][2]["content"]
    assert result["is_error"] is True
    assert json.loads(result["content"])["error"]["code"] == "approval_declined"


@pytest.mark.parametrize(
    ("stop_reason", "expected"), [("refusal", "refusal"), ("max_tokens", "truncated")]
)
async def test_a_refusal_or_a_cut_off_answer_is_a_stop_not_a_plan(
    stop_reason: str, expected: str
) -> None:
    """Tool calls in a turn that did not end properly are not offered to the loop."""
    api = ClaudeApi(claude_message([{"type": "text", "text": "I"}, TOOL_USE], stop_reason))
    provider = api.provider()
    await provider.begin(TASK)

    turn = await provider.turn([])

    assert turn.stop == expected
    assert turn.tool_calls == ()
    assert turn.text == "I"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (
            httpx2.Response(429, json={"type": "error", "error": {"type": "rate_limit_error"}}),
            "rate_limited",
        ),
        (
            httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error"}}),
            "api_error",
        ),
        (
            httpx2.Response(
                400, json={"type": "error", "error": {"type": "invalid_request_error"}}
            ),
            "api_error",
        ),
    ],
    ids=["rate-limit", "overloaded", "bad-request"],
)
async def test_an_api_failure_is_a_provider_failure_with_a_stable_code(
    response: httpx2.Response, code: str
) -> None:
    provider = ClaudeApi(response).provider()
    await provider.begin(TASK)

    with pytest.raises(ProviderFailure) as excinfo:
        await provider.turn([])

    assert excinfo.value.code == code
    assert "test-key" not in excinfo.value.message


async def test_an_unreachable_api_is_a_provider_failure_too() -> None:
    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    client = anthropic.AsyncAnthropic(
        api_key="test-key-not-a-credential",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(unreachable)),
    )
    provider = AnthropicProvider(client=client)
    await provider.begin(TASK)

    with pytest.raises(ProviderFailure) as excinfo:
        await provider.turn([])

    assert excinfo.value.code == "unreachable"


async def test_a_claude_run_goes_through_the_same_loop_and_its_tokens_are_budgeted() -> None:
    """The loop cannot tell the backends apart: same steps, same transcript, real usage."""
    api = ClaudeApi(
        claude_message([TOOL_USE], "tool_use"),
        claude_message([{"type": "text", "text": "Two passengers."}], "end_turn"),
    )
    tools = ScriptedTools({"flight_passenger_impact": COUNTS})
    ticks = iter(range(100))

    transcript = await run_agent(
        scenario_id="claude-test",
        scenario_sha256="0" * 64,
        question=TASK.question,
        provider=api.provider(),
        tools=tools,
        budget=Budget(),
        clock=lambda: 1_800_000_000.0 + next(ticks),
    )

    assert (transcript.backend, transcript.model) == ("anthropic", "claude-opus-5-5")
    assert transcript.tool_sequence() == ["flight_passenger_impact"]
    assert transcript.steps[0]["arguments"] == {"flight": "SK1429"}
    assert transcript.outcome["answer"] == "Two passengers."
    assert transcript.budget["spent"]["input_tokens"] == 200
    assert transcript.budget["spent"]["output_tokens"] == 40
    assert api.requests[0]["system"] == SYSTEM


# --- openai-compatible -----------------------------------------------------------
def completion(message: dict[str, Any], finish: str = "stop", **usage: int) -> dict[str, Any]:
    return {
        "choices": [
            {"index": 0, "message": {"role": "assistant", **message}, "finish_reason": finish}
        ],
        "usage": {"prompt_tokens": 80, "completion_tokens": 12, **usage},
    }


TOOL_CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "flight_passenger_impact", "arguments": '{"flight": "SK1429"}'},
}


class ChatApi:
    def __init__(self, *responses: dict[str, Any] | httpx.Response) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self._responses.pop(0)
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)

    def provider(self, **kwargs: Any) -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(
            base_url="http://models.test/v1/",
            model="local-model",
            transport=httpx.MockTransport(self),
            **kwargs,
        )


async def test_a_compatible_server_is_asked_the_same_question_in_its_own_format() -> None:
    api = ChatApi(
        completion({"content": None, "tool_calls": [TOOL_CALL]}, "tool_calls"),
        completion({"content": "Two passengers."}),
    )
    provider = api.provider(api_key="test-key-not-a-credential")
    await provider.begin(TASK)

    first = await provider.turn([])
    second = await provider.turn([seen("call_1", COUNTS)])

    assert str(api.requests[0].url) == "http://models.test/v1/chat/completions"
    assert api.requests[0].headers["authorization"] == "Bearer test-key-not-a-credential"
    sent = json.loads(api.requests[0].content)
    assert sent["model"] == "local-model"
    assert sent["messages"] == [
        {"role": "system", "content": "Be exact."},
        {"role": "user", "content": "Who is affected by SK1429?"},
    ]
    assert sent["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "flight_passenger_impact",
                "description": IMPACT.description,
                "parameters": IMPACT.input_schema,
            },
        }
    ]
    assert first.tool_calls == (
        ToolCall(id="call_1", name="flight_passenger_impact", arguments={"flight": "SK1429"}),
    )
    assert (first.usage.input_tokens, first.usage.output_tokens) == (80, 12)
    replayed = json.loads(api.requests[1].content)["messages"]
    assert [message["role"] for message in replayed] == ["system", "user", "assistant", "tool"]
    assert replayed[3]["tool_call_id"] == "call_1"
    assert json.loads(replayed[3]["content"]) == COUNTS
    assert (second.stop, second.text) == ("answer", "Two passengers.")


async def test_a_local_server_needs_no_key_and_gets_no_authorization_header() -> None:
    api = ChatApi(completion({"content": "ok"}))
    provider = api.provider()
    await provider.begin(TASK)

    await provider.turn([])

    assert "authorization" not in api.requests[0].headers


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(429, json={}), "rate_limited"),
        (httpx.Response(500, json={}), "api_error"),
        (httpx.Response(200, text="<html>gateway</html>"), "malformed_response"),
        (httpx.Response(200, json={"choices": []}), "malformed_response"),
        (
            completion(
                {"tool_calls": [{**TOOL_CALL, "function": {"name": "x", "arguments": "{oops"}}]}
            ),
            "malformed_response",
        ),
        (
            completion(
                {"tool_calls": [{**TOOL_CALL, "function": {"name": "x", "arguments": "[1]"}}]}
            ),
            "malformed_response",
        ),
    ],
    ids=[
        "rate-limit",
        "server-error",
        "not-json",
        "no-choices",
        "broken-arguments",
        "list-arguments",
    ],
)
async def test_an_answer_that_cannot_be_used_is_a_provider_failure_not_a_guess(
    response: Any, code: str
) -> None:
    """Half-parsed arguments are never turned into a call. The run stops and says why."""
    provider = ChatApi(response).provider()
    await provider.begin(TASK)

    with pytest.raises(ProviderFailure) as excinfo:
        await provider.turn([])

    assert excinfo.value.code == code


@pytest.mark.parametrize(
    ("finish", "expected"), [("length", "truncated"), ("content_filter", "refusal")]
)
async def test_a_compatible_servers_cut_off_or_filtered_answer_is_a_stop(
    finish: str, expected: str
) -> None:
    provider = ChatApi(completion({"content": "I", "tool_calls": [TOOL_CALL]}, finish)).provider()
    await provider.begin(TASK)

    turn = await provider.turn([])

    assert (turn.stop, turn.tool_calls) == (expected, ())


async def test_an_unreachable_compatible_server_is_a_provider_failure() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    provider = OpenAICompatibleProvider(
        base_url="http://models.test/v1", model="m", transport=httpx.MockTransport(unreachable)
    )
    await provider.begin(TASK)

    with pytest.raises(ProviderFailure) as excinfo:
        await provider.turn([])

    assert excinfo.value.code == "unreachable"


async def test_a_compatible_server_gets_temperature_zero_and_no_seed_by_default() -> None:
    api = ChatApi(completion({"content": "Two passengers."}))
    provider = api.provider()
    await provider.begin(TASK)

    await provider.turn([])

    sent = json.loads(api.requests[0].content)
    assert sent["temperature"] == 0
    assert "seed" not in sent, "a server that rejects the field must still work by default"


async def test_a_compatible_server_gets_the_temperature_and_seed_it_was_given() -> None:
    api = ChatApi(completion({"content": "Two passengers."}))
    provider = api.provider(temperature=0.5, seed=0)
    await provider.begin(TASK)

    await provider.turn([])

    sent = json.loads(api.requests[0].content)
    assert (sent["temperature"], sent["seed"]) == (0.5, 0)


COMPATIBLE = {
    "FLIGHTOPS_AGENT_OPENAI_BASE_URL": "http://localhost:11434/v1",
    "FLIGHTOPS_AGENT_OPENAI_MODEL": "llama",
}


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({}, {"temperature": 0.0, "seed": None}),
        ({"FLIGHTOPS_AGENT_OPENAI_TEMPERATURE": ""}, {"temperature": 0.0, "seed": None}),
        (
            {"FLIGHTOPS_AGENT_OPENAI_TEMPERATURE": "2", "FLIGHTOPS_AGENT_OPENAI_SEED": "-7"},
            {"temperature": 2.0, "seed": -7},
        ),
    ],
)
async def test_the_sampling_settings_come_from_the_environment(
    settings: dict[str, str], expected: dict[str, Any]
) -> None:
    env = {**COMPATIBLE, **settings}
    provider = build_provider("openai-compatible", load_scenario("rebook-approved"), env)
    assert isinstance(provider, OpenAICompatibleProvider)
    api = ChatApi(completion({"content": "Two passengers."}))
    provider._transport = httpx.MockTransport(api)
    await provider.begin(TASK)

    await provider.turn([])

    sent = json.loads(api.requests[0].content)
    assert sent["temperature"] == expected["temperature"]
    assert sent.get("seed") == expected["seed"]
    assert requested_sampling("openai-compatible", env) == expected


@pytest.mark.parametrize("backend", ["offline", "anthropic"])
def test_a_backend_that_pins_nothing_does_not_claim_to(backend: str) -> None:
    assert requested_sampling(backend, COMPATIBLE) is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TEMPERATURE", "warm"),
        ("TEMPERATURE", "-0.1"),
        ("TEMPERATURE", "2.1"),
        ("TEMPERATURE", "nan"),
        ("TEMPERATURE", "inf"),
        ("SEED", "1.5"),
        ("SEED", "lucky"),
    ],
)
def test_a_sampling_setting_that_cannot_be_sent_is_refused(name: str, value: str) -> None:
    variable = f"FLIGHTOPS_AGENT_OPENAI_{name}"

    with pytest.raises(BackendUnavailable, match=variable):
        build_provider(
            "openai-compatible", load_scenario("rebook-approved"), {**COMPATIBLE, variable: value}
        )


# --- choosing a backend ------------------------------------------------------------
def test_each_backend_is_built_from_the_environment_it_documents() -> None:
    scenario = load_scenario("rebook-approved")

    offline = build_provider("offline", scenario, {})
    compatible = build_provider(
        "openai-compatible",
        scenario,
        {
            "FLIGHTOPS_AGENT_OPENAI_BASE_URL": "http://localhost:11434/v1",
            "FLIGHTOPS_AGENT_OPENAI_MODEL": "llama",
        },
    )

    assert isinstance(offline, OfflineProvider)
    assert (offline.name, offline.model) == ("offline", "offline")
    assert (compatible.name, compatible.model) == ("openai-compatible", "llama")


def test_the_anthropic_backend_defaults_to_the_documented_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-a-credential")
    scenario = load_scenario("rebook-approved")

    default = build_provider("anthropic", scenario, {})
    chosen = build_provider(
        "anthropic",
        scenario,
        {"FLIGHTOPS_AGENT_MODEL": "claude-haiku-5-5", "FLIGHTOPS_AGENT_EFFORT": "low"},
    )

    assert (default.name, default.model) == ("anthropic", "claude-opus-5-5")
    assert chosen.model == "claude-haiku-5-5"


@pytest.mark.parametrize(
    ("backend", "env", "problem"),
    [
        ("openai-compatible", {}, "FLIGHTOPS_AGENT_OPENAI_BASE_URL"),
        ("openai-compatible", {"FLIGHTOPS_AGENT_OPENAI_BASE_URL": "http://x"}, "OPENAI_MODEL"),
        (
            "anthropic",
            {"FLIGHTOPS_AGENT_EFFORT": "heroic"},
            "FLIGHTOPS_AGENT_EFFORT must be one of",
        ),
        ("bedrock", {}, "unknown backend"),
    ],
)
def test_a_backend_that_cannot_be_built_says_what_is_missing(
    backend: str, env: dict[str, str], problem: str
) -> None:
    with pytest.raises(BackendUnavailable, match=problem):
        build_provider(backend, load_scenario("rebook-approved"), env)


def test_only_the_offline_backend_gets_the_scenarios_clock() -> None:
    scenario = load_scenario("rebook-approved")

    def wall() -> float:
        return 123.0

    offline_clock = run_clock("offline", scenario, wall)

    assert (offline_clock(), offline_clock()) == (scenario.clock_start, scenario.clock_start + 1.0)
    assert run_clock("anthropic", scenario, wall) is wall


# --- the approval request, read defensively -------------------------------------------
def approval_form(properties: dict[str, Any]) -> InputRequiredResult:
    request = ElicitRequest(
        params=ElicitRequestFormParams(
            message="Approve?", requested_schema={"type": "object", "properties": properties}
        )
    )
    return InputRequiredResult(input_requests={"approval": request}, request_state="sealed")


def test_an_approval_request_is_read_from_the_form_not_from_the_prose() -> None:
    form = approval_form(
        {
            "proposal": {"type": "string", "default": '{"v": 1, "passenger_count": 2}'},
            "approval_token": {"type": "string"},
        }
    )

    found = _approval_request("propose_rebooking", form)

    assert found is not None
    key, request = found
    assert key == "approval"
    assert request.proposal == {"v": 1, "passenger_count": 2}
    assert request.message == "Approve?"


@pytest.mark.parametrize(
    "properties",
    [
        {"approval_token": {"type": "string"}},
        {"proposal": {"type": "string", "default": "not json"}, "approval_token": {}},
        {"proposal": {"type": "string", "default": "[]"}, "approval_token": {}},
        {"proposal": {"type": "string", "default": '{"v": 1}'}},
        {"password": {"type": "string"}},
    ],
    ids=["no-proposal", "unparseable", "not-an-object", "no-grant-field", "something-else"],
)
def test_a_request_that_is_not_an_approval_of_a_stated_proposal_is_not_relayed(
    properties: dict[str, Any],
) -> None:
    """An approver is only ever shown a proposal the server spelled out."""
    assert _approval_request("propose_rebooking", approval_form(properties)) is None
