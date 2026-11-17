# ADR 0003: An approval is a signed grant for one payload, not an answer

- Status: accepted
- Date: 2026-11-16
- Phase: 3 (`v0.4.0`)

## Context

Phase 3 adds the first tool that changes anything: `propose_rebooking`. The roadmap requires a human
approval gate built on the 2026-07-28 multi-round-trip flow: the tool answers with
`resultType: "input_required"`, the client fulfils the embedded elicitation, and retries the call with
the response and the echoed `requestState`.

That flow has a property that decides everything below. **The party that carries the approval is the
client, and the client is the agent.** An elicitation response is client-supplied data. The server cannot
tell a person clicking "approve" from an agent loop filling in `{"action": "accept"}`, and it cannot
tell the stock SDK client's automatic retry from either. A gate that accepts the answer is a gate that
anything able to call the tool can open, which is to say it is the tool's own scope check, twice.

So the questions the gate has to answer are not about the protocol. They are: who may approve, for how
long does an approval hold, what exactly was approved, and what does presenting it again do.

## Decision

**The gate does not accept an answer. It accepts a grant:** a token the identity provider signed for a
person, which the agent cannot mint. The MRTR round trip is only how the grant travels.

### Who may approve

A grant is an approval only if all of this holds (`src/flightops_mcp/auth/approval.py`):

| Check | Why |
|---|---|
| Signed by our issuer, RS256, key from the JWKS | Same verification as inbound tokens; no second trust root |
| `aud` is the **approval audience** (`api://flightops-approvals`), not this server | A grant is useless as an access token here, and an access token is useless as a grant. Both directions are tested |
| Delegated (`scp`) and holds `flightops.rebooking.approve`; `idtyp` is not `app` | A service principal approving is a machine approving. `roles` is deliberately not read, unlike on the access-token path |
| `sub` differs from the proposer's `sub` | Four eyes. An agent holding its user's credentials, approve permission included, still cannot approve its own proposal |

`flightops.rebooking.approve` has no entry in the tool scope table. It is checked on the grant, and as a
caller's scope it opens nothing.

### How long an approval is valid

Three bounds, the tightest wins:

1. The grant's own `exp`, with no leeway.
2. `approval_max_age_s` (300 s) from the grant's `iat`, **whatever `exp` says**. An issuer that hands out
   hour-long tokens does not get to widen the window.
3. `approval_window_s` (600 s) from proposal to commit. This is the TTL of the sealed `requestState`:
   a proposal left longer than that must be proposed again.

### What is approved: payload binding

A proposal is a closed vocabulary (two flight designators, a date, a cohort, a reason code, a passenger
count) with one canonical JSON form. The grant carries **the proposal itself** as an RFC 9396
`authorization_details` entry of type `flightops_rebooking`, and the server hashes those fields and
compares the result with the hash of the proposal it is about to execute.

The grant carries the proposal rather than a hash of it on purpose. A hash is something the agent
computes and hands to the approver; the proposal is something an approver's consent screen can display.
Hashing the grant's own fields is what makes "the approver saw X" and "X is what runs" one statement
instead of two that are trusted to agree. Nothing is normalised on the way: a grant for `2.0` passengers
is not a grant for `2`.

Binding is checked three times, by parties that do not trust each other:

- the SDK refuses a retry whose arguments differ from the round that produced the `requestState` (the
  state is sealed, and bound to the caller, the tool name and a digest of the arguments);
- the tool refuses state whose proposal digest is not this proposal's;
- the verifier refuses a grant that describes any other proposal (`approval_payload_mismatch`).

The proposal also carries the **passenger count the approver saw**. If the manifest has changed by the
time the write arrives, the ops API answers `409 cohort_changed` and nothing happens: the approval
described a rebooking that no longer exists.

### What a replay does

The idempotency key is `rbk_` plus the first 128 bits of the proposal digest. It comes from the content,
so a retry produces the same key on any replica after any restart, and the flight date is part of the
content, so tomorrow's identical-looking rebooking is a different key.

- **Replaying the commit** (same state, same grant): the ops API returns the stored response byte for
  byte and writes nothing. The tool returns the original `result` block with `replayed: true` beside it.
- **A lost response** after the API wrote: the transport retries the `POST`, which is safe only because
  of the key. Tested by dropping the response on the way back.
- **Proposing the same content again**, approved by someone else: same key, same result, no second
  write. The ledger keeps the approver who approved it when it happened.
- **A replay still has to pass the gate.** Without a valid approval the ops API is not called at all,
  not even to read back a result that already exists. One rule, no exception to reason about.

A grant is **not** tracked for single use. The server is stateless and has no store to track it in, and
it does not need one: a grant can only authorise the one write it describes, that write is idempotent,
and the grant dies within five minutes. Reusing it yields the result it already produced.

## Options considered and rejected

- **Accept the elicitation answer** (`action: "accept"`, perhaps with a typed confirmation). The
  obvious reading of "use MRTR", and what the SDK's `Resolve`/`Elicit` helpers make easy. Rejected for
  the reason in Context. `test_saying_yes_is_not_an_approval` and
  `test_the_sdks_automatic_retry_cannot_approve_either` exist to keep it rejected.
- **URL-mode elicitation with a server-side approval record.** The approver follows a link, signs in to
  an approval service, and the server looks the approval up on retry. This is the better fit for the
  MCP guidance that form mode is for non-sensitive input, and it is the natural design once there is an
  approval service. Rejected for now because it needs shared state on a server whose transport is
  deliberately stateless, and because it moves the gate's logic out of the server and into the system
  being called. The verifier here does not care how a grant arrived, so this is a change of transport,
  not of design.
- **Enforce the approval only in the ops API.** The system of record should enforce it too, and the mock
  does refuse a rebooking approved by its requester. But a gate that lives only downstream makes the MCP
  server a pass-through for writes, and the thing this project claims to be is the governed layer.
- **Track `jti` for single use.** State, for a property idempotency already gives.
- **Let the grant name a digest instead of carrying the proposal.** Smaller, and it works. Rejected
  because it leaves "what the approver saw" outside the signed artefact.
- **Skip the gate for a replay of an executed proposal.** Friendlier to a client retrying after a
  timeout, and it would need a read of the ledger with no approval at all. Rejected: the invariant
  "no approval, no call" is worth more than the convenience.

## What this deliberately deviates from, and what it does not prove

- **A grant travels in a form-mode elicitation response.** MCP says form mode is for non-sensitive
  information. The grant is designed so that its confidentiality is not load-bearing: it is bound to one
  audience, one payload and five minutes, and replaying it reproduces a result that already exists. It
  is a proof, not a secret. It is still never logged, returned or written to a transcript
  (`approval_token` is a redaction key, and the tests plant the grant and search for it).
- **`authorization_details` is not something Entra issues.** ADR 0001 models the IdP on Entra; this
  claim is where the fixture goes beyond it. In an Entra deployment the grant would come from an
  approval service with its own signing key, or from a custom claims provider. The verifier needs an
  issuer, a JWKS URL and an audience, and nothing else changes.
- **The sealed state uses a per-process key unless `FLIGHTOPS_REQUEST_STATE_SECRET` is set.** That is
  correct for one process and fails closed across replicas: a retry that lands on another replica is
  refused. A multi-replica deployment must set the secret.
- **Nobody verifies that a human looked at anything.** The gate proves that a principal other than the
  proposer, holding the approve permission, obtained a grant for this exact payload from the IdP. That
  the IdP showed that person the proposal is the IdP's consent screen, which the fixture does not have:
  `/dev/user-token` mints on request. The local run proves the server's half of the gate.
- **Authority is not scoped.** Any holder of the approve permission may approve any rebooking. Limits by
  station, airline or size belong in the grant's claims and are not modelled.

## Consequences

- The agent loop needs an approval channel that is not the model: something that can show the proposal
  to a person and come back with a grant. `agent/` models that as a separate `Approver` the model never
  sees, and the offline backend's approver obtains its grant from the IdP like any other principal.
- A proposal the approver declines, ignores or answers too late costs one IdP exchange and no call to
  the ops API.
- An operator debugging a refusal reads `approval_rejected` in the server log, which carries the reason
  (`self_approval`, `too_old`, `payload_mismatch`, `wrong_audience`, ...). The caller is told only what
  it can act on: `approval_expired` can be asked for again, `approval_invalid` cannot be fixed.
- **Revisit if** an approval service exists (move to URL mode and a server-side record), if approvals
  need to be scoped by station or size, or if a write is added whose replay is not harmless, at which
  point single use stops being optional.
