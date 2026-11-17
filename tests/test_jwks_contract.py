"""The JWKS cache's contract, pinned where mutation testing found it unpinned (docs/QUALITY.md).

Each test names the behaviour, not the mutant. The ones that matter most:

* One bad entry in the key set skips that entry, never the keys after it.
* A key is used only if it is an RSA *signature* key for RS256.
* The cache refetches at its TTL and not before, and a forged `kid` can make it
  refetch at most once per refresh interval, measured to the boundary.
* The JWKS client refuses to carry the caller's token. This was asserted only by
  scanning the source text; here it is asserted by making a request.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from flightops_mcp.auth import JwksCache
from flightops_mcp.auth.jwks import _parse_keys
from flightops_mcp.config import Settings
from flightops_mcp.errors import ToolFailure

from .authkit import JWKS_URL, JwksEndpoint, authenticated_as, signing_key
from .conftest import FakeClock, LogCapture

KEY = signing_key("jwks-contract")
SECOND = signing_key("jwks-contract-2")


def good() -> dict[str, Any]:
    return dict(KEY.jwk)


# --- parsing -----------------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        "not an object",
        {"kty": "RSA", "use": "sig", "alg": "RS256"},  # no kid
        {**SECOND.jwk, "kid": ""},
        {**SECOND.jwk, "kid": 7},
        {**SECOND.jwk, "kty": "EC"},
        {**SECOND.jwk, "use": "enc"},
        {**SECOND.jwk, "alg": "RS512"},
        {**SECOND.jwk, "n": "not base64 at all !!"},
    ],
)
def test_a_bad_entry_is_skipped_and_the_keys_after_it_are_kept(bad: Any) -> None:
    keys = _parse_keys({"keys": [bad, good()]})
    assert list(keys) == [KEY.kid]


@pytest.mark.parametrize(
    ("field", "value"),
    [("kty", "EC"), ("kty", "rsa"), ("use", "enc"), ("use", "SIG"), ("alg", "RS512")],
)
def test_only_an_rsa_signature_key_for_rs256_is_trusted(field: str, value: str) -> None:
    assert _parse_keys({"keys": [{**good(), field: value}]}) == {}


@pytest.mark.parametrize("field", ["use", "alg"])
def test_an_entry_that_omits_use_or_alg_is_read_as_a_signature_key_for_rs256(field: str) -> None:
    entry = good()
    del entry[field]
    assert list(_parse_keys({"keys": [entry]})) == [KEY.kid]


@pytest.mark.parametrize(
    "document", [None, [], "keys", {"keys": None}, {"keys": {"a": 1}}, {"other": []}]
)
def test_a_document_that_is_not_a_key_set_has_no_keys(document: Any) -> None:
    assert _parse_keys(document) == {}


# --- timing ------------------------------------------------------------------
async def test_the_default_ttl_is_an_hour_to_the_second(clock: FakeClock) -> None:
    endpoint = JwksEndpoint(KEY)
    cache = JwksCache(JWKS_URL, transport=endpoint.transport, clock=clock)
    assert await cache.key_for(KEY.kid) is not None

    clock.advance(3600 - 0.001)
    await cache.key_for(KEY.kid)
    assert endpoint.calls == 1, "still fresh just inside the hour"

    clock.advance(0.001)
    await cache.key_for(KEY.kid)
    assert endpoint.calls == 2, "stale at exactly one hour"
    assert cache.fetch_count == 2


async def test_a_successful_fetch_makes_the_cache_fresh(clock: FakeClock) -> None:
    """Past the refetch floor but inside the TTL, a known key costs no fetch."""
    endpoint = JwksEndpoint(KEY)
    cache = JwksCache(JWKS_URL, transport=endpoint.transport, clock=clock)
    await cache.key_for(KEY.kid)

    clock.advance(31)
    await cache.key_for(KEY.kid)
    assert endpoint.calls == 1


async def test_an_unknown_kid_refetches_at_most_every_thirty_seconds_by_default(
    clock: FakeClock,
) -> None:
    endpoint = JwksEndpoint(KEY)
    cache = JwksCache(JWKS_URL, transport=endpoint.transport, clock=clock)
    await cache.key_for(KEY.kid)

    clock.advance(30 - 0.001)
    assert await cache.key_for("forged") is None
    assert endpoint.calls == 1, "inside the floor: no refetch"

    clock.advance(0.001)
    assert await cache.key_for("forged") is None
    assert endpoint.calls == 2, "at exactly the floor: one refetch"


# --- what it says ------------------------------------------------------------
async def test_fetch_failures_and_refreshes_are_logged_with_the_url(
    clock: FakeClock, logs: LogCapture
) -> None:
    endpoint = JwksEndpoint(KEY)
    endpoint.failing = True
    cache = JwksCache(JWKS_URL, transport=endpoint.transport, clock=clock)
    await cache.key_for(KEY.kid)

    (failed,) = logs.events("jwks_fetch_failed")
    assert failed["url"] == JWKS_URL
    assert failed["error_class"] == "HTTPStatusError"

    clock.advance(30)
    endpoint.failing = False
    endpoint.keys = []
    endpoint.extra_entries = [{"kty": "EC", "kid": "ec"}]
    await cache.key_for(KEY.kid)
    (empty,) = logs.events("jwks_has_no_usable_keys")
    assert empty["url"] == JWKS_URL

    clock.advance(30)
    endpoint.keys = [KEY, SECOND]
    await cache.key_for(KEY.kid)
    (refreshed,) = logs.events("jwks_refreshed")
    assert refreshed["url"] == JWKS_URL
    assert refreshed["key_ids"] == sorted([KEY.kid, SECOND.kid])


async def test_the_request_says_who_it_is_what_it_wants_and_how_long_it_waits(
    clock: FakeClock, settings: Settings
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"keys": [KEY.jwk]})

    default = JwksCache(JWKS_URL, transport=httpx.MockTransport(handler), clock=clock)
    custom = JwksCache(
        JWKS_URL,
        transport=httpx.MockTransport(handler),
        clock=clock,
        timeout_s=2.5,
        user_agent="contract-test/1",
    )
    await default.key_for(KEY.kid)
    await custom.key_for(KEY.kid)

    first, second = seen
    assert first.headers["user-agent"] == "flightops-mcp"
    assert first.headers["accept"] == "application/json"
    assert first.extensions["timeout"]["read"] == 5.0
    assert second.headers["user-agent"] == "contract-test/1"
    assert second.extensions["timeout"]["read"] == 2.5


# --- the passthrough guard, by behaviour -------------------------------------
async def test_the_jwks_client_refuses_to_carry_the_callers_token(clock: FakeClock) -> None:
    """A JWKS URL that would carry the inbound token is refused before it is sent."""
    endpoint = JwksEndpoint(KEY)
    inbound = "inbound-token-canary-7d3f"
    cache = JwksCache(f"{JWKS_URL}?leak={inbound}", transport=endpoint.transport, clock=clock)

    with authenticated_as(inbound), pytest.raises(ToolFailure) as excinfo:
        await cache.key_for(KEY.kid)

    assert excinfo.value.code == "token_passthrough_blocked"
    assert endpoint.calls == 0
