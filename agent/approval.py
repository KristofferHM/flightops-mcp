"""The approval channel: how a proposal reaches a person, and never the model.

`propose_rebooking` answers its first call with a request for approval
(docs/adr/0003). That request is not shown to the planner and its answer is not
the planner's to give. It goes to an `Approver`: something that can put the
proposal in front of a person and come back with a grant the identity provider
signed for them, or with a refusal, or with nothing.

The grant passes through this module once, on its way to the server. It is held
in a field that has no `repr`, and nothing here returns it to the loop's record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """What the server asked a person to approve."""

    tool: str
    message: str
    # The canonical proposal from the request form: exactly what would execute.
    proposal: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    action: Literal["accept", "decline", "cancel"]
    approver: str | None = None
    # A signed grant, present only on an accept. Not a secret by design, and
    # still never logged, printed or written to a transcript.
    grant: str | None = field(default=None, repr=False)


class Approver(Protocol):
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision | None:
        """A person's answer to `request`, or None if nobody answered."""
