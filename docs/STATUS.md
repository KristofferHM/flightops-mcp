# Status — where this project stands, for whoever picks it up

**Shelved 2026-11-21 at `v1.14.0`.** Working, tested, and demonstrable; not finished, and not pretending to
be. This page is the honest handover: what is proven, what is known to be wrong, and what is parked.

---

## What it is

A governed MCP server for airport-operations data, plus a live board. The governance is the point —
OAuth 2.1 with per-tool scopes and on-behalf-of exchange, a credit ledger in front of every metered
upstream, identity-partitioned caching, redaction-tested logs — and the second point is that the whole
thing is measured rather than described.

## What is proven, by numbers

```
tests            1,931, zero skipped (CI fails on a skip)
coverage         floor 90 %, actual ~92 %
mutation         ceiling 106 survivors; the gate self-tests by running one below its ceiling and
                 failing if it passes; the last triage judged 303 survivors individually
evals            70 golden scenarios, scored on five separate axes, with a committed baseline
                 and a published five-run distribution from a real model
CI               eleven jobs on every PR: lint+types, tests on 3.12 and 3.13, evals, mutation,
                 compose matrix, container build, agent determinism, a11y, secret and dependency scans,
                 live upstream contract
a11y             axe-core at two viewports; panel boxes asserted not to intersect
```

## What ships

```
MCP server       12 tools, protocol surface (annotations, output schemas, structured content, progress,
                 cursor pagination, resource, prompts, completions) with a published table of what it
                 deliberately does not implement
governance       scopes, ledger, cache, passthrough guard, approval gate for the one write, refusals as data
board            map + globe, aircraft with observed tracks and priced cost, inbound-by-geometry,
                 operator classes, airport layers and weather chips, quiet panels with ? tooltips,
                 freshness from upstream time in three modes (LIVE / STALE / RECORDED)
demo video       committed GIF in the README, MP4 on the v1.14.0 release, rebuildable in one command
                 from a replay fixture with no credentials
```

## Known wrong, in the order I would fix them

1. **Two designator tables disagree.** `airlines.csv` (163) names an operator, `operator_designators.csv`
   (7) decides its class. `SFR` gets a name and lands in `unknown`. One table, or a test that keeps them
   in step. *(Small; visible on the board.)*
2. **The banner prints a full ISO timestamp with microseconds** (`2026-11-20T20:03:26.283923+00:00`) while
   the map overlay prints `Recorded 2026-11-20 20:03 UTC`. Same fact, two formats, one unreadable.
3. **Operator classes are callsign-only in this build.** A NetJets jet reads as an airline; the type-aware
   version needs a type source this repository does not carry (the commercial edition has one).
4. **No destination from ADS-B**, by physics rather than omission: a transponder broadcasts position, not
   intent. `destination` is always `no_source`.
5. **The globe misses 30 fps** at the 95th percentile while dragging at 500 aircraft, and not reliably at
   200. Measured, recorded in `docs/DASHBOARD.md`, and the default cap is 200 for legibility rather than
   speed.
6. **No permanent host.** Local or tunnel only, by decision; a quick tunnel has no uptime guarantee.

## Parked, not lost

```
feat/view-cache   WIP branch, pushed, not mergeable as-is. Keeps what was fetched instead of blanking
                  the board on a view change: stale-while-revalidate, an LRU of views with a TTL from
                  upstream_epoch, and an opt-in bulk load with its cost stated. Gates were never run on
                  that tree. Start with a gate run, then the brief.
```

The private commercial repository is separate and unaffected: licence classes, editions gated at startup,
attribution in every payload, a BYO boundary, and its own roadmap.

## Run it

```bash
uv sync
uv run python -m dashboard          # http://127.0.0.1:8765; passcode needed if exposed
uv run pytest                       # ~5 minutes
uv run flightops-mcp                # the stdio server
```

Read in this order: `README.md` (front door), `docs/ARCHITECTURE.md` (one request end to end),
`docs/PROTOCOL.md` (the surface and its refusals), `docs/QUALITY.md` (the gates),
`docs/EVALS.md` (what the numbers mean and do not).

## If this is resumed

1. Fix 1 and 2 above — a table and a format string, half a day together.
2. Finish `feat/view-cache` or delete it; an unmerged WIP branch is debt.
3. Then the only feature I would still add: type-aware classification, which needs a type source and
   therefore a licence decision.
4. Nothing else. The project has more surface than evidence already; the next feature would make the
   README harder to defend, not easier.
