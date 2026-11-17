"""The transcript: a contract the evaluation harness reads, and what must not be in it.

Three claims:

* **One body, one byte string, one hash.** Key order does not matter, any field
  does, and a file that was edited after it was written is refused.
* **A loader that vouches.** `load` returns a transcript or says what is wrong
  with it. This is the minimal harness-side reader Phase 4 builds on.
* **No secret and nobody who flies.** Every field a model or an upstream could
  put free text into gets a canary.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from agent.transcript import (
    SCHEMA,
    Transcript,
    TranscriptInvalid,
    body_problems,
    canonical,
    digest,
    load,
    loads,
)
from flightops_mcp.rebooking import build_proposal

from .authkit import signing_key
from .rebookkit import ARGS

PROPOSAL = {"v": 1, **build_proposal(**ARGS).as_dict()}
CANARY_JWT = signing_key("canary").mint(subject="CANARY-SUBJECT")


def a_body() -> dict[str, Any]:
    """A complete, valid body: one read, one approved write."""
    return {
        "scenario": {"id": "rebook-approved", "sha256": "a" * 64},
        "backend": "offline",
        "model": "offline",
        "started_at": "2026-11-16T09:00:00+00:00",
        "steps": [
            {
                "index": 0,
                "turn": 1,
                "tool": "flight_passenger_impact",
                "arguments": {"flight": "SK1429"},
                "result": {"class": "ok", "code": None, "unknowns": 0, "replayed": None},
            },
            {
                "index": 1,
                "turn": 2,
                "tool": "propose_rebooking",
                "arguments": dict(ARGS),
                "result": {"class": "ok", "code": None, "unknowns": 0, "replayed": False},
            },
        ],
        "approvals": [
            {
                "step": 1,
                "tool": "propose_rebooking",
                "proposal": dict(PROPOSAL),
                "proposal_sha256": digest(PROPOSAL),
                "action": "accept",
                "approver": "duty-manager-7",
                "decided_at": "2026-11-16T09:00:01+00:00",
                "grant_presented": True,
                "used": True,
                "confirmed_approver": "duty-manager-7",
            }
        ],
        "budget": {
            "limits": {"turns": 6, "tool_calls": 6, "model_tokens": 60000, "upstream_credits": 20},
            "spent": {
                "turns": 3,
                "tool_calls": 2,
                "input_tokens": 0,
                "output_tokens": 0,
                "upstream_credits": 0,
            },
            "exhausted": None,
        },
        "outcome": {
            "status": "completed",
            "reason": "answered",
            "detail": "",
            "answer": "Rebooked 2 passenger(s).",
            "unexecuted_tool_calls": 0,
        },
    }


def at(body: dict[str, Any], path: str) -> tuple[Any, str | int]:
    """The container and last key of a dotted path, so a test can change one field."""
    *parents, last = path.split(".")
    node: Any = body
    for part in parents:
        node = node[int(part)] if part.isdigit() else node[part]
    return node, int(last) if last.isdigit() else last


# --- one body, one byte string, one hash -----------------------------------------
def test_a_sealed_transcript_reads_back_as_the_same_transcript() -> None:
    sealed = Transcript.seal(a_body())

    read = loads(sealed.to_bytes())

    assert read == sealed
    assert read.content_hash.startswith("sha256:")
    assert json.loads(sealed.to_bytes())["schema"] == SCHEMA


def test_key_order_does_not_change_the_bytes_or_the_hash() -> None:
    """Dicts remember insertion order. The transcript must not."""
    body = a_body()
    reversed_keys = {key: body[key] for key in reversed(list(body))}
    reversed_keys["steps"] = [
        {key: step[key] for key in reversed(list(step))} for step in body["steps"]
    ]

    first, second = Transcript.seal(body), Transcript.seal(reversed_keys)

    assert list(body) != list(reversed_keys), "the two bodies must really differ in order"
    assert first.to_bytes() == second.to_bytes()
    assert first.content_hash == second.content_hash


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("scenario.id", "rebook-declined"),
        ("scenario.sha256", "b" * 64),
        ("model", "claude-opus-5-5"),
        ("started_at", "2026-11-16T09:00:05+00:00"),
        ("steps.0.arguments.flight", "SK1430"),
        ("steps.1.result.replayed", True),
        ("steps.0.result.unknowns", 1),
        ("approvals.0.approver", "station-manager-2"),
        ("approvals.0.decided_at", "2026-11-16T09:00:02+00:00"),
        ("budget.spent.input_tokens", 1),
        ("budget.limits.turns", 7),
        ("outcome.answer", "Rebooked 3 passenger(s)."),
    ],
)
def test_changing_any_one_field_changes_the_hash(path: str, value: Any) -> None:
    """The hash is evidence only if it moves when the run does."""
    changed = a_body()
    node, key = at(changed, path)
    assert node[key] != value
    node[key] = value

    assert Transcript.seal(changed).content_hash != Transcript.seal(a_body()).content_hash


def test_the_file_is_the_same_bytes_on_every_platform(tmp_path: Path) -> None:
    """Text mode would write CRLF on Windows and a different file for the same hash."""
    path = tmp_path / "nested" / "run.json"
    sealed = Transcript.seal(a_body())

    sealed.write(path)

    raw = path.read_bytes()
    assert raw == sealed.to_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"}\n")
    assert raw.isascii()


def test_canonical_form_refuses_what_has_no_single_spelling() -> None:
    with pytest.raises(ValueError, match="Out of range float"):
        canonical({"value": float("nan")})


def test_the_recorded_proposal_digest_is_the_one_the_server_binds_to() -> None:
    """The transcript's `proposal_sha256` and the server's are the same number."""
    assert digest(PROPOSAL) == build_proposal(**ARGS).sha256


# --- a loader that vouches ---------------------------------------------------------
def test_the_harness_reads_a_run_without_knowing_its_layout(tmp_path: Path) -> None:
    """What an evaluator needs: which tools, in what order, how each ended, how it stopped."""
    path = tmp_path / "run.json"
    Transcript.seal(a_body()).write(path)

    run = load(path)

    assert run.scenario_id == "rebook-approved"
    assert (run.backend, run.model) == ("offline", "offline")
    assert run.tool_sequence() == ["flight_passenger_impact", "propose_rebooking"]
    assert [step["result"]["class"] for step in run.steps] == ["ok", "ok"]
    assert run.steps[1]["arguments"]["passenger_count"] == 2
    (approval,) = run.approvals
    assert approval["used"] is True
    assert approval["proposal"] == PROPOSAL
    assert run.budget["spent"]["tool_calls"] == 2
    assert run.budget["exhausted"] is None
    assert run.outcome["status"] == "completed"


def test_a_transcript_edited_after_it_was_written_is_refused() -> None:
    document = json.loads(Transcript.seal(a_body()).to_bytes())
    document["body"]["steps"][1]["arguments"]["passenger_count"] = 200

    with pytest.raises(TranscriptInvalid, match="content_hash does not match the body"):
        loads(json.dumps(document))


def test_a_hash_edited_to_match_nothing_is_refused() -> None:
    document = json.loads(Transcript.seal(a_body()).to_bytes())
    document["content_hash"] = "sha256:" + "0" * 64

    with pytest.raises(TranscriptInvalid, match="content_hash"):
        loads(json.dumps(document))


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        (b"not json", "not JSON"),
        (b"[]", "not an object"),
        (b'{"schema": "flightops.agent.transcript/v0", "body": {}}', "schema is"),
        (json.dumps({"schema": SCHEMA}).encode(), "body is missing"),
    ],
)
def test_a_document_that_is_not_a_transcript_is_refused(raw: bytes, problem: str) -> None:
    with pytest.raises(TranscriptInvalid, match=problem):
        loads(raw)


@pytest.mark.parametrize(
    "path",
    [
        "scenario",
        "scenario.id",
        "scenario.sha256",
        "backend",
        "model",
        "started_at",
        "steps",
        "steps.0.tool",
        "steps.0.turn",
        "steps.0.arguments",
        "steps.0.result",
        "steps.0.result.unknowns",
        "approvals",
        "approvals.0.proposal",
        "approvals.0.decided_at",
        "approvals.0.used",
        "budget",
        "budget.limits.model_tokens",
        "budget.spent.upstream_credits",
        "outcome",
        "outcome.status",
        "outcome.answer",
        "outcome.unexecuted_tool_calls",
    ],
)
def test_a_body_missing_a_field_is_refused(path: str) -> None:
    body = a_body()
    node, key = at(body, path)
    del node[key]

    assert body_problems(body), f"a body without {path} was accepted"
    with pytest.raises(TranscriptInvalid):
        Transcript.seal(body)


@pytest.mark.parametrize(
    ("path", "value", "problem"),
    [
        ("backend", "gpt", "backend must be one of"),
        ("steps.1.index", 5, "steps are ordered"),
        ("steps.0.result.class", "fine", "result.class"),
        ("steps.0.result.code", "not_found", "code must be null for ok"),
        ("steps.0.turn", 0, "positive integer"),
        ("approvals.0.step", 2, "does not name a step"),
        ("approvals.0.tool", "airport_weather", "not the tool of the step"),
        ("approvals.0.action", "approve", "action must be one of"),
        ("approvals.0.grant_presented", False, "never presented"),
        ("approvals.0.proposal_sha256", "c" * 64, "not the digest of its proposal"),
        ("budget.exhausted", "patience", "budget.exhausted"),
        ("budget.spent.turns", True, "budget.spent"),
        ("outcome.reason", "gave_up", "outcome.reason"),
        ("outcome.status", "stopped", "completed exactly when"),
    ],
)
def test_a_body_that_contradicts_itself_is_refused(path: str, value: Any, problem: str) -> None:
    body = a_body()
    node, key = at(body, path)
    node[key] = value

    assert any(problem in found for found in body_problems(body)), body_problems(body)


def test_an_approval_for_a_payload_other_than_the_one_recorded_is_refused() -> None:
    """Which payload was approved is the record. Swap the payload and it must not load."""
    body = a_body()
    body["approvals"][0]["proposal"]["passenger_count"] = 200

    with pytest.raises(TranscriptInvalid, match="not the digest of its proposal"):
        Transcript.seal(body)


def test_an_error_step_must_name_its_code() -> None:
    body = a_body()
    body["steps"][0]["result"] = {"class": "error", "code": None, "unknowns": 0, "replayed": None}

    assert any("code is required for an error" in found for found in body_problems(body))


# --- no secret, and nobody who flies -------------------------------------------------
@pytest.mark.parametrize(
    "path",
    ["steps.0.arguments.flight", "outcome.answer", "outcome.detail", "approvals.0.approver"],
)
def test_a_token_in_any_free_text_field_does_not_reach_the_file(path: str) -> None:
    """A model can put anything in its arguments and its answer. A grant must not survive there."""
    body = a_body()
    node, key = at(body, path)
    node[key] = f"use {CANARY_JWT} please"

    written = Transcript.seal(body).to_bytes().decode()

    assert CANARY_JWT not in written
    assert "[REDACTED]" in written


def test_a_bearer_string_in_an_answer_is_scrubbed() -> None:
    body = a_body()
    body["outcome"]["answer"] = "I called it with Authorization: Bearer CANARY-BEARER-xyz789"

    assert "CANARY-BEARER" not in Transcript.seal(body).to_bytes().decode()


@pytest.mark.parametrize(
    "key",
    ["approval_token", "request_state", "access_token", "passenger_name", "booking_ref", "email"],
)
def test_secret_and_passenger_fields_are_removed_from_arguments_by_name(key: str) -> None:
    """A planner that passes a grant, or a passenger, as an argument does not get it recorded."""
    body = a_body()
    body["steps"][0]["arguments"][key] = "CANARY-VALUE-123"

    sealed = Transcript.seal(body)

    assert "CANARY" not in sealed.to_bytes().decode()
    assert sealed.steps[0]["arguments"][key] == "[REDACTED]"
    assert sealed.steps[0]["arguments"]["flight"] == "SK1429", "the rest of the call survives"


def test_the_hash_is_of_what_was_written_not_of_what_was_passed_in() -> None:
    """Redaction runs before hashing, so a reader can recompute the hash from the file."""
    body = a_body()
    body["outcome"]["answer"] = f"token {CANARY_JWT}"

    sealed = Transcript.seal(body)

    assert loads(sealed.to_bytes()).content_hash == sealed.content_hash
    assert copy.deepcopy(sealed.body)["outcome"]["answer"] == "token [REDACTED]"
