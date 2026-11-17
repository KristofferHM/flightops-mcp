"""The approval verifier on its own: every way a grant can fail to be an approval.

`tests/test_rebooking_gate.py` proves the gate holds on the wire. This file is the
matrix behind it, against a fake clock and keys minted for the run, so each
refusal is pinned to the one claim that causes it.
"""

from __future__ import annotations

from typing import Any

import pytest

from flightops_mcp.auth import ApprovalVerifier, JwksCache, JwtVerifier, Scopes
from flightops_mcp.auth.approval import APPROVAL_DETAIL_TYPE
from flightops_mcp.errors import ToolFailure
from flightops_mcp.rebooking import approved_digest, build_proposal

from .authkit import ISSUER, JWKS_URL, RESOURCE, JwksEndpoint, signing_key
from .conftest import FakeClock, LogCapture

KEY = signing_key("approval-key")
OTHER_KEY = signing_key("some-other-issuers-key")
AUDIENCE = "api://flightops-approvals"
PROPOSER = "dispatcher-3"
APPROVER = "duty-manager-7"
PROPOSAL = build_proposal(
    from_flight="SK1429",
    flight_date="2026-11-16",
    to_flight="SK1431",
    cohort="tight_connections",
    reason_code="late_inbound",
    passenger_count=2,
)
SHOWN = {"v": 1, **PROPOSAL.as_dict()}


def detail(proposal: Any = SHOWN, **extra: Any) -> dict[str, Any]:
    return {"type": APPROVAL_DETAIL_TYPE, "proposal": proposal, **extra}


def grant(clock: FakeClock, **claims: Any) -> str:
    """A grant that passes, with any claim overridden or (given `None`) dropped."""
    merged: dict[str, Any] = {
        "subject": APPROVER,
        "audience": AUDIENCE,
        "scp": Scopes.REBOOKING_APPROVE,
        "authorization_details": [detail()],
        "now": clock(),
        "expires_in": 300.0,
        **claims,
    }
    key = merged.pop("key", KEY)
    drop = tuple(name for name, value in merged.items() if value is None and name != "audience")
    return key.mint(**{k: v for k, v in merged.items() if k not in drop}, drop=drop)


@pytest.fixture
def approvals(clock: FakeClock) -> ApprovalVerifier:
    jwks = JwksCache(JWKS_URL, transport=JwksEndpoint(KEY).transport)
    verifier = JwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=jwks, clock=clock)
    return ApprovalVerifier(verifier, clock=clock, max_age_s=300.0)


async def verify(approvals: ApprovalVerifier, token: str, proposer: str = PROPOSER) -> Any:
    return await approvals.verify(
        token, proposal_sha256=PROPOSAL.sha256, proposer=proposer, digest=approved_digest
    )


async def refusal(approvals: ApprovalVerifier, token: str, proposer: str = PROPOSER) -> str:
    with pytest.raises(ToolFailure) as excinfo:
        await verify(approvals, token, proposer)
    assert excinfo.value.message.startswith("Nothing was rebooked"), "a refusal says so first"
    return excinfo.value.code


async def test_a_valid_grant_is_an_approval(approvals: ApprovalVerifier, clock: FakeClock) -> None:
    approval = await verify(approvals, grant(clock))

    assert approval.approved_by == APPROVER
    assert approval.approved_at == clock()
    assert approval.proposal_sha256 == PROPOSAL.sha256
    assert not hasattr(approval, "token"), "an approval records who and when, never the grant"


# --- signed by our issuer, for the approval audience ------------------------------
@pytest.mark.parametrize(
    "claims",
    [
        {"audience": RESOURCE},
        {"audience": None},
        {"audience": "api://flightops-ops"},
        {"issuer": "http://another-idp.test"},
        {"key": OTHER_KEY},
    ],
    ids=["this-servers-audience", "no-audience", "ops-audience", "other-issuer", "unknown-key"],
)
async def test_a_grant_not_issued_for_approvals_is_invalid(
    approvals: ApprovalVerifier, clock: FakeClock, claims: dict[str, Any]
) -> None:
    assert await refusal(approvals, grant(clock, **claims)) == "approval_invalid"


@pytest.mark.parametrize("token", ["", "yes", "approved", "a.b.c", "Bearer abc"])
async def test_text_is_not_a_grant(approvals: ApprovalVerifier, token: str) -> None:
    assert await refusal(approvals, token) == "approval_invalid"


# --- a person, holding the approve permission -------------------------------------
@pytest.mark.parametrize(
    "claims",
    [
        {"scp": Scopes.REBOOKING_WRITE},
        {"scp": ""},
        {"scp": None},
        {"scp": "flightops.rebooking.approve-everything"},
        # An application permission: a service principal holding the approve role.
        {"scp": None, "roles": [Scopes.REBOOKING_APPROVE]},
        {"scp": None, "roles": [Scopes.REBOOKING_APPROVE], "idtyp": "app"},
        # Marked as an application even though it carries a delegated-looking claim.
        {"idtyp": "app"},
        # RFC 9068 spells it `scope`. Approval reads `scp` only, on purpose.
        {"scp": None, "scope": Scopes.REBOOKING_APPROVE},
    ],
    ids=[
        "write-not-approve",
        "empty-scp",
        "no-scp",
        "lookalike-scope",
        "role-only",
        "app-with-role",
        "app-with-scp",
        "rfc9068-scope",
    ],
)
async def test_only_a_person_with_the_approve_permission_can_approve(
    approvals: ApprovalVerifier, clock: FakeClock, claims: dict[str, Any]
) -> None:
    assert await refusal(approvals, grant(clock, **claims)) == "approver_not_authorised"


async def test_the_approve_permission_among_others_is_enough(
    approvals: ApprovalVerifier, clock: FakeClock
) -> None:
    token = grant(clock, scp=f"flightops.weather.read {Scopes.REBOOKING_APPROVE}")

    assert (await verify(approvals, token)).approved_by == APPROVER


# --- not the proposer ---------------------------------------------------------------
async def test_the_proposer_cannot_be_the_approver(
    approvals: ApprovalVerifier, clock: FakeClock
) -> None:
    assert await refusal(approvals, grant(clock, subject=PROPOSER)) == "approver_is_proposer"
    assert await refusal(approvals, grant(clock), proposer=APPROVER) == "approver_is_proposer"


# --- fresh -----------------------------------------------------------------------------
async def test_a_grant_is_valid_up_to_the_age_limit_and_not_past_it(
    approvals: ApprovalVerifier, clock: FakeClock
) -> None:
    token = grant(clock, expires_in=3600.0)

    clock.advance(300)
    assert (await verify(approvals, token)).approved_by == APPROVER
    clock.advance(1)
    assert await refusal(approvals, token) == "approval_expired"


async def test_a_grant_past_its_own_expiry_is_expired_whatever_its_age(
    approvals: ApprovalVerifier, clock: FakeClock
) -> None:
    token = grant(clock, expires_in=30.0)
    clock.advance(30)

    assert await refusal(approvals, token) == "approval_expired"


async def test_a_grant_issued_in_the_future_is_invalid(
    approvals: ApprovalVerifier, clock: FakeClock
) -> None:
    within_skew = grant(clock, now=clock() + 20)
    beyond_skew = grant(clock, now=clock() + 600)

    assert (await verify(approvals, within_skew)).approved_by == APPROVER
    assert await refusal(approvals, beyond_skew) == "approval_invalid"


# --- for exactly this payload -------------------------------------------------------------
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("from_flight", "SK1427"),
        ("flight_date", "2026-11-17"),
        ("to_flight", "SK1433"),
        ("cohort", "all"),
        ("reason_code", "weather"),
        ("passenger_count", 3),
        ("passenger_count", 2.0),
        ("passenger_count", "2"),
        ("v", 2),
    ],
)
async def test_a_grant_describing_any_other_proposal_is_a_mismatch(
    approvals: ApprovalVerifier, clock: FakeClock, field: str, value: Any
) -> None:
    token = grant(clock, authorization_details=[detail({**SHOWN, field: value})])

    assert await refusal(approvals, token) == "approval_payload_mismatch"


async def test_a_grant_with_an_extra_field_describes_a_different_proposal(
    approvals: ApprovalVerifier, clock: FakeClock
) -> None:
    token = grant(clock, authorization_details=[detail({**SHOWN, "also": "the crew"})])

    assert await refusal(approvals, token) == "approval_payload_mismatch"


async def test_field_order_in_the_grant_does_not_matter(
    approvals: ApprovalVerifier, clock: FakeClock
) -> None:
    token = grant(clock, authorization_details=[detail(dict(reversed(list(SHOWN.items()))))])

    assert (await verify(approvals, token)).proposal_sha256 == PROPOSAL.sha256


@pytest.mark.parametrize(
    "details",
    [
        None,
        [],
        "flightops_rebooking",
        [{"type": "payment_initiation", "proposal": SHOWN}],
        [{"type": APPROVAL_DETAIL_TYPE}],
        [{"type": APPROVAL_DETAIL_TYPE, "proposal": {}}],
        [{"type": APPROVAL_DETAIL_TYPE, "proposal": PROPOSAL.sha256}],
        [{"type": APPROVAL_DETAIL_TYPE, "proposal_sha256": PROPOSAL.sha256}],
        # Two rebookings in one grant: which one did the person approve?
        [detail(), detail({**SHOWN, "to_flight": "SK1433"})],
        [detail(), detail()],
    ],
    ids=[
        "no-details",
        "empty-details",
        "not-a-list",
        "another-type",
        "no-proposal",
        "empty-proposal",
        "a-hash-instead-of-a-proposal",
        "a-bare-digest-claim",
        "two-different-rebookings",
        "the-same-rebooking-twice",
    ],
)
async def test_a_grant_that_does_not_describe_exactly_one_rebooking_is_invalid(
    approvals: ApprovalVerifier, clock: FakeClock, details: Any
) -> None:
    assert await refusal(approvals, grant(clock, authorization_details=details)) == (
        "approval_invalid"
    )


async def test_an_unrelated_detail_next_to_the_rebooking_is_ignored(
    approvals: ApprovalVerifier, clock: FakeClock
) -> None:
    token = grant(clock, authorization_details=[{"type": "something_else"}, detail()])

    assert (await verify(approvals, token)).approved_by == APPROVER


# --- configuration and logging ---------------------------------------------------------------
async def test_a_server_that_cannot_verify_approvals_refuses_to_write(clock: FakeClock) -> None:
    unconfigured = ApprovalVerifier(None, clock=clock)

    assert unconfigured.configured() is False
    with pytest.raises(ToolFailure) as excinfo:
        await verify(unconfigured, grant(clock))
    assert excinfo.value.code == "approval_not_configured"


async def test_a_rejection_logs_its_reason_and_never_the_grant(
    approvals: ApprovalVerifier, clock: FakeClock, logs: LogCapture
) -> None:
    mismatched = grant(clock, authorization_details=[detail({**SHOWN, "passenger_count": 9})])
    wrong_audience = grant(clock, audience=RESOURCE)

    await refusal(approvals, mismatched)
    await refusal(approvals, wrong_audience)

    reasons = [event["reason"] for event in logs.events("approval_rejected")]
    assert reasons == ["payload_mismatch", "wrong_audience"]
    (mismatch, _) = logs.events("approval_rejected")
    assert mismatch["proposed_sha256"] == PROPOSAL.sha256
    # Equal to the digest of what the grant showed, not merely "not the proposal's": the
    # weaker check also passed when the field was logged as None (mutation run 3).
    assert mismatch["approved_sha256"] == approved_digest(detail({**SHOWN, "passenger_count": 9}))
    assert mismatch["approved_sha256"] != PROPOSAL.sha256
    assert mismatched not in logs.text
    assert wrong_audience not in logs.text
