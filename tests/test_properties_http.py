"""Properties of the rate-limit arithmetic: Retry-After, the credit header, backoff.

`now` is passed in everywhere, so the HTTP-date form is checked against a fixed
instant rather than against whatever second the suite happens to run in.
"""

from __future__ import annotations

import email.utils
import math
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from flightops_mcp.http import (
    backoff_delay,
    credits_remaining,
    credits_remaining_from_headers,
    parse_credits_remaining,
    parse_retry_after,
    retry_after_from_headers,
    retry_after_seconds,
)

NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)
padding = st.sampled_from(["", " ", "  ", "\t"])
# Instants an upstream might name: whole seconds (HTTP-dates have no fraction),
# from a decade back to a decade ahead of NOW.
instants = st.integers(-10 * 365 * 86400, 10 * 365 * 86400).map(
    lambda offset: NOW + timedelta(seconds=offset)
)


# --- delta-seconds -----------------------------------------------------------
@given(seconds=st.integers(0, 10**12), left=padding, right=padding)
def test_whole_seconds_are_read_as_written(seconds: int, left: str, right: str) -> None:
    assert parse_retry_after(f"{left}{seconds}{right}", now=NOW) == float(seconds)


@given(whole=st.integers(0, 10**6), fraction=st.integers(0, 999))
def test_a_decimal_is_read_as_written(whole: int, fraction: int) -> None:
    raw = f"{whole}.{fraction:03d}"
    assert parse_retry_after(raw, now=NOW) == float(raw)


@given(raw=st.sampled_from(["", " ", "\t", "\n"]))
def test_blank_is_absent_not_zero(raw: str) -> None:
    assert parse_retry_after(raw, now=NOW) is None
    assert parse_retry_after(None, now=NOW) is None


@given(seconds=st.integers(1, 10**9))
def test_a_negative_wait_is_not_a_wait(seconds: int) -> None:
    """`-5` used to become 0 s: retry immediately, which is the storm backoff prevents."""
    assert parse_retry_after(f"-{seconds}", now=NOW) is None


@pytest.mark.parametrize(
    "raw", ["inf", "Infinity", "nan", "NaN", "1e3", "1_000", "+5", "0x10", "5s", "5 s", "١٢"]
)
def test_what_float_would_accept_is_still_not_a_wait(raw: str) -> None:
    """Each of these is a valid Python float (or nearly) and none is RFC delta-seconds."""
    assert parse_retry_after(raw, now=NOW) is None


@given(digits=st.integers(309, 2000))
def test_a_number_too_large_for_a_float_is_absent(digits: int) -> None:
    assert parse_retry_after("9" * digits, now=NOW) is None


@given(raw=st.text(max_size=60))
@example(raw="Wed, 21 Oct 2015 07:28:00 GMT")
@example(raw="Mon, 32 Foo 99999 99:99:99 GMT")
@example(raw="Wed, 21 Oct 2015 07:28:00 +9999")
def test_anything_at_all_is_a_finite_non_negative_wait_or_absent(raw: str) -> None:
    parsed = parse_retry_after(raw, now=NOW)
    assert parsed is None or (math.isfinite(parsed) and parsed >= 0)


# --- HTTP-date ---------------------------------------------------------------
@given(when=instants)
def test_an_http_date_is_the_time_until_it_or_zero(when: datetime) -> None:
    raw = email.utils.format_datetime(when, usegmt=True)
    expected = max(0.0, (when - NOW).total_seconds())
    assert parse_retry_after(raw, now=NOW) == expected


@given(when=instants, later=st.integers(0, 86400))
def test_the_same_date_asks_for_less_as_time_passes(when: datetime, later: int) -> None:
    raw = email.utils.format_datetime(when, usegmt=True)
    sooner = parse_retry_after(raw, now=NOW)
    then = parse_retry_after(raw, now=NOW + timedelta(seconds=later))
    assert sooner is not None
    assert then is not None
    assert then <= sooner


# --- which header ------------------------------------------------------------
header_names = st.sampled_from(
    [
        "X-Rate-Limit-Retry-After-Seconds",
        "x-rate-limit-retry-after-seconds",
        "X-RATE-LIMIT-RETRY-AFTER-SECONDS",
    ]
)
standard_names = st.sampled_from(["Retry-After", "retry-after", "RETRY-AFTER"])


@given(
    opensky=st.integers(0, 10**6),
    standard=st.integers(0, 10**6),
    name=header_names,
    other=standard_names,
)
def test_the_opensky_header_wins_in_any_case(
    opensky: int, standard: int, name: str, other: str
) -> None:
    headers = {name: str(opensky), other: str(standard)}
    assert retry_after_from_headers(headers, now=lambda: NOW) == float(opensky)
    response = httpx.Response(429, headers=headers)
    assert retry_after_seconds(response, now=lambda: NOW) == float(opensky)


@given(junk=st.sampled_from(["", "soon", "-1", "inf"]), standard=st.integers(0, 10**6))
def test_an_unreadable_opensky_header_falls_back_to_the_standard_one(
    junk: str, standard: int
) -> None:
    headers = {"X-Rate-Limit-Retry-After-Seconds": junk, "Retry-After": str(standard)}
    assert retry_after_from_headers(headers, now=lambda: NOW) == float(standard)


@given(names=st.lists(st.sampled_from(["Content-Type", "X-Other", "Date"]), max_size=3))
def test_no_rate_limit_header_is_absent(names: list[str]) -> None:
    assert retry_after_from_headers(dict.fromkeys(names, "5"), now=lambda: NOW) is None


# --- the credit balance ------------------------------------------------------
@given(balance=st.integers(0, 10**9), left=padding, right=padding)
def test_a_credit_balance_is_read_as_written(balance: int, left: str, right: str) -> None:
    raw = f"{left}{balance}{right}"
    assert parse_credits_remaining(raw) == balance
    assert credits_remaining_from_headers({"X-Rate-Limit-Remaining": raw}) == balance
    assert credits_remaining(httpx.Response(200, headers={"x-rate-limit-remaining": raw})) == (
        balance
    )


@given(raw=st.text(max_size=20))
def test_a_credit_balance_is_never_negative(raw: str) -> None:
    parsed = parse_credits_remaining(raw)
    assert parsed is None or parsed >= 0
    if parsed is not None:
        assert raw.strip().isascii()
        assert raw.strip().isdigit()


@pytest.mark.parametrize("raw", ["-1", "1_000", "+3", "3.0", "", " "])
def test_a_balance_python_would_read_is_still_unknown(raw: str) -> None:
    assert parse_credits_remaining(raw) is None


# --- backoff -----------------------------------------------------------------
bases = st.floats(0.0, 10.0, allow_nan=False)
caps = st.floats(0.0, 900.0, allow_nan=False)


@given(attempt=st.integers(1, 10_000), base=bases, cap=caps)
def test_backoff_is_never_negative_and_never_above_the_cap(
    attempt: int, base: float, cap: float
) -> None:
    delay = backoff_delay(attempt, base_s=base, cap_s=cap)
    assert 0.0 <= delay <= cap
    assert math.isfinite(delay)


@given(attempt=st.integers(1, 200), base=bases, cap=caps)
def test_backoff_never_shrinks_from_one_attempt_to_the_next(
    attempt: int, base: float, cap: float
) -> None:
    assert backoff_delay(attempt, base_s=base, cap_s=cap) <= backoff_delay(
        attempt + 1, base_s=base, cap_s=cap
    )


@given(attempt=st.integers(1, 20), base=st.floats(0.001, 1.0, allow_nan=False))
def test_backoff_doubles_until_it_reaches_the_cap(attempt: int, base: float) -> None:
    cap = 1e9
    first = backoff_delay(attempt, base_s=base, cap_s=cap)
    second = backoff_delay(attempt + 1, base_s=base, cap_s=cap)
    assume(second < cap)
    assert second == pytest.approx(2 * first)
    assert first == pytest.approx(base * 2 ** (attempt - 1) * 1.1)


@given(attempt=st.integers(-100, 0))
def test_attempts_are_counted_from_one(attempt: int) -> None:
    with pytest.raises(ValueError, match="counted from 1"):
        backoff_delay(attempt, base_s=1.0, cap_s=5.0)
