# Protocol surface

What this server implements of MCP revision `2026-07-28` (SDK `mcp==2.3.0`), and what it does not. Each row
names the code and the test that holds it to the claim.

## The table

| Feature | State | Where | Note |
|---|---|---|---|
| Tools | **Implemented**: 12 | `server.py` | Sorted by name, byte-identical across builds, `ttlMs: 300000` |
| Tool annotations | **Implemented**: every tool | `annotations.py` | A tool without an entry fails registration |
| `outputSchema` + `structuredContent` | **Implemented**: 3 tools | `schemas.py` | `resolve_airport`, `airport_weather`, `disruption_triage`; text block kept |
| Progress notifications | **Implemented** on stdio; **not delivered** over HTTP | `progress.py` | One per upstream request, only with a `progressToken`. See below |
| Pagination | **Implemented**: `search_airports` | `server.py`, `airports.py` | Cursor-paged, ≤ 100 per page, ICAO order |
| Resources | **Implemented**: `airport://{icao}`, `airport://index` | `surface.py` | JSON; unknown code is error `-32602` |
| Prompts | **Implemented**: `triage_airport`, `brief_movements` | `surface.py` | |
| Completions | **Implemented**: ICAO codes, `direction` | `surface.py` | Resource template and prompt arguments only: see below |
| Elicitation | **Used, not offered generally** | `server.py` | Only for `propose_rebooking`'s human approval, through `input_required` |
| Sampling | **Not implemented** | | No tool needs a model's judgement; a server that asks the client's model to decide would hide where a severity came from |
| Logging capability | **Not implemented** | | Logs are structured JSON for the operator, redacted (`telemetry.py`); sending them to the client would put operator data in front of a model |
| Resource subscriptions | **Not implemented** | | The reference data changes only with a new image; there is nothing to notify |

Capabilities are declared at `initialize`: `tools`, `resources`, `prompts`, `completions`. `logging` is absent on
purpose (`tests/test_protocol_surface.py::test_capabilities_declare_resources_prompts_and_completions`).

## Annotations

| Tool | readOnly | destructive | idempotent | openWorld |
|---|---|---|---|---|
| `resolve_airport` | true | | | **false** |
| `search_airports` | true | | | **false** |
| `upstream_status` | true | | | **false** |
| `airport_weather` | true | | | true |
| `weather_overview` | true | | | true |
| `airport_live_traffic` | true | | | true |
| `airport_movements` | true | | | true |
| `airspace_snapshot` | true | | | true |
| `disruption_triage` | true | | | true |
| `schiphol_flights` | true | | | true |
| `flight_passenger_impact` | true | | | true |
| `propose_rebooking` | **false** | **true** | **true** | true |

`openWorldHint: false` means the tool reads only the bundled reference or this process's own state. A read leaves
`destructiveHint` and `idempotentHint` unset: the specification gives them meaning only for a write.
`propose_rebooking` is idempotent because the proposal's digest is the idempotency key the operations API honours:
a repeat returns the original result and rebooks no one twice.

## Output schemas

The schemas describe each payload as it is built today. They were not used as a reason to reshape it. Each one
is `oneOf` the success shape and the failure shape (`ok: false` with `error.code`, `message`, optional `hint`,
`retry_after_s`, `details`). Objects this server builds are closed, so a renamed field fails validation.

What they **can** express: `unknowns` lists; `null` for a count never made (zero is a count, null is not);
`severity: unknown`; a leg `unavailable` or `not_fetched`; `upstream_time_epoch: null` when an upstream sent no
`Date`; the cache block on a served-from-cache answer.

What they **cannot** express:

- **The type of a value copied from NOAA.** `station_id`, `observation_time`, `raw_text`, `flight_category` and
  `wx_string` are passed through as NOAA sent them, so the schema accepts any JSON value there. Typing them as
  strings would describe what NOAA usually sends, not what the code guarantees.
- **Consistency between fields.** That `counts.arrivals.count` equals the number of arrival rows, or that
  `severity: low` needs a leg with evidence, is the tool's logic, tested in `test_server_tools.py`. JSON Schema
  cannot express it.
- **Meaning.** A schema can say `unknowns` is a list of strings. It cannot say that a non-empty list forbids
  concluding that operations are normal. The tool descriptions and the prompts say that.

The SDK still validates results against its own derived model (any object). The real schemas are held to the
payloads by `test_every_payload_validates_against_its_own_schema` and
`test_every_payload_in_the_eval_scenarios_validates`, which runs every golden scenario that touches the three tools
(absent-evidence included). They are not enforced at runtime, so a mistake in a schema cannot turn a correct
answer into an error.

## Progress

`airport_movements` (1-2 requests), `airspace_snapshot` (1) and `weather_overview` (one per 50 airports, plus two
for each halved request) report one notification after each upstream request: `progress` counts requests made,
`total` the requests planned. No token, no notification. A cache hit made no request and reports nothing.
`disruption_triage` reports nothing: its parts are called without a request of their own, and counting them would
need a total the composite cannot know before its cache lookups.

Measured on the live stdio server (2026-11-19): `airport_movements(EGLL, both)` sent **2** notifications
(`1/2`, `2/2`) before its result; `weather_overview` for 60 airports sent **2** (one per NOAA request).

**Over HTTP progress is not delivered.** The HTTP server answers with a single JSON body (`json_response: True`,
`HTTP_OPTIONS` in `server.py`), which has no room for a notification. A request with a progress token succeeds
normally (`test_over_http_with_json_responses_a_token_is_accepted_but_nothing_streams`). Delivering progress over
HTTP means answering with an SSE stream instead, which changes what a load balancer and every raw-HTTP client
receive. That is a separate decision.

## Pagination

`search_airports(query, region, limit, cursor)` returns at most `limit` airports (default 20, maximum 100) in ICAO
order, and `nextCursor` while more match. The cursor is `1:<last ICAO served>:<digest of query and region>`.
Paging is by key (`ident > last`), not offset, so a page fetched twice is the same page. A cursor used with a
different query or region is refused (`invalid_cursor`). It is not signed: it points into public data, and a
forged one can only skip ahead. Plain text on purpose: base64 of JSON begins `eyJ`, as a JWT does.

A full walk of the 3,270-airport world reference, 100 per page: **3,270 airports, 33 pages, no repeats, in
order** (`test_a_full_walk_of_the_world_reaches_every_airport_once`, and on the live stdio server).

## Completions

The protocol completes **prompt arguments and resource-template arguments only**: `completion/complete` takes a
`ref/prompt` or a `ref/resource`, and there is no reference type for a tool argument. The ICAO arguments of
`airport_weather`, `airport_movements`, `airport_live_traffic` and `disruption_triage` therefore cannot be
completed. What is completed:

- `icao` on `airport://{icao}`;
- `airport` on `triage_airport` and `brief_movements` (the prompts that call `disruption_triage` and
  `airport_movements`);
- `direction` on `brief_movements`, from `arrival`, `departure`, `both`.

An airport completion is an ICAO prefix match over the reference, in order, at most 100 values (the protocol's
cap), with `total` and `hasMore`. A completion value is the literal text inserted into the argument, so it is
the code alone; the name cannot go in it. A host that wants the name reads `airport://{icao}`.

## On the authenticated HTTP server

Resource reads and airport completions need `flightops.reference.read`, the scope `resolve_airport` needs. A
refusal is a JSON-RPC error with `data.error: "insufficient_scope"`, because neither method has a tool-result
channel. Prompts are text and need no scope. Listing tools, resources, templates and prompts needs a token and
no scope, as before.
