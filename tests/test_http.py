"""HTTP plumbing: Retry-After parsing, status classification, retry counts."""

from __future__ import annotations

import httpx
import pytest

from flightops_mcp.config import Settings
from flightops_mcp.http import (
    HttpResponse,
    UpstreamHttp,
    credits_remaining,
    parse_http_date,
    retry_after_seconds,
    upstream_time_from_headers,
)

from .conftest import SleepRecorder


def response(
    status: int, headers: dict[str, str] | None = None, json: object | None = None
) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, json=json)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Retry-After": "12"}, 12.0),
        ({"retry-after": "0"}, 0.0),
        ({"X-Rate-Limit-Retry-After-Seconds": "900"}, 900.0),
        ({}, None),
        ({"Retry-After": "not-a-number"}, None),
    ],
)
def test_retry_after_parsing(headers: dict[str, str], expected: float | None) -> None:
    assert retry_after_seconds(response(429, headers)) == expected


def test_retry_after_accepts_an_http_date() -> None:
    # An HTTP-date in the past means "you may retry now", which is 0.0, not None.
    parsed = retry_after_seconds(response(429, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}))
    assert parsed == 0.0


def test_opensky_retry_header_wins_over_the_standard_one() -> None:
    headers = {"Retry-After": "999", "X-Rate-Limit-Retry-After-Seconds": "3"}
    assert retry_after_seconds(response(429, headers)) == 3.0


def test_credit_balance_parsing() -> None:
    assert credits_remaining(response(200, {"X-Rate-Limit-Remaining": "3998"})) == 3998
    assert credits_remaining(response(200, {"X-Rate-Limit-Remaining": "oops"})) is None
    assert credits_remaining(response(200)) is None


async def test_transient_errors_are_retried_then_succeed(
    settings: Settings, sleep: SleepRecorder
) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return response(503)
        return response(200, json={"ok": True})

    http = UpstreamHttp(
        settings,
        "test",
        "https://example.test",
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )
    result = await http.request_json("GET", "/x")

    assert result.status_code == 200
    assert result.json_body == {"ok": True}
    assert len(calls) == 3
    assert len(sleep.calls) == 2, "two backoffs for two failures"


async def test_client_errors_are_not_retried(settings: Settings, sleep: SleepRecorder) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return response(404, json={"error": "nope"})

    http = UpstreamHttp(
        settings,
        "test",
        "https://example.test",
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )
    result = await http.request_json("GET", "/missing")

    assert result.status_code == 404
    assert len(calls) == 1, "a 404 is a fact, not a transient fault"


async def test_429_is_returned_immediately_for_the_caller_to_decide(
    settings: Settings, sleep: SleepRecorder
) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return response(429, {"X-Rate-Limit-Retry-After-Seconds": "600"}, json={"errors": []})

    http = UpstreamHttp(
        settings,
        "test",
        "https://example.test",
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )
    result = await http.request_json("GET", "/states/all")

    assert result.status_code == 429
    assert len(calls) == 1, (
        "the retry policy for 429 belongs to the domain client, not the transport"
    )
    assert sleep.calls == []


async def test_allowed_status_is_a_normal_outcome(settings: Settings, sleep: SleepRecorder) -> None:
    http = UpstreamHttp(
        settings,
        "test",
        "https://example.test",
        transport=httpx.MockTransport(lambda request: response(204)),
        sleep=sleep,
    )
    result = await http.request_json("GET", "/metar", allow_status=frozenset({204}))

    assert result.status_code == 204
    assert result.json_body is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Sat, 14 Nov 2026 23:30:07 GMT", 1794699007),
        ("Sat, 14 Nov 2026 23:30:07", 1794699007),
        ("Sun, 15 Nov 2026 00:30:07 +0100", 1794699007),
        ("", None),
        ("   ", None),
        (None, None),
        ("soon", None),
        ("Thu, 01 Jan 1970 00:00:00 GMT", None),
    ],
)
def test_an_http_date_keeps_its_seconds(raw: str | None, expected: int | None) -> None:
    assert parse_http_date(raw) == expected


def test_the_date_header_is_found_whatever_its_case() -> None:
    stamp = "Sat, 14 Nov 2026 23:30:07 GMT"

    assert upstream_time_from_headers({"date": stamp}) == 1794699007
    assert upstream_time_from_headers({"DATE": stamp}) == 1794699007
    assert upstream_time_from_headers({}) is None
    assert HttpResponse(200, None, {"Date": stamp}).upstream_time_epoch == 1794699007
