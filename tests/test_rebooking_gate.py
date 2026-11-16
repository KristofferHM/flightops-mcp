"""`propose_rebooking`: the write that cannot happen without a human.

Three claims, each asserted where it is true rather than where it is convenient:

* **No approval, no write.** Not "the tool returns an error": the operations API
  receives no request at all. Counted on the wire and on the API's own ledger.
* **An approval is a signed grant for one payload.** Saying yes, presenting
  someone else's grant, approving your own proposal, or changing the proposal
  after it was approved all end the same way: nothing written.
* **The same proposal executes once.** A replay, a retry after a lost response
  and a second approval of the same proposal all return the original result,
  and the ledger still holds one write.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from mcp import Client
from mcp.server import request_state
from mcp.shared.exceptions import MCPError
from mcp.types import ElicitResult, InputRequiredResult

from flightops_mcp import server as srv
from flightops_mcp.auth import Scopes
from flightops_mcp.config import Settings
from flightops_mcp.errors import ToolFailure
from flightops_mcp.rebooking import build_proposal

from .conftest import LogCapture
from .idpkit import MCP_AUDIENCE, OPS_AUDIENCE, SERVER, claims_of
from .rebookkit import (
    APPROVER,
    ARGS,
    PROPOSER,
    WriteStack,
    accept,
    asked,
    held,
    payload,
    proposal_of,
    propose,
    write_stack,
)


@pytest.fixture
async def stack(settings: Settings) -> AsyncIterator[WriteStack]:
    async with write_stack(settings) as built:
        yield built


@pytest.fixture
async def client(stack: WriteStack) -> AsyncIterator[Client]:
    """The agent's connection: signed in as the proposer, able to relay an approval request."""
    async with held(stack.client(await stack.proposer_token())) as connected:
        yield connected


async def approved(stack: WriteStack, client: Client, **grant: Any) -> dict[str, Any]:
    """Propose, have the approver approve what was proposed, and commit."""
    request = asked(await propose(client))
    token = await stack.grant(proposal_of(request), **grant)
    return payload(await propose(client, answer=accept(token), state=request.request_state))


def nothing_written(stack: WriteStack) -> None:
    assert stack.ops.rebookings() == [], "the operations API must not even be asked"
    assert stack.writes == []


# --- no approval, no write ------------------------------------------------------
async def test_a_proposal_is_answered_with_an_approval_request_and_writes_nothing(
    stack: WriteStack, client: Client
) -> None:
    result = await propose(client)

    assert isinstance(result, InputRequiredResult)
    assert result.result_type == "input_required"
    assert result.request_state, "the proposal must come back with state to retry under"
    request = result.input_requests["approval"]  # type: ignore[index]
    assert request.method == "elicitation/create"
    message = request.params.message  # type: ignore[union-attr]
    expected = build_proposal(**ARGS)
    assert expected.sha256 in message
    assert "move 2 passenger(s) (tight_connections) from SK1429" in message
    assert f"Proposed by {PROPOSER}" in message
    assert proposal_of(result) == {"v": 1, **expected.as_dict()}
    nothing_written(stack)


async def test_retrying_without_an_approval_response_is_refused_as_data(
    stack: WriteStack, client: Client
) -> None:
    request = asked(await propose(client))

    refused = payload(await propose(client, state=request.request_state))

    assert refused["ok"] is False
    assert refused["error"]["code"] == "approval_required"
    assert "cannot approve this yourself" in refused["error"]["hint"]
    nothing_written(stack)


async def test_an_approval_without_a_proposal_round_is_refused(
    stack: WriteStack, client: Client
) -> None:
    """A valid grant, presented straight away, for a question this server never asked."""
    grant = await stack.grant({"v": 1, **build_proposal(**ARGS).as_dict()})

    refused = payload(await propose(client, answer=accept(grant)))

    assert refused["error"]["code"] == "approval_required"
    nothing_written(stack)


@pytest.mark.parametrize("action", ["decline", "cancel"])
async def test_a_declined_or_dismissed_approval_writes_nothing(
    stack: WriteStack, client: Client, action: str
) -> None:
    request = asked(await propose(client))

    refused = payload(
        await propose(
            client,
            answer=ElicitResult(action=action),  # type: ignore[arg-type]
            state=request.request_state,
        )
    )

    assert refused["error"]["code"] == "approval_declined"
    assert refused["error"]["details"] == {"action": action}
    nothing_written(stack)


@pytest.mark.parametrize(
    "content",
    [None, {}, {"approval_token": ""}, {"approved": True}, {"approval_token": True}],
    ids=["no-content", "empty-form", "empty-grant", "just-says-yes", "boolean-yes"],
)
async def test_saying_yes_is_not_an_approval(
    stack: WriteStack, client: Client, content: dict[str, Any] | None
) -> None:
    """The agent is the client. An accepted form with no grant is the agent agreeing with itself."""
    request = asked(await propose(client))

    refused = payload(
        await propose(
            client,
            answer=ElicitResult(action="accept", content=content),
            state=request.request_state,
        )
    )

    assert refused["error"]["code"] == "approval_required"
    nothing_written(stack)


async def test_a_client_that_cannot_relay_an_approval_request_is_refused_as_data(
    stack: WriteStack,
) -> None:
    async with stack.client(await stack.proposer_token(), can_elicit=False) as plain:
        refused = payload(await propose(plain))

    assert refused["ok"] is False
    assert refused["error"]["code"] == "approval_required"
    assert "cannot relay an approval request" in refused["error"]["message"]
    nothing_written(stack)


async def test_the_sdks_automatic_retry_cannot_approve_either(stack: WriteStack) -> None:
    """The stock client loop answers the elicitation itself. Its answer carries no grant."""

    async def says_yes(_context: Any, _params: Any) -> ElicitResult:
        return ElicitResult(action="accept", content={"approval_token": "yes, approved"})

    async with stack.client(await stack.proposer_token()) as connected:
        connected.session._elicitation_callback = says_yes  # noqa: SLF001 - the stock driver
        result = await connected.call_tool("propose_rebooking", ARGS)

    assert result.structured_content["error"]["code"] == "approval_invalid"  # type: ignore[index]
    nothing_written(stack)


# --- what counts as an approval -------------------------------------------------
async def test_an_approved_proposal_executes_once_as_the_user(
    stack: WriteStack, client: Client
) -> None:
    done = await approved(stack, client)

    assert done["ok"] is True
    assert done["result"]["status"] == "confirmed"
    assert done["result"]["passengers_rebooked"] == 2
    assert done["idempotency"] == {
        "key": build_proposal(**ARGS).idempotency_key,
        "replayed": False,
    }
    assert done["audit"]["proposed_by"] == PROPOSER
    assert done["audit"]["approved_by"] == APPROVER
    assert done["audit"]["proposed"] == build_proposal(**ARGS).as_dict()
    assert done["audit"]["executed"] == {
        "rebooking_id": done["result"]["rebooking_id"],
        "passengers_rebooked": 2,
    }
    assert len(stack.writes) == 1
    assert stack.writes[0]["user"] == PROPOSER
    assert stack.writes[0]["approved_by"] == APPROVER
    assert stack.ops_app.state.audit[-1]["actor"] == SERVER


async def test_what_goes_downstream_is_an_exchanged_token_and_never_the_grant(
    stack: WriteStack,
) -> None:
    inbound = await stack.proposer_token()
    async with stack.client(inbound) as connected:
        request = asked(await propose(connected))
        grant = await stack.grant(proposal_of(request))
        await propose(connected, answer=accept(grant), state=request.request_state)

    (sent,) = stack.ops.rebookings()
    bearer = sent.headers["authorization"].removeprefix("Bearer ")
    assert bearer not in {inbound, grant}
    claims = claims_of(bearer)
    assert claims["aud"] == OPS_AUDIENCE
    assert claims["sub"] == PROPOSER
    assert claims["scp"] == Scopes.REBOOKING_WRITE, "only the scope this call needs"
    assert claims["act"] == {"sub": SERVER}
    assert sent.headers["idempotency-key"] == build_proposal(**ARGS).idempotency_key
    wire = sent.content.decode() + json.dumps(dict(sent.headers))
    assert grant not in wire and inbound not in wire


async def test_a_fabricated_grant_is_refused(stack: WriteStack, client: Client) -> None:
    request = asked(await propose(client))

    refused = payload(
        await propose(
            client, answer=accept("eyJhbGciOiJub25lIn0.e30."), state=request.request_state
        )
    )

    assert refused["error"]["code"] == "approval_invalid"
    nothing_written(stack)


async def test_an_access_token_is_not_a_grant(stack: WriteStack, client: Client) -> None:
    """Right issuer, right person, right permission, right proposal. Wrong audience."""
    request = asked(await propose(client))
    access_token = await stack.grant(proposal_of(request), audience=MCP_AUDIENCE)

    refused = payload(
        await propose(client, answer=accept(access_token), state=request.request_state)
    )

    assert refused["error"]["code"] == "approval_invalid"
    nothing_written(stack)


async def test_a_grant_is_not_an_access_token(stack: WriteStack) -> None:
    """The reverse: a grant presented as a bearer token opens nothing on this server."""
    grant = await stack.grant({"v": 1, **build_proposal(**ARGS).as_dict()})

    response = await stack.remote.call_tool("resolve_airport", {"query": "ESGG"}, token=grant)

    assert response.status_code == 401


async def test_the_proposer_cannot_approve_their_own_proposal(
    stack: WriteStack, client: Client
) -> None:
    """Even holding the approve permission: the agent has its user's credentials."""
    refused = await approved(stack, client, sub=PROPOSER)

    assert refused["error"]["code"] == "approver_is_proposer"
    nothing_written(stack)


async def test_an_approver_without_the_permission_is_refused(
    stack: WriteStack, client: Client
) -> None:
    refused = await approved(stack, client, scope=Scopes.REBOOKING_WRITE)

    assert refused["error"]["code"] == "approver_not_authorised"
    nothing_written(stack)


async def test_an_approval_of_another_proposal_is_refused(
    stack: WriteStack, client: Client
) -> None:
    """Approve moving two people; try to use it to move them somewhere else."""
    other = asked(await propose(client, {**ARGS, "to_flight": "SK1433"}))
    request = asked(await propose(client))
    grant_for_other = await stack.grant(proposal_of(other))

    refused = payload(
        await propose(client, answer=accept(grant_for_other), state=request.request_state)
    )

    assert refused["error"]["code"] == "approval_payload_mismatch"
    assert refused["error"]["details"] == {
        "approved_sha256": build_proposal(**{**ARGS, "to_flight": "SK1433"}).sha256,
        "proposed_sha256": build_proposal(**ARGS).sha256,
    }
    nothing_written(stack)


@pytest.mark.parametrize(
    "change",
    [{"passenger_count": 3}, {"to_flight": "SK1433"}, {"cohort": "all"}, {"reason_code": "crew"}],
    ids=lambda change: next(iter(change)),
)
async def test_changing_the_proposal_after_it_was_approved_is_refused(
    stack: WriteStack, client: Client, change: dict[str, Any]
) -> None:
    """The approved payload and the executed payload must be the same payload."""
    request = asked(await propose(client))
    grant = await stack.grant(proposal_of(request))

    with pytest.raises(MCPError, match="Invalid or expired requestState"):
        await propose(client, {**ARGS, **change}, answer=accept(grant), state=request.request_state)

    nothing_written(stack)


async def test_the_grant_alone_does_not_carry_over_to_a_changed_proposal(
    stack: WriteStack, client: Client
) -> None:
    """Propose the changed payload properly, then answer it with the old approval."""
    original = asked(await propose(client))
    grant = await stack.grant(proposal_of(original))
    changed = asked(await propose(client, {**ARGS, "passenger_count": 3}))

    refused = payload(
        await propose(
            client,
            {**ARGS, "passenger_count": 3},
            answer=accept(grant),
            state=changed.request_state,
        )
    )

    assert refused["error"]["code"] == "approval_payload_mismatch"
    nothing_written(stack)


async def test_another_caller_cannot_commit_someone_elses_proposal(stack: WriteStack) -> None:
    async with stack.client(await stack.proposer_token()) as first:
        request = asked(await propose(first))
    grant = await stack.grant(proposal_of(request))

    async with stack.client(await stack.proposer_token(sub="dispatcher-8")) as second:
        with pytest.raises(MCPError, match="Invalid or expired requestState"):
            await propose(second, answer=accept(grant), state=request.request_state)

    nothing_written(stack)


# --- how long an approval is valid ----------------------------------------------
async def test_an_approval_older_than_the_limit_is_refused(
    stack: WriteStack, client: Client
) -> None:
    """A grant with an hour left on its `exp` is still too old after five minutes."""
    request = asked(await propose(client))
    grant = await stack.grant(proposal_of(request), expires_in=3600)
    stack.clock.advance(301)

    refused = payload(await propose(client, answer=accept(grant), state=request.request_state))

    assert refused["error"]["code"] == "approval_expired"
    nothing_written(stack)


async def test_an_approval_just_inside_the_limit_is_accepted(
    stack: WriteStack, client: Client
) -> None:
    request = asked(await propose(client))
    grant = await stack.grant(proposal_of(request), expires_in=3600)
    stack.clock.advance(290)

    done = payload(await propose(client, answer=accept(grant), state=request.request_state))

    assert done["ok"] is True
    assert len(stack.writes) == 1


async def test_an_expired_grant_is_refused(stack: WriteStack, client: Client) -> None:
    request = asked(await propose(client))
    grant = await stack.grant(proposal_of(request), expires_in=60)
    stack.clock.advance(61)

    refused = payload(await propose(client, answer=accept(grant), state=request.request_state))

    assert refused["error"]["code"] == "approval_expired"
    nothing_written(stack)


async def test_a_proposal_left_past_its_window_must_be_proposed_again(
    stack: WriteStack, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window is the SDK's state TTL. Move its clock, not ours, and do not sleep."""
    request = asked(await propose(client))
    grant = await stack.grant(proposal_of(request))
    window = stack.runtime.settings.approval_window_s
    real = time.time
    monkeypatch.setattr(request_state, "time", SimpleNamespace(time=lambda: real() + window + 1))

    with pytest.raises(MCPError, match="Invalid or expired requestState"):
        await propose(client, answer=accept(grant), state=request.request_state)

    nothing_written(stack)


# --- the same proposal executes once ---------------------------------------------
async def test_replaying_the_commit_returns_the_original_result_and_writes_once(
    stack: WriteStack, client: Client
) -> None:
    request = asked(await propose(client))
    grant = await stack.grant(proposal_of(request))

    first = payload(await propose(client, answer=accept(grant), state=request.request_state))
    stack.clock.advance(30)
    second = payload(await propose(client, answer=accept(grant), state=request.request_state))

    assert first["idempotency"]["replayed"] is False
    assert second["idempotency"]["replayed"] is True
    assert second["idempotency"]["key"] == first["idempotency"]["key"]
    assert (
        json.dumps(second["result"], sort_keys=True).encode()
        == json.dumps(first["result"], sort_keys=True).encode()
    ), "the original result, byte for byte"
    assert second["as_of"] != first["as_of"], "only the envelope knows it is a later call"
    assert len(stack.ops.rebookings()) == 2, "the replay did reach the API"
    assert len(stack.writes) == 1, "and the API wrote once"


async def test_a_second_proposal_of_the_same_content_is_the_same_rebooking(
    stack: WriteStack, client: Client
) -> None:
    """A fresh proposal round, a different approver, the same key: still one write."""
    first = await approved(stack, client)

    second = await approved(stack, client, sub="station-manager-2")

    assert second["ok"] is True
    assert second["idempotency"] == {"key": first["idempotency"]["key"], "replayed": True}
    assert second["result"] == first["result"]
    assert [write["approved_by"] for write in stack.writes] == [APPROVER]


async def test_a_lost_response_is_retried_without_a_second_write(
    stack: WriteStack, client: Client
) -> None:
    """The API writes, the connection drops, the client retries. This is what the key is for."""
    request = asked(await propose(client))
    grant = await stack.grant(proposal_of(request))
    stack.ops.lose_responses = 1

    done = payload(await propose(client, answer=accept(grant), state=request.request_state))

    assert done["ok"] is True
    assert done["result"]["passengers_rebooked"] == 2
    assert done["idempotency"]["replayed"] is True, "the answer came from the retry"
    assert len(stack.ops.rebookings()) == 2
    assert len(stack.writes) == 1


async def test_a_different_date_is_a_different_rebooking(stack: WriteStack) -> None:
    today = build_proposal(**ARGS)
    tomorrow = build_proposal(**{**ARGS, "flight_date": "2026-11-17"})

    assert today.idempotency_key != tomorrow.idempotency_key


# --- refusals after approval are still data --------------------------------------
async def test_a_stale_count_stops_an_approved_rebooking(stack: WriteStack) -> None:
    """Approved for five passengers; the cohort holds two. The approval describes nothing real."""
    async with stack.client(await stack.proposer_token()) as connected:
        request = asked(await propose(connected, {**ARGS, "passenger_count": 5}))
        grant = await stack.grant(proposal_of(request))
        refused = payload(
            await propose(
                connected,
                {**ARGS, "passenger_count": 5},
                answer=accept(grant),
                state=request.request_state,
            )
        )

    assert refused["error"]["code"] == "cohort_changed"
    assert refused["error"]["details"] == {"approved": 5, "current": 2}
    assert stack.writes == []


@pytest.mark.parametrize(
    ("change", "code"),
    [({"to_flight": "SK1433"}, "insufficient_seats"), ({"to_flight": "SK9001"}, "not_found")],
)
async def test_an_approved_rebooking_the_api_refuses_is_reported_not_raised(
    stack: WriteStack, client: Client, change: dict[str, Any], code: str
) -> None:
    arguments = {**ARGS, **change}
    request = asked(await propose(client, arguments))
    grant = await stack.grant(proposal_of(request))

    refused = payload(
        await propose(client, arguments, answer=accept(grant), state=request.request_state)
    )

    assert refused["ok"] is False
    assert refused["error"]["code"] == code
    assert stack.writes == []


@pytest.mark.parametrize(
    "change",
    [
        {"to_flight": "SK1429"},
        {"from_flight": "not a flight"},
        {"flight_date": "tomorrow"},
        {"cohort": "the passenger in 12A"},
        {"reason_code": "because Ada Lovelace asked"},
        {"passenger_count": 0},
        {"passenger_count": 501},
    ],
    ids=lambda change: f"{next(iter(change))}={next(iter(change.values()))}",
)
async def test_a_malformed_proposal_never_reaches_an_approver(
    stack: WriteStack, client: Client, change: dict[str, Any]
) -> None:
    refused = payload(await propose(client, {**ARGS, **change}))

    assert refused["error"]["code"] == "invalid_argument"
    nothing_written(stack)


# --- who may propose ---------------------------------------------------------------
async def test_a_caller_without_the_write_scope_gets_a_scope_challenge(
    stack: WriteStack,
) -> None:
    token = await stack.proposer_token(scope=Scopes.PASSENGERS_READ)

    response = await stack.remote.call_tool("propose_rebooking", ARGS, token=token)

    assert response.status_code == 403
    assert Scopes.REBOOKING_WRITE in response.headers["www-authenticate"]
    nothing_written(stack)


async def test_a_service_principal_is_turned_away_before_anyone_is_asked(
    stack: WriteStack,
) -> None:
    """A machine holding the write permission cannot propose: there is no user to act for."""
    robot = stack.idp.provider._issue(  # noqa: SLF001 - an application token the fixture has no grant for
        {
            "aud": MCP_AUDIENCE,
            "sub": "robot-1",
            "azp": "robot-1",
            "idtyp": "app",
            "roles": [Scopes.REBOOKING_WRITE],
        },
        ttl_s=300,
        scope=Scopes.REBOOKING_WRITE,
    ).access_token

    async with stack.client(robot) as connected:
        refused = payload(await propose(connected))

    assert refused["error"]["code"] == "obo_exchange_rejected"
    nothing_written(stack)


async def test_over_stdio_there_is_no_user_so_there_is_no_write(runtime: Any) -> None:
    async with Client(srv.mcp) as local:
        result = await local.call_tool("propose_rebooking", ARGS)

    assert result.structured_content["error"]["code"] == "user_token_required"  # type: ignore[index]


def test_state_for_another_proposal_is_refused_by_the_tool_as_well() -> None:
    """The second line behind the SDK's binding, in this codebase's own terms."""
    proposal = build_proposal(**ARGS)
    other = build_proposal(**{**ARGS, "passenger_count": 3})
    state = json.dumps({"v": 1, "proposal_sha256": other.sha256})

    for bad in (None, "not json", "[]", state):
        with pytest.raises(ToolFailure) as excinfo:
            srv._check_proposal_state(bad, proposal)  # noqa: SLF001
        assert excinfo.value.code == "approval_required"

    srv._check_proposal_state(  # noqa: SLF001
        json.dumps({"v": 1, "proposal_sha256": proposal.sha256}), proposal
    )


# --- the audit record, and what must not be in it -----------------------------------
CANARY_ROWS = [
    {
        "passenger_name": f"CANARY-PAX-{number}",
        "email": f"CANARY-EMAIL-{number}@example.com",
        "phone": f"CANARY-PHONE-{number}",
        "booking_ref": f"CANARY-PNR-{number}",
        "date_of_birth": f"CANARY-DOB-{number}",
        "seat": "1A",
        "checked_in": True,
        "connecting_flight": "SK903",
        "connection_minutes": 20,
        "needs_assistance": False,
    }
    for number in (1, 2)
]


async def test_the_audit_record_names_people_who_act_and_nobody_who_flies(
    settings: Settings, logs: LogCapture
) -> None:
    async with write_stack(settings, manifests={"SK1429": CANARY_ROWS}) as stack:
        inbound = await stack.proposer_token()
        async with stack.client(inbound) as connected:
            request = asked(await propose(connected))
            grant = await stack.grant(proposal_of(request))
            done = payload(
                await propose(connected, answer=accept(grant), state=request.request_state)
            )

    assert done["result"]["passengers_rebooked"] == 2, "the canaries were the ones moved"
    (audit,) = logs.events("rebooking_audit")
    assert audit["proposed_by"] == PROPOSER
    assert audit["approved_by"] == APPROVER
    assert audit["proposal"] == build_proposal(**ARGS).as_dict()
    assert audit["proposal_sha256"] == build_proposal(**ARGS).sha256
    assert audit["rebooking_id"] == done["result"]["rebooking_id"]
    assert audit["passengers_rebooked"] == 2
    assert audit["replayed"] is False
    (proposed,) = logs.events("rebooking_proposed")
    assert proposed["proposed_by"] == PROPOSER

    everything = logs.text + json.dumps(done)
    assert "CANARY" not in everything, "a passenger reached a log line or the tool result"
    assert grant not in everything, "the approval grant was written down"
    assert inbound not in everything


async def test_a_refused_approval_is_logged_with_its_reason_and_without_the_grant(
    stack: WriteStack, client: Client, logs: LogCapture
) -> None:
    request = asked(await propose(client))
    grant = await stack.grant(proposal_of(request), sub=PROPOSER)

    await propose(client, answer=accept(grant), state=request.request_state)

    (rejected,) = logs.events("approval_rejected")
    assert rejected["reason"] == "self_approval"
    assert rejected["approver"] == PROPOSER
    assert grant not in logs.text
