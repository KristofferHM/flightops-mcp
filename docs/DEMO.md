# Demo: from a clone to an approved rebooking, in under two minutes

Each step is a command to type, followed by what you should see. Every result below was produced on the
machine in [docs/LOAD-TEST.md](LOAD-TEST.md) (a 2-core Windows laptop, git-bash) on 2026-11-17.
From a fresh clone it took **24 s** with a warm uv cache (clone 1.7 s, `uv sync` 13.3 s, first run 9.0 s).
A cold cache adds the package downloads.

**No recording.** This host has no reliable terminal recorder, so there is no GIF or asciinema
cast, and none has been made to look like one. The transcript each run writes is the record: a
signed, hashable JSON file, shown in step 3.

What runs: the real MCP server in Streamable HTTP mode, the local IdP (`services/idp`), and the mock
operations API (`services/ops_api`), all in one process over ASGI. The agent is the `offline`
backend: a scripted policy, not a model, so no API key is needed and the run is deterministic. The
approver is a fixture principal (`duty-manager-7`) holding the approve permission. The approval is
still a grant the IdP signs, checked by the real gate (ADR 0003).

## 0. Get it (≈15 s)

```bash
git clone https://github.com/KristofferHM/flightops-mcp.git && cd flightops-mcp
uv sync
mkdir -p agent-runs
```

You should see: `uv` resolve and install from `uv.lock`, and nothing else. Python 3.12 is required. If
`python` on your machine is older, that is fine: everything below goes through `uv run`.

## 1. Propose a rebooking, have it approved, see it written (≈5 s)

```bash
uv run python -m agent run --scenario rebook-approved --backend offline --out agent-runs/approved.json
```

You should see one JSON line. The parts that matter:

```json
{"ok": true,
 "tools_called": ["flight_passenger_impact", "propose_rebooking"],
 "ops_api": {"rebooking_requests": 1, "rebookings_written": 1},
 "content_hash": "sha256:d2f0645d8c33bf55e0ae0537e4dace62fe662070b2fe0e8cd6e4b4bea948ea45",
 "scenario": "rebook-approved", ...}
```

What happened: the agent read passenger counts for SK1429 on behalf of `dispatcher-3` (an RFC 8693
exchange, counts only). It proposed moving the 2 passengers with tight connections to SK1431. The
server answered with an approval request instead of acting. A different person approved that exact
proposal, and the server wrote it once. `ops_api` is counted by the operations API, not by the agent.

## 2. See who approved what (≈2 s)

```bash
uv run python -c "import json; a = json.load(open('agent-runs/approved.json'))['body']['approvals'][0]; print(a['approver'], a['action'], a['proposal_sha256'][:16], a['proposal'])"
```

You should see:

```
duty-manager-7 accept cb7e1e5434ba9ad2 {'cohort': 'tight_connections', 'flight_date': '2026-11-16', 'from_flight': 'SK1429', 'passenger_count': 2, 'reason_code': 'late_inbound', 'to_flight': 'SK1431', 'v': 1}
```

## 3. Same scenario, same bytes (≈5 s)

```bash
uv run python -m agent run --scenario rebook-approved --backend offline --out agent-runs/again.json > /dev/null
uv run python -m agent hash agent-runs/approved.json
uv run python -m agent hash agent-runs/again.json
```

You should see the same hash twice: `sha256:d2f0645d…48ea45`. Each run has its own signing key, its
own secrets and its own IdP, so equal bytes mean the transcript depends on the scenario and nothing
else. CI checks this on every push.

## 4. Now the gate refuses (≈10 s)

```bash
uv run python -m agent run --scenario rebook-declined --backend offline --out agent-runs/declined.json
uv run python -m agent run --scenario rebook-self-approved --backend offline --out agent-runs/self.json
```

You should see `"ops_api": {"rebooking_requests": 0, "rebookings_written": 0}` in both. In the first,
the approver declined, and the tool answered `approval_declined`. In the second, the proposer tried
to approve their own proposal, and the tool answered `approver_is_proposer` (four eyes, ADR 0003).
The second also logs one warning line, `approval_rejected` with `reason: self_approval`. In neither
case was the operations API asked to write.

## 5. How often is it right? (≈15 s)

```bash
uv run python -m evals.run --offline
```

You should see the table that is also in the README: 58 scenarios, every axis at 100.00 %, `No
scenario fails in this run`, then `OK: no regression against the baseline.` and a run time around
10 s.

---

Total: about 24 s of commands on a warm cache, plus reading time. Next:
[the README](../README.md) for the architecture, [docs/EVALS.md](EVALS.md) for what the table measures,
and [docs/LOAD-TEST.md](LOAD-TEST.md) for the server under load.
