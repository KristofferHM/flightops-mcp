# A commercial edition: licence classes, editions, and the BYO boundary

This repository is a demo and stays one. Nothing here is for sale, and there is no pricing page or payment
system. This document describes what a commercial product built from this code would look like, and the code
on this branch enforces it: a for-profit deployment cannot serve data it has no right to, by accident or by
configuration.

The short version: **the demo is built on a class-C source.** OpenSky Network supplies its traffic, and
OpenSky's terms require a written licence for any for-profit use and for any operational use. That is fine
for a portfolio demo. It is not fine for a paid product, and the code makes the difference impossible to miss.

## The licence facts, checked on 2026-11-20

The table is copied word for word from the terms as checked on 2026-11-20. The same wording is in
`src/flightops_mcp/licences.py`, which payloads, the board's footer and `/licences` repeat.

| Source | Class | What the terms say | Link |
|---|---|---|---|
| NOAA `aviationweather.gov` (METAR, TAF, SIGMET, station/airport info) | **A** public domain | US Government work; the API asks that requests be *limited in scope and frequency* and points large queries at its cache files. Operational limits, not a licence fee. | [aviationweather.gov/data/api](https://aviationweather.gov/data/api/) |
| OurAirports (the 3,270-airport reference) | **A** public domain | No attribution required; we attribute anyway. | [ourairports.com/data](https://ourairports.com/data/) |
| `adsb.lol` | **B** open with obligations | API is **ODbL 1.0**: commercial use is permitted, but attribution is required and a *derived database* used publicly must be offered under ODbL. This is the only genuinely free commercial traffic source. | [api.adsb.lol/docs](https://api.adsb.lol/docs), [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/) |
| OpenSky Network | **C** commercial terms | "Any use by a for-profit or commercial entity requires written permission and a license." **Also**: "Operational use of the REST API in any live product, service, or automated system also requires a written license, regardless of the entity's non-profit status." | [opensky-network.org/about/terms-of-use](https://opensky-network.org/about/terms-of-use) |
| AviationStack | **C** commercial terms | Free tier is **100 requests/month, non-commercial**. Commercial use starts at **$49.99/month** for 10,000 requests. | [aviationstack.com/pricing](https://aviationstack.com/pricing) |
| AeroDataBox | **C** commercial terms | Symbolic pricing, no SLA, explicitly **not permitted for mission-critical or safety-critical use**; caching, retention, attribution, resale and sublicensing are restricted by plan. | [aerodatabox.com](https://aerodatabox.com/) |
| Schiphol | **C** commercial terms | Partner tier by agreement; the public tier is what the current adapter mocks. | [developer.schiphol.nl](https://developer.schiphol.nl/) |
| A customer's own provider (Cirium, OAG, FlightAware, FR24, an airline's AODB) | **D** customer-supplied | We never bundle, cache beyond the request, or redistribute it. | The customer's own contract |

Two more facts used below come from adsb.lol's own API description (OpenAPI 0.0.2, read the same day), not
from the table: without a key the API allows **500 requests a day per IP**, and over quota it answers `429`
with `Retry-After`.

This repository's internal operations API (`services/ops_api`, the passenger counts behind
`flight_passenger_impact` and `propose_rebooking`) stands for an airline's own system, so the registry gives it
class **D**.

## What the code enforces

| Rule | Where | Proven by |
|---|---|---|
| Every source has a registry entry. An adapter calls `licensed("...")` at import, so a source without an entry is an `ImportError` that names it. There is no default class. | `licences.py` | `test_a_source_without_a_licence_entry_fails_import_naming_it` |
| Every tool has a source route, as it has a scope, an annotation and a cache hint. A tool without one cannot be registered. | `provenance.py` | `test_every_tool_has_a_source_route_as_it_has_a_scope` |
| `FLIGHTOPS_EDITION` decides which classes may be served, **once, at startup**. A refused source stops the process with the variable, the class and the licence. It is not a warning and not a silent downgrade. | `edition.py`, `Runtime.build`, both CLIs | `test_the_exact_refusal_message`, `test_the_server_refuses_to_start_and_says_why` |
| A tool the edition cannot serve is not listed. If it is called anyway, for example as one leg of a triage, it answers `not_in_edition` before any upstream is asked. | `provenance.available`, `create_server` | `test_without_a_traffic_source_traffic_is_refused_before_any_request` |
| Every payload, failures included, carries `edition` and `commercial_use`. Every successful payload carries `sources`: name, licence class, licence, attribution and fetch time. | `server._with_edition`, `provenance.stamp` | `test_commercial_with_open_sources_boots_and_attributes_every_payload` |
| The demo's payloads differ from their bodies only by those fields. | the same | `test_demo_payloads_differ_from_their_bodies_only_by_the_new_fields` (11 calls) |
| The board's footer is built from the payloads behind what is on screen, never from a fixed string. | `dashboard/licensing.py`, `board.js` `renderAttribution` | `test_the_footer_credits_odbl_whenever_adsblol_served_part_of_the_page`, `..._never_..._when_adsblol_served_nothing` |
| The demo board always shows a strip saying it is not licensed for commercial use. A page that has lost the strip's marker is refused, not served. | `licensing.index_page` | `test_the_demo_board_carries_its_strip_and_licences_page`, `test_the_strip_cannot_be_quietly_removed` |
| `/licences` (on the board and on the MCP HTTP server, unauthenticated) states what the deployment may and may not do, each source's terms, and the ODbL derived-database notice when an ODbL source is in use. | `server.licence_notice`, `licensing.licences_page` | `test_licences_page_carries_the_derived_database_notice_only_with_odbl` |

### The startup refusal, exactly

```
$ FLIGHTOPS_EDITION=commercial FLIGHTOPS_OPENSKY_CLIENT_ID=... uv run flightops-mcp
usage: flightops-mcp [-h] [--transport {stdio,streamable-http}] [--host HOST]
                     [--port PORT]
flightops-mcp: error: FLIGHTOPS_EDITION=commercial refuses to start.
  FLIGHTOPS_OPENSKY_CLIENT_ID configures OpenSky Network, licence class C (commercial terms; OpenSky Network Terms of Use).
The commercial edition serves licence classes A and B only. Unset FLIGHTOPS_OPENSKY_CLIENT_ID, or choose the edition whose terms you hold (docs/COMMERCIAL.md).
```

The exit status is 2. Every refused variable is listed at once. The variables that count as putting a source in use are its
credentials and its on-switch: `FLIGHTOPS_OPENSKY_CLIENT_ID`, `_CLIENT_SECRET` and `_ANONYMOUS` (anonymous
access is still OpenSky's REST API); `FLIGHTOPS_SCHIPHOL_CLIENT_ID` and `_CLIENT_SECRET`;
`FLIGHTOPS_AVIATIONSTACK_ACCESS_KEY`; `FLIGHTOPS_AERODATABOX_API_KEY`; `FLIGHTOPS_OPS_API_BASE_URL` (class D);
`FLIGHTOPS_BYO_PROVIDERS` and `FLIGHTOPS_BYO_EXAMPLE_PROVIDER` (class D). AviationStack and AeroDataBox have no
adapter. Their keys exist only so that the gate can refuse them by name.

### What a payload gained

| Field | On | Meaning |
|---|---|---|
| `edition` | every payload, failures too | `demo`, `commercial` or `byo` |
| `commercial_use` | every payload, failures too | `not-permitted` in the demo, `permitted` otherwise |
| `sources[]` | every successful payload | `{name, licence_class, licence_name, attribution, fetched_at}` for each source the answer was built from. A triage lists only the legs that answered. `upstream_status` reads no data source, so its list is empty. |
| `source.licence_class`, `.licence_name`, `.attribution`, `.fetched_at` | single-source payloads | the same record, merged into the `source` block the payload already had |
| `licences` | `upstream_status` | the edition, its allowed classes, and every configured source with its class and the variable that configured it |

`fetched_at` is stamped inside the tool cache, so a cached answer keeps the time it was fetched.

## Three editions, and what each may legally do

| Edition | Classes | For | May | May not |
|---|---|---|---|---|
| `demo` (default) | A, B, C, D | A portfolio. Never for sale. | Show the server; read each source within its free, non-commercial terms. | Be sold or run as part of a paid product. Be presented as licensed: every payload says `not-permitted`, and the board shows the strip. |
| `commercial` | A, B | See the offerings below. | Serve public-domain and ODbL data to paying customers, with ODbL's attribution and notice. | Serve OpenSky, AviationStack, AeroDataBox or Schiphol. The process refuses to start with their credentials set. |
| `byo` | A, B, D | An organisation that already pays Cirium, OAG or FlightAware, or runs its own AODB. | Everything `commercial` may do, plus the customer's own provider, served to that customer, request by request. | Cache, store or redistribute provider data; serve one customer's provider to another; use the built-in class-C adapters. |

The `commercial` edition covers two of the three offerings, depending on one switch:

1. **Ops weather and reference**: `FLIGHTOPS_EDITION=commercial` and nothing else. METAR/TAF, the weather
   overview, the airport reference, and a triage whose traffic and movements legs say *none in this edition*.
   No ADS-B at all. **For FBOs, flight schools, ground handlers and UAS operators**, whose question is whether
   the weather allows the operation.
2. **Traffic**: add `FLIGHTOPS_ADSBLOL_ENABLED=true`. Live traffic near an airport comes from adsb.lol under
   ODbL, and the triage gains its traffic leg. Region-wide snapshots, tracks and movement lists stay
   unavailable: adsb.lol answers a circle of at most 250 nm and keeps no history in its API. This offering is
   **for anyone with a traffic licence**. ODbL is that licence for adsb.lol data. A customer who holds a
   written OpenSky licence, or a FlightAware or FR24 contract, plugs that feed in through the third offering.
3. **BYO**: `FLIGHTOPS_EDITION=byo` with `FLIGHTOPS_BYO_PROVIDERS=package.module:ADAPTER`. `provider_flights`
   serves the customer's schedule provider. **For an organisation that already pays Cirium or OAG.**

### The BYO boundary

A provider is one `ProviderAdapter` (`src/flightops_mcp/providers.py`) with:

- a `Licence` of class **D**; any other class is refused at construction;
- `fetch(request, credentials, http)`, where `credentials` holds the environment variables the adapter names in
  `credential_env`, read at call time, and `http` is the server's own guarded client (passthrough guard,
  retries, circuit breaker);
- `normalise(raw, request)`, which turns the vendor's answer into `ProviderFlight`s;
- a `Quota`: the customer's contract, enforced locally per hour.

Adding one edits nothing in `server.py`, `board.py` or `cache.py`. It is registered by `FLIGHTOPS_BYO_PROVIDERS`
(a `module:attribute` path anywhere on `sys.path`) or by `register_provider()`. The example,
`providers_example.py`, sits behind `FLIGHTOPS_BYO_EXAMPLE_PROVIDER=true`. It serves invented `TEST...`
flights, says so in its licence, its attribution and an `unknowns` line, and reads
`FLIGHTOPS_EXAMPLE_PROVIDER_KEY` the way a real adapter reads its key.

What the boundary guarantees, and the test that holds it to it (`test_byo_serves_a_d_adapter_and_writes_nothing_of_it_down`):

- the adapter's data appears in the answer;
- the tool cache stays empty, because `provider_flights` has no cache entry and a class-D answer bypasses the
  cache;
- every credential value is registered with the log redaction before the first request. The test puts the key
  in a query string on purpose, because `httpx` logs every request URL at INFO;
- an adapter's exception is reported by its class only, since its message may quote a URL with a key in it;
- nothing in the run's working directory contains the credential or the data afterwards;
- the digests of `server.py`, `board.py` and `cache.py` are the same before and after the registration.

## Unit economics, in plain numbers

Only the table's own numbers are used here. Where a price is not public, it is called unknown, not zero.

| Source | Smallest commercial tier | Per request | What it buys |
|---|---|---|---|
| NOAA | $0 | $0 | Limited only by "scope and frequency" |
| OurAirports | $0 (bundled file) | $0 | — |
| adsb.lol | $0 licence fee | $0 | 500 requests/day per IP without a key; the engineering cost of the ODbL duties below |
| AviationStack | **$49.99/month for 10,000 requests** | **$0.005** | Schedules |
| AeroDataBox | "symbolic" | not stated | Schedules, but **not for mission-critical or safety-critical use** |
| OpenSky | written licence | **unknown** | Not public. Do not budget it as zero. |

What a schedules customer would have to pay before the data bill is covered:

- One airport's schedule refreshed every **5 minutes** is 12 × 24 = 288 requests a day, or **8,640 in a 30-day
  month**. That fits the $49.99 tier. **The floor is $49.99 a month per airport watched at that rate**, before
  hosting, support and payment fees.
- Every **2 minutes** is 720 a day, or **21,600 a month**: 2.16 times the smallest tier. The price of the next
  tier is not in the checked table, so the cost at that rate is unknown.
- The subscription cannot be shared across customers to spread the cost. Resale and sublicensing are restricted
  (AeroDataBox says so outright), so the $49.99 floor is per customer, not per deployment.

Traffic through adsb.lol has no licence fee, but the quota binds first. The board's airport panel refreshes
every 2 minutes, which is 720 requests a day: over the 500 a day an IP gets without a key. The server's own
ceiling is `FLIGHTOPS_ADSBLOL_REQUESTS_PER_HOUR=20`, about 480 a day, so it stops before adsb.lol has to.
A deployment with more than one active panel needs a key or a sponsor plan.

So: **ops weather and reference** has a data bill of $0. **Traffic through adsb.lol** has $0 in fees and the
cost of complying with ODbL. **Schedules** cost at least $49.99 a month for each customer airport watched every
5 minutes. That figure is the cheapest commercial schedule source in the table, and AeroDataBox is ruled out for
operational use by its own terms.

## What ODbL requires, as engineering requirements

adsb.lol is the only class-B source. Each of its obligations is a requirement with a test, not a warning:

1. **Attribution in the payload.** Every answer built from adsb.lol carries
   `"attribution": "Traffic: adsb.lol, made available under the Open Database License (ODbL) 1.0, ..."` in
   `source` and in `sources[]`. The model is told to repeat it when it quotes the data.
2. **Attribution on the page.** The board's footer is the union of the `attribution` lists of the responses
   behind what is on screen: the board, the selected airport, its triage, the traffic layer and a focused track.
   Each part is forgotten when it leaves the screen. ODbL's line appears whenever adsb.lol served part of the
   page, and never when it did not. Both directions are tested.
3. **The derived-database notice.** `/licences` states that the service contains information from adsb.lol under
   ODbL 1.0, and that any database derived from it and used publicly is offered under ODbL 1.0. The page is
   unauthenticated on both servers, because a notice behind a passcode has not been offered.
4. **What "derived" means here.** The server takes adsb.lol's `/v2/point` answer, cuts the circle back to the
   requested box, drops rows that are not aircraft, converts feet, knots and feet per minute to metres and
   metres per second, and re-keys the fields. The resulting array of aircraft records is a database derived
   from adsb.lol's. It is held in the tool cache for 10 seconds and returned to every caller who asks, which
   makes it publicly used. A map drawn from it, or a sentence an agent writes about it, is a produced work.
   That needs the attribution, not the share-alike.

## Do not

- **Do not sell a demo build**, or run one as part of anything paid. It says `commercial_use: not-permitted`
  in every payload and carries a strip on every page, and neither may be removed.
- **Do not serve OpenSky in a paid product without a written licence.** The `commercial` and `byo` editions
  refuse to start with OpenSky's credentials, or with anonymous access switched on.
- **Do not cache or resell a D-class provider's data beyond the request.** That includes the client side: the
  agent harness writes transcripts to disk, and the board can record. Neither should be pointed at a customer's
  provider.
- **Do not describe NOAA data as warrantied.** It is a US Government work provided as is, and the attribution
  says so.
- **Do not use AeroDataBox for mission-critical or safety-critical use**, which is what an operations product is.
- **Do not replay a demo recording in a commercial deployment.** It may hold OpenSky data, and the board
  refuses `--replay` outside the demo.

## Where the licences leave genuine ambiguity

1. **Is the demo itself "operational use"?** OpenSky's second sentence requires a written licence for
   "operational use of the REST API in any live product, service, or automated system ... regardless of the
   entity's non-profit status". The terms do not define *operational*. A board that refreshes itself, and an
   agent loop that calls the API unattended, are automated systems that are live. If "operational" means
   "used to run operations", the demo is outside the clause. If it means "running unattended in production",
   a public demo deployment may already need the written licence the commercial edition refuses to work
   without. The code cannot settle this. It labels the demo, keeps its spend local and small, and leaves the
   question to OpenSky.
2. **Is a transient, filtered extract a derivative database?** The reading in requirement 4 treats the 10-second
   cached array as a derived database that is publicly used, and so offers it under ODbL. It is never kept, so
   "offering" it in practice means offering the method and, on request, a copy of what was served. Another
   reading treats a per-request API answer as a produced work, which needs attribution only. This document
   takes the stricter reading.

## Running each edition

```bash
uv run flightops-mcp                                                   # demo, as before
FLIGHTOPS_EDITION=commercial uv run flightops-mcp                      # ops weather and reference
FLIGHTOPS_EDITION=commercial FLIGHTOPS_ADSBLOL_ENABLED=true uv run flightops-mcp   # + traffic (ODbL)
FLIGHTOPS_EDITION=byo FLIGHTOPS_BYO_EXAMPLE_PROVIDER=true \
  FLIGHTOPS_EXAMPLE_PROVIDER_KEY=anything uv run flightops-mcp         # + the example provider
FLIGHTOPS_EDITION=commercial uv run python -m dashboard                # the board, no strip, footer by source
```
