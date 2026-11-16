"""The proposal: one canonical form, and three things derived from it.

The digest an approver signs, the idempotency key the write goes out under and
the audit record all come from the same bytes. These tests pin those bytes,
because a silent change to them would orphan every approval in flight and give
an already-executed rebooking a new key.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from flightops_mcp.errors import ToolFailure
from flightops_mcp.rebooking import COHORTS, REASON_CODES, approved_digest, build_proposal

ARGS: dict[str, Any] = {
    "from_flight": "SK1429",
    "flight_date": "2026-11-16",
    "to_flight": "SK1431",
    "cohort": "tight_connections",
    "reason_code": "late_inbound",
    "passenger_count": 2,
}
CANONICAL = (
    b'{"cohort":"tight_connections","flight_date":"2026-11-16","from_flight":"SK1429",'
    b'"passenger_count":2,"reason_code":"late_inbound","to_flight":"SK1431","v":1}'
)


def test_the_canonical_form_is_pinned() -> None:
    proposal = build_proposal(**ARGS)

    assert proposal.canonical() == CANONICAL
    assert proposal.sha256 == hashlib.sha256(CANONICAL).hexdigest()
    assert proposal.sha256 == "cb7e1e5434ba9ad2f8a984b82c6f20e2ec097bb65cb74e4a91a90490097762ca"


def test_the_idempotency_key_comes_from_the_content_and_nothing_else() -> None:
    first = build_proposal(**ARGS)
    again = build_proposal(**ARGS)

    assert first.idempotency_key == "rbk_cb7e1e5434ba9ad2f8a984b82c6f20e2"
    assert again.idempotency_key == first.idempotency_key, "no counter, no clock, no randomness"
    assert first.idempotency_key == "rbk_" + first.sha256[:32]


def test_the_same_proposal_spelled_differently_is_the_same_proposal() -> None:
    spelled = build_proposal(
        from_flight=" sk 1429 ",
        flight_date=" 2026-11-16",
        to_flight="sk1431",
        cohort=" Tight_Connections ",
        reason_code="LATE_INBOUND",
        passenger_count=2,
    )

    assert spelled == build_proposal(**ARGS)
    assert spelled.idempotency_key == build_proposal(**ARGS).idempotency_key


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("from_flight", "SK1427"),
        ("flight_date", "2026-11-17"),
        ("to_flight", "SK1433"),
        ("cohort", "all"),
        ("reason_code", "weather"),
        ("passenger_count", 3),
    ],
)
def test_changing_any_field_changes_the_digest_and_the_key(field: str, value: Any) -> None:
    original = build_proposal(**ARGS)
    changed = build_proposal(**{**ARGS, field: value})

    assert changed.sha256 != original.sha256
    assert changed.idempotency_key != original.idempotency_key


def test_a_proposal_cannot_carry_free_text_or_a_person() -> None:
    """Every field is a designator, a date, a count or one of a fixed set of words."""
    proposal = build_proposal(**ARGS)

    assert set(proposal.as_dict()) == set(ARGS)
    assert proposal.cohort in COHORTS
    assert proposal.reason_code in REASON_CODES


@pytest.mark.parametrize(
    "change",
    [
        {"from_flight": "the late one"},
        {"to_flight": ""},
        {"to_flight": "SK1429"},
        {"to_flight": " sk 1429"},
        {"flight_date": "16/11/2026"},
        {"flight_date": "2026-02-30"},
        {"cohort": "Example Passenger 01"},
        {"cohort": ""},
        {"reason_code": "the captain said so"},
        {"passenger_count": 0},
        {"passenger_count": -2},
        {"passenger_count": 501},
        {"passenger_count": True},
    ],
    ids=lambda change: f"{next(iter(change))}={next(iter(change.values()))!r}",
)
def test_a_bad_argument_is_a_failure_the_model_can_read(change: dict[str, Any]) -> None:
    with pytest.raises(ToolFailure) as excinfo:
        build_proposal(**{**ARGS, **change})

    assert excinfo.value.code == "invalid_argument"
    assert excinfo.value.hint


def test_an_approval_detail_hashes_to_the_proposal_it_shows() -> None:
    proposal = build_proposal(**ARGS)
    shown = {"v": 1, **proposal.as_dict()}

    assert approved_digest({"proposal": shown}) == proposal.sha256
    assert approved_digest({"proposal": {**shown, "passenger_count": 3}}) != proposal.sha256


@pytest.mark.parametrize(
    "detail",
    [{}, {"proposal": None}, {"proposal": {}}, {"proposal": "SK1429"}, {"proposal": [1, 2]}],
)
def test_a_detail_that_shows_no_proposal_has_no_digest(detail: dict[str, Any]) -> None:
    assert approved_digest(detail) is None
