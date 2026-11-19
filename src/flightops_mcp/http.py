"""Shared HTTP plumbing: one place that knows about timeouts, retries and 429s.

Keeping this in one module is what makes the retry semantics testable. Every
upstream client receives an injectable transport and sleep function, so the
suite can assert backoff behaviour without ever sleeping or touching a network.
"""

from __future__ import annotations

import asyncio
import email.utils
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import httpx
from opentelemetry.trace import SpanKind

from .auth.passthrough import refuse_inbound_token
from .config import Settings
from .errors import upstream_auth_failed, upstream_rate_limited, upstream_unavailable
from .otel import inject_trace_headers, tracer
from .resilience import breakers
from .telemetry import UPSTREAM_LATENCY, UPSTREAM_REQUESTS, get_logger

if TYPE_CHECKING:
    from .opensky_token import ClientCredentialsTokenManager

SleepFn = Callable[[float], Awaitable[None]]

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


# Delta-seconds as RFC 9110 writes it, plus a fraction because OpenSky's own header
# is documented as seconds without saying "integer". `float()` alone is too kind: it
# reads "inf", "nan", "1_000" and "-5", none of which is a wait an upstream meant.
_SECONDS = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_DIGITS = re.compile(r"[0-9]+")
# OpenSky's header first: when both are present it is the more specific answer.
RETRY_AFTER_HEADERS = ("x-rate-limit-retry-after-seconds", "retry-after")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def parse_retry_after(raw: str | None, *, now: datetime) -> float | None:
    """One header value as seconds to wait, or None when it does not say.

    `now` is a parameter so the HTTP-date form can be tested without a clock.
    An unreadable value is absent rather than zero: waiting 0 s is a retry storm
    dressed up as backoff. A date in the past is zero: "you may retry now".
    """
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if _SECONDS.fullmatch(raw):
        seconds = float(raw)
        # A thousand digits is a well-formed number and an infinite float.
        return seconds if math.isfinite(seconds) else None
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now).total_seconds())


def retry_after_from_headers(
    headers: Mapping[str, str], *, now: Callable[[], datetime] = _utcnow
) -> float | None:
    """The wait from whichever rate-limit header is present and readable.

    `headers` may be an `httpx.Headers` or the plain dict an `HttpResponse` keeps;
    names are compared without case either way.
    """
    lowered = {name.lower(): value for name, value in headers.items()}
    for header in RETRY_AFTER_HEADERS:
        seconds = parse_retry_after(lowered.get(header), now=now())
        if seconds is not None:
            return seconds
    return None


def retry_after_seconds(
    response: httpx.Response, *, now: Callable[[], datetime] = _utcnow
) -> float | None:
    """Read the wait time from either the standard or the upstream-specific header.

    OpenSky returns `X-Rate-Limit-Retry-After-Seconds`; the OAuth/REST standard
    is `Retry-After` in either delta-seconds or HTTP-date form. We honour both.
    """
    return retry_after_from_headers(response.headers, now=now)


def parse_credits_remaining(raw: str | None) -> int | None:
    """A credit balance is a non-negative integer. Anything else is unknown, not zero."""
    if raw is None:
        return None
    raw = raw.strip()
    return int(raw) if _DIGITS.fullmatch(raw) else None


def credits_remaining_from_headers(headers: Mapping[str, str]) -> int | None:
    lowered = {name.lower(): value for name, value in headers.items()}
    return parse_credits_remaining(lowered.get("x-rate-limit-remaining"))


def credits_remaining(response: httpx.Response) -> int | None:
    return credits_remaining_from_headers(response.headers)


def parse_http_date(raw: str | None) -> int | None:
    """An HTTP `Date` header as epoch seconds, or None when it does not say.

    The header is the producing server's clock, so data ages are measured against it
    rather than against this machine's clock, which may be wrong by minutes. HTTP-date
    carries whole seconds, and they are kept: rounding to the minute would hide skew.
    """
    if raw is None or not raw.strip():
        return None
    try:
        when = email.utils.parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    # RFC 9110 dates are always GMT. A zone-less one is read as GMT too.
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    epoch = int(when.timestamp())
    return epoch if epoch > 0 else None


def upstream_time_from_headers(headers: Mapping[str, str]) -> int | None:
    lowered = {name.lower(): value for name, value in headers.items()}
    return parse_http_date(lowered.get("date"))


@dataclass(slots=True)
class HttpResponse:
    """The parts of a response we actually keep, so callers never hold sockets open."""

    status_code: int
    json_body: object | None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def upstream_time_epoch(self) -> int | None:
        """The upstream's own clock when it answered, from its `Date` header."""
        return upstream_time_from_headers(self.headers)


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
        return backoff_delay(
            attempt,
            base_s=self._settings.backoff_base_s,
            cap_s=self._settings.max_rate_limit_wait_s,
        )


def backoff_delay(attempt: int, *, base_s: float, cap_s: float) -> float:
    """Seconds to wait before retry `attempt` (1-based): doubling, plus 10 %, capped.

    There is no random jitter, so a test can assert the exact sleeps. The cap is
    the same ceiling a `Retry-After` may ask for, so a retry never waits longer
    than an upstream would be allowed to make it wait.
    """
    if attempt < 1:
        raise ValueError(f"attempts are counted from 1, not {attempt}")
    # Capping the exponent first keeps a large attempt from overflowing a float.
    doubling = 2.0 ** min(attempt - 1, 64)
    base = base_s * doubling
    return max(0.0, min(base + base * 0.1, cap_s))


def _safe_json(response: httpx.Response) -> object | None:
    try:
        return cast("object | None", response.json())
    except ValueError:
        return None


async def authorized_get(
    http: UpstreamHttp,
    tokens: ClientCredentialsTokenManager | None,
    path: str,
    params: dict[str, object],
    *,
    upstream: str,
    settings: Settings,
    sleep: Callable[[float], Awaitable[None]],
    empty_status: frozenset[int] = frozenset(),
) -> HttpResponse:
    """GET an upstream endpoint under the one policy every upstream shares.

    `tokens=None` is an anonymous request: no `Authorization` header, and a `401`
    is final because there is no token to replace. The rest of the policy is the
    same, which is why anonymous access is not a second code path.

    * A `401` drops the cached token and tries once more with a fresh one. A second
      `401` is `upstream_auth_failed`: the credentials, not the token, are wrong.
    * A `429` whose wait is within `max_rate_limit_wait_s` is waited out once; a
      longer one, or a second `429`, is `upstream_rate_limited` carrying the wait.
    * A status in `empty_status` is the upstream saying "nothing here" (OpenSky's
      `404` for an empty window) and returns with no body, but with its headers: an
      empty answer still carries the upstream's `Date`. Any other error status is
      `upstream_unavailable`, never an empty result.

    OpenSky's client used to own this. The second adapter is why it moved: the
    rate-limit contract is a governance rule, and a rule that lives in one
    adapter is a rule the next adapter does not follow.
    """
    log = get_logger(__name__).bind(upstream=upstream)
    for auth_attempt in range(2):
        headers: dict[str, str] = {}
        if tokens is not None:
            headers["Authorization"] = f"Bearer {await tokens.token()}"
        response = await http.request_json("GET", path, params=params, headers=headers)

        if response.status_code == 401:
            if tokens is not None and auth_attempt == 0:
                log.info("token_rejected_retrying", path=path)
                tokens.invalidate()
                continue
            raise upstream_auth_failed(401, upstream)

        if response.status_code == 429:
            wait = retry_after_from_headers(response.headers)
            remaining_header = response.headers.get("x-rate-limit-remaining")
            if wait is None or wait > settings.max_rate_limit_wait_s:
                raise upstream_rate_limited(upstream, wait, remaining_header)
            log.info("rate_limited_waiting", wait_s=wait, path=path)
            await sleep(wait)
            response = await http.request_json("GET", path, params=params, headers=headers)
            if response.status_code == 429:
                raise upstream_rate_limited(upstream, wait, remaining_header)

        if response.status_code in empty_status:
            return HttpResponse(response.status_code, None, response.headers)
        if response.status_code >= 400:
            raise upstream_unavailable(upstream, f"HTTP {response.status_code} for {path}")
        return response

    raise upstream_auth_failed(401, upstream)  # pragma: no cover
