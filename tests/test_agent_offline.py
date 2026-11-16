"""The offline run, end to end: the real loop against the real gate, with no model.

Nothing between the scripted planner and the operations API's ledger is mocked.
The planner is a script; the tool dispatch, the token exchange, the approval gate
and the transcript are the code that runs in production.

Four claims:

* **Deterministic.** The same scenario gives the same bytes, and a different
  scenario does not.
* **No approval, no write.** Counted by the mock operations API, which is the
  only party that knows whether a write happened.
* **A repeat is the original result.** Two submissions, one write.
* **Nothing secret and nobody who flies reaches the transcript.**
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from agent.__main__ import main
from agent.approval import ApprovalDecision, ApprovalRequest
from agent.clock import LogicalClock
from agent.localstack import FixtureApprover, local_stack
from agent.loop import run_agent
from agent.offline import OfflineProvider
from agent.provider import ToolCall, Turn
from agent.runner import RunResult, run_scenario
from agent.scenarios import (
    Scenario,
    ScenarioInvalid,
    load_scenario,
    parse_scenario,
    scenario_ids,
)
from agent.transcript import load
from flightops_mcp.rebooking import build_proposal

from .conftest import LogCapture
from .rebookkit import ARGS
from .test_agent_loop import ScriptedPlanner
from .test_rebooking_gate import CANARY_ROWS

AGENT_DIR = Path(__file__).parent.parent / "agent"
PROPOSAL = {"v": 1, **build_proposal(**ARGS).as_dict()}
NOTHING_WRITTEN = {"rebooking_requests": 0, "rebookings_written": 0}


async def offline(name: str | Scenario, **stack: Any) -> RunResult:
    scenario = load_scenario(name) if isinstance(name, str) else name
    return await run_scenario(
        scenario,
        OfflineProvider(scenario.policy, scenario.params),
        clock=LogicalClock(scenario.clock_start),
        **stack,
    )


def variant(name: str, **changes: Any) -> Scenario:
    """A bundled scenario with some fields replaced, as a scenario of its own."""
    raw = json.loads((AGENT_DIR / "scenarios" / f"{name}.json").read_bytes())
    raw.update(changes)
    return parse_scenario(raw)


def results(run: RunResult) -> list[tuple[str, str, str | None]]:
    return [
        (step["tool"], step["result"]["class"], step["result"]["code"])
        for step in run.transcript.steps
    ]


# --- deterministic ---------------------------------------------------------------
@pytest.mark.parametrize("name", scenario_ids())
async def test_the_same_scenario_twice_is_the_same_transcript_byte_for_byte(name: str) -> None:
    first = await offline(name)
    second = await offline(name)

    assert first.transcript.to_bytes() == second.transcript.to_bytes()
    assert first.transcript.content_hash == second.transcript.content_hash
    assert first.ops_api == second.ops_api


async def test_different_scenarios_have_different_hashes() -> None:
    """The check above cannot fail if the hash ignores the run. This one can."""
    hashes = {name: (await offline(name)).transcript.content_hash for name in scenario_ids()}

    assert len(set(hashes.values())) == len(hashes), hashes


async def test_one_changed_parameter_changes_the_run_and_its_hash() -> None:
    """Same question, same policy, another cohort: the plan varies from the scenario."""
    base = load_scenario("rebook-approved")
    other = variant(
        "rebook-approved",
        offline={"policy": "rebook_cohort", "params": {**base.params, "cohort": "connecting"}},
    )

    first, second = await offline(base), await offline(other)

    assert first.transcript.steps[1]["arguments"]["passenger_count"] == 2
    assert second.transcript.steps[1]["arguments"]["passenger_count"] == 3
    assert first.transcript.content_hash != second.transcript.content_hash
    assert (
        first.transcript.body["scenario"]["sha256"] != second.transcript.body["scenario"]["sha256"]
    )


async def test_the_transcript_carries_no_wall_clock_time() -> None:
    """Every instant in an offline transcript is the scenario's clock, counted in seconds."""
    run = await offline("rebook-approved")

    assert run.transcript.body["started_at"] == "2026-11-16T09:00:00+00:00"
    assert run.transcript.approvals[0]["decided_at"] == "2026-11-16T09:00:01+00:00"


def test_the_loop_reads_no_clock_and_no_entropy_of_its_own() -> None:
    """Structural: a new `time.time()` or `random` in the loop is a failed test, not a flaky hash.

    The stack fixture generates per-run secrets and the CLI names the wall clock
    for model runs. Nothing else in the package may reach for either.
    """
    exempt = {"localstack.py": {"secrets"}, "__main__.py": {"time"}}
    forbidden = {"time", "random", "secrets", "uuid", "datetime.now", "datetime.utcnow"}
    found: list[str] = []
    scanned = 0
    for path in sorted(AGENT_DIR.glob("*.py")):
        scanned += 1
        allowed = exempt.get(path.name, set())
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [(node.module or "").split(".")[0]]
            elif isinstance(node, ast.Attribute):
                names = [ast.unparse(node)]
            found.extend(
                f"{path.name}:{node.lineno} {name}"
                for name in names
                if name in forbidden and name not in allowed
            )

    assert scanned >= 12, "the scan found fewer modules than exist; fix the scan"
    assert found == []


def test_every_http_client_the_agent_opens_installs_the_passthrough_guard() -> None:
    """Rule 7 applies to this package too: the scan in `test_no_passthrough` covers only `src`."""
    unguarded: list[str] = []
    clients = 0
    for path in sorted(AGENT_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and ast.unparse(node.func) == "httpx.AsyncClient"):
                continue
            clients += 1
            hooks = next((kw.value for kw in node.keywords if kw.arg == "event_hooks"), None)
            if hooks is None or "refuse_inbound_token" not in ast.unparse(hooks):
                unguarded.append(f"{path.name}:{node.lineno}")

    assert clients >= 2, "the scan found fewer clients than exist; fix the scan"
    assert unguarded == []


# --- the approved path -------------------------------------------------------------
async def test_an_approved_rebooking_executes_once_and_the_transcript_says_who_approved() -> None:
    run = await offline("rebook-approved")

    assert results(run) == [
        ("flight_passenger_impact", "ok", None),
        ("propose_rebooking", "ok", None),
    ]
    assert run.transcript.steps[1]["arguments"] == ARGS, "the count came from the first tool"
    assert run.transcript.steps[1]["result"]["replayed"] is False
    assert run.transcript.approvals == [
        {
            "step": 1,
            "tool": "propose_rebooking",
            "proposal": PROPOSAL,
            "proposal_sha256": build_proposal(**ARGS).sha256,
            "action": "accept",
            "approver": "duty-manager-7",
            "decided_at": "2026-11-16T09:00:01+00:00",
            "grant_presented": True,
            "used": True,
            "confirmed_approver": "duty-manager-7",
        }
    ]
    assert run.transcript.outcome["status"] == "completed"
    assert "Rebooked 2 passenger(s)" in run.transcript.outcome["answer"]
    assert run.ops_api == {"rebooking_requests": 1, "rebookings_written": 1}
    assert run.transcript.budget["spent"]["tool_calls"] == 2


# --- no approval, no write -----------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "code", "action"),
    [
        ("rebook-unanswered", "approval_required", "unanswered"),
        ("rebook-declined", "approval_declined", "decline"),
        ("rebook-self-approved", "approver_is_proposer", "accept"),
    ],
)
async def test_without_a_valid_approval_the_write_is_refused_and_the_api_is_never_asked(
    name: str, code: str, action: str
) -> None:
    run = await offline(name)

    assert results(run)[-1] == ("propose_rebooking", "error", code)
    (approval,) = run.transcript.approvals
    assert approval["action"] == action
    assert approval["used"] is False
    assert approval["confirmed_approver"] is None
    assert run.ops_api == NOTHING_WRITTEN, "refused by the server, not merely unrecorded"
    assert run.transcript.outcome["status"] == "completed"
    assert run.transcript.outcome["answer"].startswith("Nothing was rebooked.")
    assert len(run.transcript.steps) == 2, "a refusal is reported, not retried"


async def test_an_unanswered_approval_is_recorded_as_nobody_and_nothing() -> None:
    (approval,) = (await offline("rebook-unanswered")).transcript.approvals

    assert approval["approver"] is None
    assert approval["grant_presented"] is False
    assert approval["proposal"] == PROPOSAL, "what went unapproved is still on the record"


async def test_an_agent_with_no_approval_channel_is_refused_before_anyone_is_asked() -> None:
    """No approver, so the client does not claim it can relay a request. The server refuses."""
    run = await offline("rebook-no-approval-channel")

    assert results(run)[-1] == ("propose_rebooking", "error", "approval_required")
    assert run.transcript.approvals == [], "there was no approval round to record"
    assert run.ops_api == NOTHING_WRITTEN


async def test_a_cancelled_approval_writes_nothing() -> None:
    run = await offline(
        variant("rebook-declined", approval={"action": "cancel", "approver": "duty-manager-7"})
    )

    assert results(run)[-1] == ("propose_rebooking", "error", "approval_declined")
    assert run.transcript.approvals[0]["action"] == "cancel"
    assert run.ops_api == NOTHING_WRITTEN


class SaysYes:
    """An approval channel that agrees and has no grant. This is the agent approving itself."""

    def __init__(self, grant: str | None) -> None:
        self._grant = grant

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        assert request.proposal, "it was shown a proposal, and agreed without a grant for it"
        return ApprovalDecision(action="accept", approver="duty-manager-7", grant=self._grant)


@pytest.mark.parametrize(
    ("grant", "code", "presented"),
    [
        (None, "approval_required", False),
        ("yes, approved", "approval_invalid", True),
        ("eyJhbGciOiJub25lIn0.e30.", "approval_invalid", True),
    ],
    ids=["no-grant", "a-sentence", "an-unsigned-token"],
)
async def test_an_accept_the_idp_did_not_sign_is_not_an_approval(
    grant: str | None, code: str, presented: bool
) -> None:
    """ADR 0003, from the agent's side: the loop cannot manufacture what the gate accepts."""
    run = await offline("rebook-approved", approver=SaysYes(grant))

    assert results(run)[-1] == ("propose_rebooking", "error", code)
    (approval,) = run.transcript.approvals
    assert (approval["action"], approval["grant_presented"], approval["used"]) == (
        "accept",
        presented,
        False,
    )
    assert run.ops_api == NOTHING_WRITTEN


async def test_a_user_without_the_write_scope_gets_a_refusal_as_data() -> None:
    """The scope challenge is an HTTP 403. The loop must turn it into a step, not a crash."""
    run = await offline(
        variant(
            "rebook-approved",
            user={"sub": "dispatcher-3", "scopes": ["flightops.passengers.read"]},
        )
    )

    assert results(run)[-1] == ("propose_rebooking", "error", "insufficient_scope")
    assert run.transcript.approvals == [], "nobody is asked to approve what cannot be proposed"
    assert run.ops_api == NOTHING_WRITTEN
    assert run.transcript.outcome["answer"].startswith("Nothing was rebooked.")


# --- a repeat is the original result ---------------------------------------------------
async def test_submitting_the_same_rebooking_twice_writes_once() -> None:
    """Both submissions reach the operations API. Its ledger, not the client, says one write."""
    run = await offline("rebook-replayed")

    assert results(run) == [
        ("flight_passenger_impact", "ok", None),
        ("propose_rebooking", "ok", None),
        ("propose_rebooking", "ok", None),
    ]
    first, second = run.transcript.steps[1], run.transcript.steps[2]
    assert first["arguments"] == second["arguments"]
    assert (first["result"]["replayed"], second["result"]["replayed"]) == (False, True)
    assert run.ops_api == {"rebooking_requests": 2, "rebookings_written": 1}
    assert [approval["used"] for approval in run.transcript.approvals] == [True, True], (
        "a replay still has to pass the gate"
    )
    assert (
        "1 of 2 submissions were answered with the original result"
        in (run.transcript.outcome["answer"])
    )


# --- the budget, end to end ------------------------------------------------------------
async def test_a_run_over_its_own_budget_stops_before_the_write_and_says_why() -> None:
    run = await offline("rebook-over-budget")

    assert run.transcript.tool_sequence() == ["flight_passenger_impact"]
    assert run.transcript.outcome == {
        "status": "stopped",
        "reason": "budget_exhausted",
        "detail": "agent budget exhausted (tool_calls): calling propose_rebooking would be "
        "tool call 2, over the limit of 1",
        "answer": "",
        "unexecuted_tool_calls": 1,
    }
    assert run.transcript.budget["exhausted"] == "tool_calls"
    assert run.transcript.approvals == []
    assert run.ops_api == NOTHING_WRITTEN


async def test_the_agent_stops_on_its_own_credit_limit_while_the_server_has_plenty() -> None:
    """Two ledgers see the same call. The run ends on the agent's, and the server's is untouched
    by that decision: it is not asked whether the agent may continue, and it is not drawn down
    to let it."""
    scenario = variant(
        "weather-unknown",
        user={"sub": "dispatcher-3", "scopes": ["flightops.traffic.read"]},
        budget={"upstream_credits": 0},
    )
    clock = LogicalClock(scenario.clock_start)
    traffic = {"airport": "ESGG"}
    planner = ScriptedPlanner(
        Turn(
            tool_calls=(
                ToolCall(id="c1", name="airport_live_traffic", arguments=traffic),
                ToolCall(id="c2", name="airport_live_traffic", arguments=traffic),
            ),
            stop="tool_calls",
        )
    )
    async with local_stack(scenario, clock=clock) as stack:
        transcript = await run_agent(
            scenario_id=scenario.id,
            scenario_sha256=scenario.sha256,
            question=scenario.question,
            provider=planner,
            tools=stack.tools,
            budget=scenario.budget,
            clock=clock,
        )
        server_spent = stack.runtime.opensky.credits_spent_total
        server_limit = stack.runtime.settings.credit_budget_per_hour

    assert transcript.steps[0]["result"]["class"] == "ok"
    assert transcript.budget["exhausted"] == "upstream_credits"
    assert transcript.outcome["reason"] == "budget_exhausted"
    assert transcript.outcome["unexecuted_tool_calls"] == 1
    assert transcript.budget["spent"]["upstream_credits"] == server_spent == 1, (
        "both ledgers counted the one call that was made"
    )
    assert server_spent < server_limit, "the server would have served the second call"


# --- unknowns survive to the transcript -------------------------------------------------
async def test_an_upstream_with_no_data_is_recorded_as_unknown_not_as_normal() -> None:
    run = await offline("weather-unknown")

    assert results(run) == [("resolve_airport", "ok", None), ("airport_weather", "ok", None)]
    assert run.transcript.steps[0]["result"]["unknowns"] == 0
    assert run.transcript.steps[1]["result"]["unknowns"] >= 1
    assert run.transcript.steps[1]["arguments"] == {"airport": "ESGG"}
    assert "unknown" in run.transcript.outcome["answer"]
    assert "not evidence that conditions are normal" in run.transcript.outcome["answer"]


# --- nothing secret, and nobody who flies ------------------------------------------------
async def test_the_transcript_holds_no_grant_no_token_and_no_passenger(
    logs: LogCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plant passengers that are all canary, capture the real grant, and search for both."""
    grants: list[str] = []

    class RecordingApprover(FixtureApprover):
        """The fixture approver, remembering the grant it obtained so the test can look for it."""

        async def decide(self, request: ApprovalRequest) -> ApprovalDecision | None:
            decision = await super().decide(request)
            if decision is not None and decision.grant:
                grants.append(decision.grant)
            return decision

    monkeypatch.setattr("agent.localstack.FixtureApprover", RecordingApprover)

    run = await offline("rebook-approved", manifests={"SK1429": CANARY_ROWS})

    (grant,) = grants
    assert grant.count(".") == 2, "the approver under test obtained a real signed grant"
    assert run.ops_api == {"rebooking_requests": 1, "rebookings_written": 1}
    assert run.transcript.steps[1]["arguments"]["passenger_count"] == 2, "the canaries were moved"
    written = run.transcript.to_bytes().decode()
    assert "CANARY" not in written, "a passenger reached the transcript"
    assert grant not in written, "the approval grant was written down"
    assert "eyJ" not in written, "something token-shaped is in the transcript"
    # Redaction would have hidden a leak. There must have been nothing for it to hide.
    assert "[REDACTED]" not in written
    assert "approval_token" not in written and "request_state" not in written
    assert grant not in logs.text and "CANARY" not in logs.text
    assert grant not in repr(ApprovalDecision(action="accept", approver="x", grant=grant))


# --- scenarios ---------------------------------------------------------------------------
def test_every_bundled_scenario_loads_and_names_itself() -> None:
    assert len(scenario_ids()) >= 2
    for name in scenario_ids():
        scenario = load_scenario(name)
        assert scenario.id == name
        assert len(scenario.sha256) == 64


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"clock": {"start": "2026-11-16T09:00:00"}}, "needs a UTC offset"),
        ({"approval": {"action": "approve", "approver": "x"}}, "approval.action must be one of"),
        ({"budget": {"patience": 3}}, "malformed scenario"),
        ({"user": {"sub": "dispatcher-3"}}, "malformed scenario"),
    ],
)
def test_a_scenario_that_cannot_be_run_as_written_is_refused(
    change: dict[str, Any], problem: str
) -> None:
    with pytest.raises(ScenarioInvalid, match=problem):
        variant("rebook-approved", **change)


def test_an_unknown_scenario_or_policy_is_refused_by_name() -> None:
    with pytest.raises(ScenarioInvalid, match="no scenario 'rebook-everyone'"):
        load_scenario("rebook-everyone")
    with pytest.raises(ValueError, match="unknown offline policy 'improvise'"):
        OfflineProvider("improvise", {})


async def test_a_policy_asking_for_a_tool_the_server_does_not_offer_stops_the_run() -> None:
    """A broken script is a stated failure, not a call sent into the dark."""
    scenario = load_scenario("weather-unknown")
    clock = LogicalClock(scenario.clock_start)

    class OneTool:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        async def list_tools(self) -> Any:
            return tuple(t for t in await self._inner.list_tools() if t.name == "resolve_airport")

        async def call(self, call: ToolCall) -> Any:
            return await self._inner.call(call)

    async with local_stack(scenario, clock=clock) as stack:
        transcript = await run_agent(
            scenario_id=scenario.id,
            scenario_sha256=scenario.sha256,
            question=scenario.question,
            provider=OfflineProvider(scenario.policy, scenario.params),
            tools=OneTool(stack.tools),
            budget=scenario.budget,
            clock=clock,
        )

    assert transcript.tool_sequence() == ["resolve_airport"]
    assert transcript.outcome["reason"] == "provider_error"
    assert transcript.outcome["detail"] == "tool_not_offered: the policy asked for airport_weather"


# --- the public entry point -----------------------------------------------------------------
def test_the_cli_runs_a_scenario_twice_to_identical_files_and_a_third_to_another(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """What the CI job does, in process: run, run again, compare, then run something else."""
    paths = [tmp_path / name for name in ("a.json", "b.json", "c.json")]
    names = ["rebook-approved", "rebook-approved", "rebook-declined"]

    summaries = []
    for path, name in zip(paths, names, strict=True):
        assert main(["run", "--scenario", name, "--backend", "offline", "--out", str(path)]) == 0
        summaries.append(json.loads(capsys.readouterr().out.strip().splitlines()[-1]))

    assert paths[0].read_bytes() == paths[1].read_bytes()
    assert paths[0].read_bytes() != paths[2].read_bytes()
    hashes = []
    for path in paths:
        assert main(["hash", str(path)]) == 0
        hashes.append(capsys.readouterr().out.strip())
    assert hashes[0] == hashes[1] != hashes[2]
    assert [summary["content_hash"] for summary in summaries] == hashes
    assert summaries[0]["ops_api"] == {"rebooking_requests": 1, "rebookings_written": 1}
    assert summaries[2]["ops_api"] == NOTHING_WRITTEN
    assert load(paths[0]).scenario_id == "rebook-approved"


def test_the_cli_refuses_a_tampered_transcript_and_an_unknown_scenario_as_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "run.json"
    assert main(["run", "--scenario", "weather-unknown", "--out", str(path)]) == 0
    capsys.readouterr()
    path.write_bytes(path.read_bytes().replace(b"ESGG", b"ESSA"))

    assert main(["hash", str(path)]) == 2
    refused = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert refused == {
        "ok": False,
        "error": {"code": "invalid_transcript", "message": "content_hash does not match the body"},
    }

    assert main(["run", "--scenario", "rebook-everyone"]) == 2
    assert json.loads(capsys.readouterr().err.strip().splitlines()[-1])["error"]["code"] == (
        "cannot_run"
    )
    assert main(["hash", str(tmp_path / "missing.json")]) == 2
    assert json.loads(capsys.readouterr().err.strip().splitlines()[-1])["error"]["code"] == (
        "unreadable"
    )
