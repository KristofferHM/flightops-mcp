"""The mock ops API's one write: idempotent, counted, and refused when it should be.

These tests talk to the API directly, as the MCP server would, so the properties
are pinned at the system that owns the data. "No second write" is asserted on the
API's own ledger (`app.state.writes`), never on what a client believes it sent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from services.ops_api.app import DEFAULT_MANIFESTS, OpsApiConfig, create_app

from .authkit import ISSUER, JWKS_URL
from .conftest import FakeClock
from .idpkit import OPS_AUDIENCE, PASSENGERS_READ, REBOOKING_WRITE, LocalIdp

KEY = "rbk_" + "a1" * 16
OTHER_KEY = "rbk_" + "b2" * 16
APPROVER = "duty-manager-7"


def proposal(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "from_flight": "SK1429",
        "flight_date": "2026-11-16",
        "to_flight": "SK1431",
        "cohort": "tight_connections",
        "reason_code": "late_inbound",
        "expected_passengers": 2,
        "approved_by": APPROVER,
        "proposal_sha256": "0" * 64,
    }
    body.update(changes)
    return body


@dataclass(slots=True)
class Ops:
    idp: LocalIdp
    app: Any
    http: httpx.AsyncClient

    @property
    def writes(self) -> list[dict[str, Any]]:
        writes: list[dict[str, Any]] = self.app.state.writes
        return writes

    async def token(self, scope: str = REBOOKING_WRITE, sub: str = "dispatcher-3") -> str:
        """A token exchanged for this API, as the MCP server would present it."""
        inbound = await self.idp.user_token(sub, scope=scope)
        response = await self.idp.exchange(inbound, scope=scope)
        assert response.status_code == 200, response.text
        return str(response.json()["access_token"])

    async def rebook(
        self, body: dict[str, Any], *, key: str | None = KEY, token: str | None = None
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token or await self.token()}"}
        if key is not None:
            headers["Idempotency-Key"] = key
        return await self.http.post("/rebookings", json=body, headers=headers)


@pytest.fixture
async def ops(clock: FakeClock) -> AsyncIterator[Ops]:
    idp = LocalIdp()
    app = create_app(
        OpsApiConfig(issuer=ISSUER, jwks_url=JWKS_URL, audience=OPS_AUDIENCE),
        jwks_transport=idp.transport,
        clock=clock,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ops.test"
    ) as http:
        yield Ops(idp=idp, app=app, http=http)


async def test_the_first_request_writes_once_and_moves_the_cohort(ops: Ops) -> None:
    response = await ops.rebook(proposal())

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["executed_at"] == "2023-11-14T22:13:20+00:00", "the injected clock, not the wall"
    assert len(body["passengers"]) == 2
    assert len(ops.writes) == 1
    assert ops.writes[0]["passengers_moved"] == 2
    assert ops.writes[0]["approved_by"] == APPROVER

    read = await ops.http.get(
        "/flights/SK1429/passengers",
        headers={"Authorization": f"Bearer {await ops.token(PASSENGERS_READ)}"},
    )
    assert len(read.json()["passengers"]) == 4, "the moved passengers left the source flight"


async def test_a_replayed_key_returns_the_original_bytes_and_writes_nothing(ops: Ops) -> None:
    first = await ops.rebook(proposal())
    second = await ops.rebook(proposal())
    third = await ops.rebook(proposal())

    assert first.status_code == second.status_code == third.status_code == 201
    assert second.content == first.content, "byte for byte, not merely equal after parsing"
    assert third.content == first.content
    assert "idempotent-replayed" not in first.headers
    assert second.headers["idempotent-replayed"] == "true"
    assert len(ops.writes) == 1, "three requests, one write"
    outcomes = [entry["outcome"] for entry in ops.app.state.audit]
    assert outcomes == ["executed", "replayed", "replayed"]


async def test_a_replay_approved_by_someone_else_is_still_the_original(ops: Ops) -> None:
    first = await ops.rebook(proposal())
    second = await ops.rebook(proposal(approved_by="station-manager-2"))

    assert second.content == first.content
    assert second.headers["idempotent-replayed"] == "true"
    assert [write["approved_by"] for write in ops.writes] == [APPROVER], (
        "one write, and the ledger keeps who approved it when it happened"
    )


async def test_a_replay_survives_the_clock_moving_on(ops: Ops, clock: FakeClock) -> None:
    first = await ops.rebook(proposal())
    clock.advance(3600)
    second = await ops.rebook(proposal())

    assert second.content == first.content, "a replay returns what happened, not a new timestamp"


async def test_the_same_key_for_a_different_request_is_refused_not_replayed(ops: Ops) -> None:
    await ops.rebook(proposal())

    response = await ops.rebook(proposal(to_flight="SK1433"))

    assert response.status_code == 422
    assert response.json() == {"error": "idempotency_key_reused"}
    assert len(ops.writes) == 1


async def test_a_different_key_is_a_different_request(ops: Ops) -> None:
    await ops.rebook(proposal())

    # The tight connections have already moved, so the count the approver saw is stale.
    response = await ops.rebook(proposal(), key=OTHER_KEY)

    assert response.status_code == 409
    assert response.json() == {"error": "cohort_changed", "expected": 2, "actual": 0}
    assert len(ops.writes) == 1


@pytest.mark.parametrize("key", [None, "", "short", "has spaces in it, which is no key"])
async def test_a_write_without_a_usable_idempotency_key_is_refused(
    ops: Ops, key: str | None
) -> None:
    response = await ops.rebook(proposal(), key=key)

    assert response.status_code == 400
    assert response.json() == {"error": "idempotency_key_required"}
    assert ops.writes == []


async def test_a_stale_passenger_count_stops_the_write(ops: Ops) -> None:
    response = await ops.rebook(proposal(expected_passengers=5))

    assert response.status_code == 409
    assert response.json() == {"error": "cohort_changed", "expected": 5, "actual": 2}
    assert ops.writes == []


async def test_a_refused_request_is_not_remembered_as_a_result(ops: Ops) -> None:
    refused = await ops.rebook(proposal(to_flight="SK1433"))
    assert refused.json()["error"] == "insufficient_seats"

    # Same key, same body, after a seat was freed elsewhere would write. Here the
    # point is narrower: the refusal was not stored and replayed as if it were final.
    again = await ops.rebook(proposal(to_flight="SK1433"))

    assert again.status_code == 409
    assert "idempotent-replayed" not in again.headers
    assert ops.writes == []


async def test_more_passengers_than_seats_is_refused_with_counts_only(ops: Ops) -> None:
    response = await ops.rebook(proposal(to_flight="SK1433"))

    assert response.status_code == 409
    assert response.json() == {"error": "insufficient_seats", "needed": 2, "free": 1}
    assert ops.writes == []


async def test_the_requester_cannot_be_named_as_the_approver(ops: Ops) -> None:
    token = await ops.token(sub="dispatcher-3")

    response = await ops.rebook(proposal(approved_by="dispatcher-3"), token=token)

    assert response.status_code == 403
    assert response.json() == {"error": "approver_is_requester"}
    assert ops.writes == []


@pytest.mark.parametrize(
    "change",
    [{"from_flight": "ZZ1"}, {"to_flight": "SK1429"}],
    ids=["unknown-source", "target-takes-no-rebookings"],
)
async def test_an_unknown_flight_is_not_found(ops: Ops, change: dict[str, Any]) -> None:
    response = await ops.rebook(proposal(**change))

    assert response.status_code == 404
    assert ops.writes == []


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"cohort": "everyone-in-row-12"}, "cohort"),
        ({"flight_date": "tomorrow"}, "flight_date"),
        ({"expected_passengers": 0}, "expected_passengers"),
        ({"expected_passengers": True}, "expected_passengers"),
        ({"approved_by": ""}, "approved_by"),
        ({"proposal_sha256": None}, "proposal_sha256"),
    ],
)
async def test_a_malformed_request_names_the_field_and_writes_nothing(
    ops: Ops, change: dict[str, Any], field: str
) -> None:
    response = await ops.rebook(proposal(**change))

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request", "field": field}
    assert ops.writes == []


async def test_a_read_token_cannot_write_and_a_write_token_cannot_read(ops: Ops) -> None:
    read_token = await ops.token(PASSENGERS_READ)
    write_token = await ops.token(REBOOKING_WRITE)

    write = await ops.rebook(proposal(), token=read_token)
    read = await ops.http.get(
        "/flights/SK1429/passengers", headers={"Authorization": f"Bearer {write_token}"}
    )

    assert write.status_code == 403
    assert write.json()["error"] == "insufficient_scope"
    assert read.status_code == 403
    assert ops.writes == []


async def test_the_users_own_token_cannot_write(ops: Ops) -> None:
    """Not exchanged for this API: wrong audience, and no actor."""
    inbound = await ops.idp.user_token("dispatcher-3", scope=REBOOKING_WRITE)

    response = await ops.rebook(proposal(), token=inbound)

    assert response.status_code == 401
    assert ops.writes == []


async def test_one_app_does_not_edit_anothers_passengers(ops: Ops) -> None:
    await ops.rebook(proposal())

    assert len(DEFAULT_MANIFESTS["SK1429"]) == 6, "the shared default must not be mutated"
