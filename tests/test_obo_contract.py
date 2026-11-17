"""The on-behalf-of exchanger's contract, pinned where mutation testing found it unpinned.

`tests/test_obo.py` proves the flow end to end. This file pins the edges inside it:
which parts of an identity key the cache, which IdP answers are a refusal and which
are an outage, the exact second a cached token stops being served, and that one
user's slow exchange never makes another user wait.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from mcp.server.auth.provider import AccessToken

from flightops_mcp.auth.obo import OboExchanger, obo_exchange_rejected, obo_not_configured
from flightops_mcp.config import Settings
from flightops_mcp.errors import ToolFailure

from .authkit import ISSUER
from .conftest import FakeClock, LogCapture
from .idpkit import OPS_AUDIENCE, PASSENGERS_READ
from .test_obo import INBOUND, TokenEndpoint, exchanger_with, inbound_user, obo_settings


def user(sub: str, *, iss: str = ISSUER, tid: str = "tenant-1") -> AccessToken:
    return AccessToken(
        token=INBOUND, client_id="agent", scopes=[], subject=sub, claims={"iss": iss, "tid": tid}
    )


async def failure_for(settings: Settings, response: httpx.Response) -> ToolFailure:
    exchanger = OboExchanger(
        obo_settings(settings), transport=httpx.MockTransport(lambda _request: response)
    )
    with pytest.raises(ToolFailure) as excinfo:
        await exchanger.token_for(inbound_user("user-1"), audience=OPS_AUDIENCE, scopes=["a"])
    return excinfo.value


# --- the cache key -----------------------------------------------------------
async def test_the_same_subject_from_another_issuer_is_another_identity(
    settings: Settings, clock: FakeClock
) -> None:
    exchanger, endpoint = exchanger_with(settings, clock)
    ours = await exchanger.token_for(user("alice"), audience=OPS_AUDIENCE, scopes=["a"])
    theirs = await exchanger.token_for(
        user("alice", iss="http://another-idp.test"), audience=OPS_AUDIENCE, scopes=["a"]
    )
    assert ours != theirs
    assert len(endpoint.requests) == 2


async def test_scope_order_does_not_make_a_new_identity(
    settings: Settings, clock: FakeClock
) -> None:
    exchanger, endpoint = exchanger_with(settings, clock)
    first = await exchanger.token_for(user("alice"), audience=OPS_AUDIENCE, scopes=["a", "b"])
    again = await exchanger.token_for(user("alice"), audience=OPS_AUDIENCE, scopes=["b", "a"])
    assert first == again
    assert len(endpoint.requests) == 1


async def test_a_token_is_not_served_at_the_instant_it_stops_being_usable(
    settings: Settings, clock: FakeClock
) -> None:
    exchanger, endpoint = exchanger_with(settings, clock)
    alice = user("alice")
    await exchanger.token_for(alice, audience=OPS_AUDIENCE, scopes=["a"])

    clock.advance(300 - 30)  # lifetime minus skew: exactly the end of its usable life
    assert await exchanger.token_for(alice, audience=OPS_AUDIENCE, scopes=["a"]) == "down-2"
    assert exchanger.exchange_count == 2


async def test_a_lifetime_shorter_than_the_skew_is_never_cached(
    settings: Settings, clock: FakeClock
) -> None:
    endpoint = TokenEndpoint(expires_in=10)
    exchanger = OboExchanger(
        obo_settings(settings), transport=httpx.MockTransport(endpoint), clock=clock
    )
    alice = user("alice")
    await exchanger.token_for(alice, audience=OPS_AUDIENCE, scopes=["a"])
    await exchanger.token_for(alice, audience=OPS_AUDIENCE, scopes=["a"])
    assert len(endpoint.requests) == 2


async def test_forgetting_a_token_that_was_never_cached_is_harmless(
    settings: Settings, clock: FakeClock
) -> None:
    exchanger, _ = exchanger_with(settings, clock)
    exchanger.invalidate(user("nobody"), audience=OPS_AUDIENCE, scopes=["a"])


# --- concurrency -------------------------------------------------------------
class GatedEndpoint:
    """Holds the exchange for one subject open until released; answers others at once."""

    def __init__(self, held_subject_token: str) -> None:
        self.held = held_subject_token
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.requests: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        self.requests.append(form["subject_token"])
        if form["subject_token"] == self.held:
            self.started.set()
            await self.release.wait()
        return httpx.Response(
            200, json={"access_token": f"for-{form['subject_token']}", "expires_in": 300}
        )


def token_of(sub: str, token: str) -> AccessToken:
    return AccessToken(
        token=token, client_id="agent", scopes=[], subject=sub, claims={"iss": ISSUER}
    )


async def test_one_users_slow_exchange_does_not_make_another_user_wait(
    settings: Settings, clock: FakeClock
) -> None:
    endpoint = GatedEndpoint("slow-token")
    exchanger = OboExchanger(
        obo_settings(settings), transport=httpx.MockTransport(endpoint), clock=clock
    )
    slow = asyncio.create_task(
        exchanger.token_for(token_of("slow", "slow-token"), audience=OPS_AUDIENCE, scopes=["a"])
    )
    await endpoint.started.wait()

    fast = await asyncio.wait_for(
        exchanger.token_for(token_of("fast", "fast-token"), audience=OPS_AUDIENCE, scopes=["a"]),
        timeout=2,
    )
    assert fast == "for-fast-token"

    endpoint.release.set()
    assert await slow == "for-slow-token"


async def test_pruning_never_drops_a_lock_that_is_held(
    settings: Settings, clock: FakeClock
) -> None:
    """An expired identity being re-exchanged keeps its lock while another user's
    exchange prunes the cache; otherwise a third caller gets a fresh lock and a
    second, concurrent exchange for the same identity."""
    endpoint = GatedEndpoint("held-token")
    exchanger = OboExchanger(
        obo_settings(settings), transport=httpx.MockTransport(endpoint), clock=clock
    )
    held = token_of("held", "held-token")
    endpoint.release.set()
    await exchanger.token_for(held, audience=OPS_AUDIENCE, scopes=["a"])
    endpoint.release.clear()
    endpoint.started.clear()

    clock.advance(600)  # the cached token for `held` is now expired
    refresh = asyncio.create_task(exchanger.token_for(held, audience=OPS_AUDIENCE, scopes=["a"]))
    await endpoint.started.wait()
    await exchanger.token_for(token_of("other", "other-token"), audience=OPS_AUDIENCE, scopes=["a"])
    again = asyncio.create_task(exchanger.token_for(held, audience=OPS_AUDIENCE, scopes=["a"]))
    await asyncio.sleep(0.05)

    endpoint.release.set()
    await asyncio.gather(refresh, again)
    assert endpoint.requests.count("held-token") == 2, "the first exchange, and one refresh"


# --- what the IdP's answer means ---------------------------------------------
@pytest.mark.parametrize("status", [400, 401, 403])
async def test_a_refusal_from_the_idp_is_a_rejection(settings: Settings, status: int) -> None:
    failure = await failure_for(settings, httpx.Response(status, json={"error": "invalid_grant"}))
    assert failure.code == "obo_exchange_rejected"
    assert failure.details == {"idp_error": "invalid_grant"}


@pytest.mark.parametrize("status", [402, 404, 429, 500, 502])
async def test_any_other_error_status_is_an_outage(settings: Settings, status: int) -> None:
    failure = await failure_for(settings, httpx.Response(status))
    assert failure.code == "upstream_unavailable"
    assert failure.details == {
        "upstream": "identity provider",
        "detail": f"HTTP {status} from the token endpoint",
    }


@pytest.mark.parametrize(
    "body", [["invalid_grant"], {"error": 5}, {"error": None}, {"no_error": "x"}, "plain"]
)
async def test_a_refusal_without_a_readable_error_says_unknown(
    settings: Settings, body: Any
) -> None:
    response = (
        httpx.Response(400, text=body) if isinstance(body, str) else httpx.Response(400, json=body)
    )
    failure = await failure_for(settings, response)
    assert failure.details == {"idp_error": "unknown"}


async def test_an_outage_names_the_idp_and_what_went_wrong(settings: Settings) -> None:
    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    exchanger = OboExchanger(obo_settings(settings), transport=httpx.MockTransport(down))
    with pytest.raises(ToolFailure) as excinfo:
        await exchanger.token_for(inbound_user("user-1"), audience=OPS_AUDIENCE, scopes=["a"])
    assert excinfo.value.details == {"upstream": "identity provider", "detail": "ConnectError"}

    unusable = await failure_for(settings, httpx.Response(200, json={"token_type": "Bearer"}))
    assert unusable.details == {
        "upstream": "identity provider",
        "detail": "the exchange response was not usable",
    }


async def test_a_rejection_is_logged_with_what_was_asked_for(
    settings: Settings, logs: LogCapture
) -> None:
    await failure_for(settings, httpx.Response(403, json={"error": "consent_required"}))
    (event,) = logs.events("obo_exchange_rejected")
    assert event["audience"] == OPS_AUDIENCE
    assert event["scope"] == "a"
    assert event["idp_error"] == "consent_required"


async def test_exchanges_are_counted(settings: Settings, clock: FakeClock) -> None:
    exchanger, _ = exchanger_with(settings, clock)
    for name in ("a", "b", "c"):
        await exchanger.token_for(user(name), audience=OPS_AUDIENCE, scopes=[PASSENGERS_READ])
    assert exchanger.exchange_count == 3


async def test_the_exchange_request_says_who_it_is_and_how_long_it_waits(
    settings: Settings, clock: FakeClock
) -> None:
    configured = obo_settings(settings).model_copy(
        update={"user_agent": "contract-test/2", "http_timeout_s": 7.5}
    )
    endpoint = TokenEndpoint()
    exchanger = OboExchanger(configured, transport=httpx.MockTransport(endpoint), clock=clock)
    await exchanger.token_for(user("alice"), audience=OPS_AUDIENCE, scopes=["a"])

    (request,) = endpoint.requests
    assert request.headers["user-agent"] == "contract-test/2"
    assert request.headers["accept"] == "application/json"
    assert request.extensions["timeout"]["read"] == 7.5


# --- configuration -----------------------------------------------------------
@pytest.mark.parametrize("missing", ["auth_token_url", "auth_client_id", "auth_client_secret"])
def test_each_server_credential_is_required(settings: Settings, missing: str) -> None:
    partial = obo_settings(settings).model_copy(update={missing: None})
    assert OboExchanger(partial).configured() is False
    assert OboExchanger(obo_settings(settings)).configured() is True


@pytest.mark.parametrize("failure", [obo_not_configured(), obo_exchange_rejected("x")])
def test_every_refusal_says_what_happened_and_what_to_do(failure: ToolFailure) -> None:
    assert failure.message
    assert failure.hint
    assert failure.to_payload()["error"]["hint"] == failure.hint
