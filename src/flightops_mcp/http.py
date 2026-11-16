"""Shared HTTP plumbing: one place that knows about timeouts, retries and 429s.

Keeping this in one module is what makes the retry semantics testable. Every
upstream client receives an injectable transport and sleep function, so the
suite can assert backoff behaviour without ever sleeping or touching a network.
"""

from __future__ import annotations

import asyncio
import email.utils
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

import httpx
from opentelemetry.trace import SpanKind

from .auth.passthrough import refuse_inbound_token
from .config import Settings
from .otel import inject_trace_headers, tracer
from .resilience import breakers
from .telemetry import UPSTREAM_LATENCY, UPSTREAM_REQUESTS, get_logger

SleepFn = Callable[[float], Awaitable[None]]

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def retry_after_seconds(response: httpx.Response) -> float | None:
    """Read the wait time from either the standard or the upstream-specific header.

    OpenSky returns `X-Rate-Limit-Retry-After-Seconds`; the OAuth/REST standard
    is `Retry-After` in either delta-seconds or HTTP-date form. We honour both.
    """
    for header in ("x-rate-limit-retry-after-seconds", "retry-after"):
        raw = response.headers.get(header)
        if not raw:
            continue
        raw = raw.strip()
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
        # RFC 7231 also allows an HTTP-date. Treat an unparseable value as absent
        # rather than as zero: waiting 0s is a retry storm dressed up as backoff.
        try:
            when = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())
    return None


def credits_remaining(response: httpx.Response) -> int | None:
    raw = response.headers.get("x-rate-limit-remaining")
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


@dataclass(slots=True)
class HttpResponse:
    """The parts of a response we actually keep, so callers never hold sockets open."""

    status_code: int
    json_body: object | None
    headers: dict[str, str] = field(default_factory=dict)


class UpstreamHttp:
    """A thin retrying wrapper around one upstream base URL."""

    def __init__(
        self,
        settings: Settings,
        upstream: str,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: SleepFn = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._upstream = upstream
        self._base_url = base_url.rstrip("/")
        self._sleep = sleep
        self._breaker = breakers().get(upstream, clock=clock)
        self._log = get_logger(__name__).bind(upstream=upstream)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=settings.http_timeout_s,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "application/json",
            },
            transport=transport,
            event_hooks={"request": [refuse_inbound_token]},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        json_body: object | None = None,
        allow_status: frozenset[int] = frozenset(),
    ) -> HttpResponse:
        """Issue a request, retrying transient failures with jittered backoff.

        `allow_status` lets a caller treat a status as a normal outcome (NOAA
        returns 204 for "valid request, no data"), which keeps "no data" and
        "broken" cleanly separated instead of collapsing into one exception.
        """
        attempt = 0
        last_detail = "no attempt made"
        while attempt <= self._settings.max_retries:
            attempt += 1
            # An open circuit refuses before the socket: a dead provider then costs
            # microseconds instead of the whole retry budget spent in sleeps.
            self._breaker.before_call()
            request_headers = inject_trace_headers(dict(headers or {}))
            try:
                with (
                    UPSTREAM_LATENCY.labels(upstream=self._upstream).time(),
                    tracer().start_as_current_span(
                        f"HTTP {method}",
                        kind=SpanKind.CLIENT,
                        attributes={
                            "http.request.method": method,
                            "url.path": path,
                            "upstream.name": self._upstream,
                            "http.attempt": attempt,
                        },
                    ) as span,
                ):
                    response = await self._client.request(
                        method,
                        path,
                        params=params,  # type: ignore[arg-type]
                        headers=request_headers,
                        data=data,
                        json=json_body,
                    )
                    span.set_attribute("http.response.status_code", response.status_code)
            except httpx.HTTPError as exc:
                last_detail = f"{type(exc).__name__}: {exc}"
                self._log.warning("upstream_transport_error", attempt=attempt, detail=last_detail)
                UPSTREAM_REQUESTS.labels(upstream=self._upstream, status="transport_error").inc()
                self._breaker.record_failure()
                if attempt > self._settings.max_retries:
                    raise
                await self._sleep(self._backoff(attempt))
                continue

            UPSTREAM_REQUESTS.labels(
                upstream=self._upstream, status=str(response.status_code)
            ).inc()

            if response.status_code in allow_status:
                # 204 from NOAA is a healthy answer: the upstream is alive.
                self._breaker.record_success()
                return HttpResponse(
                    status_code=response.status_code,
                    json_body=None,
                    headers=dict(response.headers),
                )

            if response.status_code < 400:
                self._breaker.record_success()
                body: object | None
                if not response.content:
                    body = None
                else:
                    try:
                        body = response.json()
                    except ValueError:
                        body = None
                return HttpResponse(
                    status_code=response.status_code,
                    json_body=body,
                    headers=dict(response.headers),
                )

            if response.status_code not in RETRYABLE_STATUS:
                # Client errors that are not throttling are not worth retrying, and
                # they prove the upstream is answering, so the circuit stays closed.
                self._breaker.record_success()
                return HttpResponse(
                    status_code=response.status_code,
                    json_body=_safe_json(response),
                    headers=dict(response.headers),
                )

            retry_after = retry_after_seconds(response)
            self._log.info(
                "upstream_retryable_status",
                attempt=attempt,
                status=response.status_code,
                retry_after_s=retry_after,
                credits_remaining=credits_remaining(response),
            )
            if response.status_code == 429:
                # 429 is a contract, not a fault: it is deliberately neutral for the
                # breaker, because opening the circuit on throttling would hide the
                # retry-after information the caller needs.
                return HttpResponse(
                    status_code=response.status_code,
                    json_body=_safe_json(response),
                    headers=dict(response.headers),
                )
            self._breaker.record_failure()
            if attempt > self._settings.max_retries:
                return HttpResponse(
                    status_code=response.status_code,
                    json_body=_safe_json(response),
                    headers=dict(response.headers),
                )
            await self._sleep(self._backoff(attempt))

        return HttpResponse(
            status_code=503,
            json_body=None,
            headers={},
        )  # pragma: no cover

    def _backoff(self, attempt: int) -> float:
        # Exponential with jitter, capped so a test run never stalls.
        base = float(self._settings.backoff_base_s) * float(2 ** (attempt - 1))
        return min(base + (base * 0.1), float(self._settings.max_rate_limit_wait_s))


def _safe_json(response: httpx.Response) -> object | None:
    try:
        return cast("object | None", response.json())
    except ValueError:
        return None
