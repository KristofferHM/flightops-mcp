"""The run transcript: one JSON document per run, and the loader that reads it back.

The evaluation harness consumes this file, so its shape is a contract:

* **A canonical body and a hash over it.** Sorted keys, no whitespace, ASCII:
  one byte string per body. Two runs are the same run exactly when their
  `content_hash` is equal, and the file on disk is written from the same
  canonical rules, so equal hashes mean equal bytes.
* **Result classes, not results.** A step records which tool was called, with
  what, and how it ended (`ok`, an error code, how many unknowns). The payload
  itself is not kept: it carries the server's wall clock, and nothing in an
  evaluation should depend on that.
* **Nothing secret, nobody who flies.** The body passes through the same
  redaction as the logs before it is hashed. An approval is recorded as who
  decided, when, and for which payload. The grant is never written.

`load` refuses a document it cannot vouch for: wrong schema, a missing field,
or a body that no longer matches its hash.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from flightops_mcp.telemetry import redact

SCHEMA: Final = "flightops.agent.transcript/v1"
BACKENDS: Final = ("offline", "anthropic", "openai-compatible")
RESULT_CLASSES: Final = ("ok", "error")
APPROVAL_ACTIONS: Final = ("accept", "decline", "cancel", "unanswered")
OUTCOME_STATUSES: Final = ("completed", "stopped")
OUTCOME_REASONS: Final = (
    "answered",
    "budget_exhausted",
    "model_refused",
    "model_truncated",
    "provider_error",
    "server_unavailable",
)
BUDGET_DIMENSIONS: Final = ("turns", "tool_calls", "model_tokens", "upstream_credits")
SPENT_FIELDS: Final = ("turns", "tool_calls", "input_tokens", "output_tokens", "upstream_credits")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HASH_PREFIX: Final = "sha256:"


class TranscriptInvalid(Exception):
    """A document that is not a transcript this version can vouch for."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def canonical(value: Any) -> bytes:
    """Sorted keys, no whitespace, ASCII, no NaN: one byte string per value."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class Transcript:
    """A validated transcript. Build one with `seal`, read one with `load`."""

    body: dict[str, Any]
    content_hash: str

    @classmethod
    def seal(cls, body: dict[str, Any]) -> Transcript:
        """Redact, validate and hash `body`. Raises `TranscriptInvalid` on a malformed one.

        Redaction runs before the hash, so the hash is of what is written. It is
        the second line: the loop already keeps grants and tool payloads out.
        """
        cleaned: dict[str, Any] = redact(body)
        problems = body_problems(cleaned)
        if problems:
            raise TranscriptInvalid(problems)
        return cls(body=cleaned, content_hash=_HASH_PREFIX + digest(cleaned))

    def document(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "content_hash": self.content_hash, "body": self.body}

    def to_bytes(self) -> bytes:
        """The file. Deterministic for a given body, on every platform."""
        text = json.dumps(self.document(), sort_keys=True, indent=2, ensure_ascii=True)
        return (text + "\n").encode("ascii")

    def write(self, path: Path) -> None:
        # Bytes, not text: text mode would turn "\n" into "\r\n" on Windows and
        # two machines would disagree about a file with the same hash.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.to_bytes())

    # --- what the evaluation harness reads ------------------------------------
    @property
    def scenario_id(self) -> str:
        return str(self.body["scenario"]["id"])

    @property
    def backend(self) -> str:
        return str(self.body["backend"])

    @property
    def model(self) -> str:
        return str(self.body["model"])

    @property
    def steps(self) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = self.body["steps"]
        return steps

    @property
    def approvals(self) -> list[dict[str, Any]]:
        approvals: list[dict[str, Any]] = self.body["approvals"]
        return approvals

    @property
    def budget(self) -> dict[str, Any]:
        budget: dict[str, Any] = self.body["budget"]
        return budget

    @property
    def outcome(self) -> dict[str, Any]:
        outcome: dict[str, Any] = self.body["outcome"]
        return outcome

    def tool_sequence(self) -> list[str]:
        return [str(step["tool"]) for step in self.steps]


def loads(raw: bytes | str) -> Transcript:
    """Parse and verify a transcript document. Raises `TranscriptInvalid`."""
    try:
        document = json.loads(raw)
    except ValueError:
        raise TranscriptInvalid(["not JSON"]) from None
    if not isinstance(document, dict):
        raise TranscriptInvalid(["the document is not an object"])
    if document.get("schema") != SCHEMA:
        raise TranscriptInvalid([f"schema is {document.get('schema')!r}, expected {SCHEMA!r}"])
    body = document.get("body")
    if not isinstance(body, dict):
        raise TranscriptInvalid(["body is missing or not an object"])
    problems = body_problems(body)
    if problems:
        raise TranscriptInvalid(problems)
    expected = _HASH_PREFIX + digest(body)
    if document.get("content_hash") != expected:
        raise TranscriptInvalid(["content_hash does not match the body"])
    return Transcript(body=body, content_hash=expected)


def load(path: Path) -> Transcript:
    return loads(path.read_bytes())


# --- validation ----------------------------------------------------------------
def body_problems(body: dict[str, Any]) -> list[str]:
    """Everything wrong with `body`, as sentences. Empty means it is a transcript."""
    problems: list[str] = []
    scenario = body.get("scenario")
    if not isinstance(scenario, dict) or not _text(scenario.get("id")):
        problems.append("scenario.id is missing")
    elif not _sha256(scenario.get("sha256")):
        problems.append("scenario.sha256 is not a SHA-256")
    if body.get("backend") not in BACKENDS:
        problems.append(f"backend must be one of {', '.join(BACKENDS)}")
    if not _text(body.get("model")):
        problems.append("model is missing")
    if not _text(body.get("started_at")):
        problems.append("started_at is missing")

    steps = body.get("steps")
    if not isinstance(steps, list):
        problems.append("steps is not a list")
        steps = []
    for position, step in enumerate(steps):
        problems.extend(_step_problems(position, step))

    approvals = body.get("approvals")
    if not isinstance(approvals, list):
        problems.append("approvals is not a list")
        approvals = []
    for position, approval in enumerate(approvals):
        problems.extend(_approval_problems(position, approval, steps))

    problems.extend(_budget_problems(body.get("budget")))
    problems.extend(_outcome_problems(body.get("outcome")))
    return problems


def _step_problems(position: int, step: Any) -> list[str]:
    where = f"steps[{position}]"
    if not isinstance(step, dict):
        return [f"{where} is not an object"]
    problems: list[str] = []
    if step.get("index") != position:
        problems.append(f"{where}.index must be {position}: steps are ordered")
    if not _count(step.get("turn"), minimum=1):
        problems.append(f"{where}.turn must be a positive integer")
    if not _text(step.get("tool")):
        problems.append(f"{where}.tool is missing")
    if not isinstance(step.get("arguments"), dict):
        problems.append(f"{where}.arguments is not an object")
    result = step.get("result")
    if not isinstance(result, dict) or result.get("class") not in RESULT_CLASSES:
        return [*problems, f"{where}.result.class must be one of {', '.join(RESULT_CLASSES)}"]
    code = result.get("code")
    if result["class"] == "error" and not _text(code):
        problems.append(f"{where}.result.code is required for an error")
    if result["class"] == "ok" and code is not None:
        problems.append(f"{where}.result.code must be null for ok")
    if not _count(result.get("unknowns")):
        problems.append(f"{where}.result.unknowns must be a count")
    if result.get("replayed") not in (None, True, False):
        problems.append(f"{where}.result.replayed must be a boolean or null")
    return problems


def _approval_problems(position: int, approval: Any, steps: list[Any]) -> list[str]:
    where = f"approvals[{position}]"
    if not isinstance(approval, dict):
        return [f"{where} is not an object"]
    problems: list[str] = []
    step = approval.get("step")
    if not isinstance(step, int) or isinstance(step, bool) or not 0 <= step < len(steps):
        problems.append(f"{where}.step does not name a step")
    elif isinstance(steps[step], dict) and steps[step].get("tool") != approval.get("tool"):
        problems.append(f"{where}.tool is not the tool of the step it names")
    proposal = approval.get("proposal")
    if not isinstance(proposal, dict) or not proposal:
        problems.append(f"{where}.proposal is missing")
    elif approval.get("proposal_sha256") != digest(proposal):
        # Which payload was approved is the point of the record. A digest that
        # does not match the payload beside it describes neither.
        problems.append(f"{where}.proposal_sha256 is not the digest of its proposal")
    if approval.get("action") not in APPROVAL_ACTIONS:
        problems.append(f"{where}.action must be one of {', '.join(APPROVAL_ACTIONS)}")
    for name in ("approver", "confirmed_approver"):
        if approval.get(name) is not None and not _text(approval.get(name)):
            problems.append(f"{where}.{name} must be a string or null")
    if not _text(approval.get("decided_at")):
        problems.append(f"{where}.decided_at is missing")
    presented, used = approval.get("grant_presented"), approval.get("used")
    if not isinstance(presented, bool) or not isinstance(used, bool):
        problems.append(f"{where}.grant_presented and .used must be booleans")
    elif used and not presented:
        problems.append(f"{where}.used is true for a grant that was never presented")
    return problems


def _budget_problems(budget: Any) -> list[str]:
    if not isinstance(budget, dict):
        return ["budget is missing"]
    problems: list[str] = []
    limits, spent = budget.get("limits"), budget.get("spent")
    if not isinstance(limits, dict) or not all(_count(limits.get(n)) for n in BUDGET_DIMENSIONS):
        problems.append(f"budget.limits needs a count for each of {', '.join(BUDGET_DIMENSIONS)}")
    if not isinstance(spent, dict) or not all(_count(spent.get(n)) for n in SPENT_FIELDS):
        problems.append(f"budget.spent needs a count for each of {', '.join(SPENT_FIELDS)}")
    if budget.get("exhausted") not in (None, *BUDGET_DIMENSIONS):
        problems.append("budget.exhausted must be null or a budget dimension")
    return problems


def _outcome_problems(outcome: Any) -> list[str]:
    if not isinstance(outcome, dict):
        return ["outcome is missing"]
    problems: list[str] = []
    if outcome.get("status") not in OUTCOME_STATUSES:
        problems.append(f"outcome.status must be one of {', '.join(OUTCOME_STATUSES)}")
    if outcome.get("reason") not in OUTCOME_REASONS:
        problems.append(f"outcome.reason must be one of {', '.join(OUTCOME_REASONS)}")
    elif (outcome.get("status") == "completed") != (outcome["reason"] == "answered"):
        problems.append("outcome.status is completed exactly when the reason is answered")
    for name in ("detail", "answer"):
        if not isinstance(outcome.get(name), str):
            problems.append(f"outcome.{name} must be a string")
    if not _count(outcome.get("unexecuted_tool_calls")):
        problems.append("outcome.unexecuted_tool_calls must be a count")
    return problems


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def _count(value: Any, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum
