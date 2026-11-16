"""The human approval gate: what counts as an approval, and what does not.

The write tool asks for approval through the protocol's `input_required` round
trip. That round trip is carried by the client, and the client is the agent. So
an answer of "yes" proves nothing: anything able to call the tool is able to
say yes. The gate therefore does not accept an answer. It accepts a **grant**: a
token the identity provider signed for a human, which the agent cannot mint.

A grant is accepted only if all of this holds:

* **Signed by our issuer, for the approval audience.** The audience is not this
  server's, so a grant is useless as an access token here, and an access token
  is useless as a grant.
* **A person, holding the approve permission.** A delegated (`scp`) token with
  `flightops.rebooking.approve`. An application permission does not count: a
  service principal approving is a machine approving.
* **Not the proposer.** The subject must differ from the caller's. An agent that
  holds its user's credentials still cannot approve its own proposal.
* **Fresh.** Inside its own `exp`, and no older than `max_age_s` whatever its
  `exp` says, so a long-lived token from a lax issuer does not widen the window.
* **For exactly this payload.** It carries the proposal its approver was shown
  (`authorization_details`, RFC 9396), and that must hash to the proposal being
  executed. A grant for another proposal is refused.

What the gate deliberately does not do is track single use. It has no store to
track it in, and does not need one: a grant can only ever authorise the one
write it names, and that write is idempotent. See docs/adr/0003.
"""

from __future__ import annotations

import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from ..errors import ToolFailure
from ..telemetry import get_logger
from .scopes import Scopes
from .verifier import JwtVerifier, TokenRejected

# The `type` of the authorization detail a grant must carry.
APPROVAL_DETAIL_TYPE: Final = "flightops_rebooking"
# Tolerance for an issuer whose clock runs ahead of ours.
_FUTURE_SKEW_S: Final = 30.0

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Approval:
    """A verified approval. Holds who and when, never the grant itself."""

    approved_by: str
    approved_at: float
    proposal_sha256: str


class ApprovalVerifier:
    def __init__(
        self,
        verifier: JwtVerifier | None,
        *,
        clock: Callable[[], float] = time.time,
        max_age_s: float = 300.0,
    ) -> None:
        self._verifier = verifier
        self._clock = clock
        self._max_age_s = max_age_s

    def configured(self) -> bool:
        return self._verifier is not None

    async def verify(
        self,
        grant: str,
        *,
        proposal_sha256: str,
        proposer: str,
        digest: Callable[[dict[str, Any]], str | None],
    ) -> Approval:
        """Return the approval `grant` proves, or raise the `ToolFailure` that explains why not.

        `digest` turns the grant's authorization detail into the SHA-256 of the
        proposal it describes. It is passed in because what a proposal is belongs
        to the tool, and what an approval is belongs here.
        """
        if self._verifier is None:
            raise approval_not_configured()
        try:
            claims = await self._verifier.verified_claims(grant)
        except TokenRejected as exc:
            # The reason is for the operator. The caller learns only what it can act on:
            # an expired grant can be asked for again, any other bad grant cannot be fixed.
            _log.warning("approval_rejected", reason=exc.reason, **exc.context)
            raise (approval_expired() if exc.reason == "expired" else approval_invalid()) from None

        approver = str(claims["sub"])
        if not _is_person_with_permission(claims):
            _log.warning("approval_rejected", reason="not_an_authorised_person", approver=approver)
            raise approver_not_authorised()
        if approver == proposer:
            _log.warning("approval_rejected", reason="self_approval", approver=approver)
            raise approver_is_proposer()

        issued_at = claims.get("iat")
        if isinstance(issued_at, bool) or not isinstance(issued_at, int | float):
            raise approval_invalid()
        age = self._clock() - issued_at
        if age < -_FUTURE_SKEW_S:
            _log.warning("approval_rejected", reason="issued_in_the_future", approver=approver)
            raise approval_invalid()
        if age > self._max_age_s:
            _log.warning(
                "approval_rejected", reason="too_old", approver=approver, age_s=round(age, 1)
            )
            raise approval_expired()

        detail = _approval_detail(claims)
        approved = digest(detail) if detail is not None else None
        if approved is None:
            _log.warning("approval_rejected", reason="names_no_proposal", approver=approver)
            raise approval_invalid()
        if not hmac.compare_digest(approved.encode(), proposal_sha256.encode()):
            _log.warning(
                "approval_rejected",
                reason="payload_mismatch",
                approver=approver,
                approved_sha256=approved,
                proposed_sha256=proposal_sha256,
            )
            raise approval_payload_mismatch(approved, proposal_sha256)

        return Approval(
            approved_by=approver, approved_at=float(issued_at), proposal_sha256=proposal_sha256
        )


def _is_person_with_permission(claims: dict[str, Any]) -> bool:
    # `scp` only. `roles` is where application permissions live, and reading both
    # (as the access-token path rightly does) would let a service principal approve.
    if claims.get("idtyp") == "app":
        return False
    delegated = claims.get("scp")
    return isinstance(delegated, str) and Scopes.REBOOKING_APPROVE in delegated.split()


def _approval_detail(claims: dict[str, Any]) -> dict[str, Any] | None:
    """The one rebooking the grant describes. More than one, or none, is no approval."""
    details = claims.get("authorization_details")
    if not isinstance(details, list):
        return None
    described = [
        item
        for item in details
        if isinstance(item, dict) and item.get("type") == APPROVAL_DETAIL_TYPE
    ]
    return described[0] if len(described) == 1 else None


# --- Refusals ----------------------------------------------------------------
def approval_required(reason: str) -> ToolFailure:
    return ToolFailure(
        code="approval_required",
        message=f"Nothing was rebooked: this action needs a human approval. {reason}",
        hint="You cannot approve this yourself. Tell the user that an approver must sign the "
        "proposal, and stop; do not retry without an approval.",
    )


def approval_declined(action: str) -> ToolFailure:
    return ToolFailure(
        code="approval_declined",
        message=f"Nothing was rebooked: the approval request was answered with {action!r}.",
        hint="Do not retry or rephrase the proposal to get a different answer. Report the "
        "outcome to the user.",
        details={"action": action},
    )


def approval_invalid() -> ToolFailure:
    return ToolFailure(
        code="approval_invalid",
        message="Nothing was rebooked: the approval presented is not a valid approval grant.",
        hint="Not retryable with the same grant. A new approval must come from the approver; "
        "it cannot be constructed.",
    )


def approval_expired() -> ToolFailure:
    return ToolFailure(
        code="approval_expired",
        message="Nothing was rebooked: the approval is too old to act on.",
        hint="Ask the approver to approve the proposal again, then retry with the new grant.",
    )


def approval_payload_mismatch(approved: str, proposed: str) -> ToolFailure:
    return ToolFailure(
        code="approval_payload_mismatch",
        message="Nothing was rebooked: the approval is for a different proposal than this one.",
        hint="An approval covers exactly the payload the approver saw. Propose again and have "
        "that proposal approved; do not reuse an approval across proposals.",
        details={"approved_sha256": approved, "proposed_sha256": proposed},
    )


def approver_not_authorised() -> ToolFailure:
    return ToolFailure(
        code="approver_not_authorised",
        message="Nothing was rebooked: the approver is not a person holding the approve "
        "permission.",
        hint=f"The approval must come from a signed-in user with {Scopes.REBOOKING_APPROVE}. "
        "Not retryable with the same approver.",
    )


def approver_is_proposer() -> ToolFailure:
    return ToolFailure(
        code="approver_is_proposer",
        message="Nothing was rebooked: the proposal was approved by the identity that proposed it.",
        hint="A second person must approve. Tell the user who needs to act; do not retry.",
    )


def approval_not_configured() -> ToolFailure:
    return ToolFailure(
        code="approval_not_configured",
        message="This server cannot verify approvals, so it will not write.",
        hint="An operator must set FLIGHTOPS_AUTH_ISSUER and FLIGHTOPS_AUTH_JWKS_URL. "
        "Not retryable; the read tools are unaffected.",
    )
