"""The example bring-your-own provider. A fixture, clearly labelled, for a customer to replace.

It serves invented flights, never real ones: every flight number starts with `TEST`,
the licence says "example fixture", and `provider_flights` adds an unknown saying so.
It is off unless `FLIGHTOPS_BYO_EXAMPLE_PROVIDER=true`.

It exercises the whole boundary a real adapter crosses: a credential read from the
environment by name, a fetch that answers in a vendor's own shape, a normaliser that
turns that shape into `ProviderFlight`s, and a quota. A real adapter keeps this
structure and replaces `_fetch` with calls through the `ProviderHttp` it is given.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Final

from .licences import Licence
from .providers import (
    FlightsRequest,
    ProviderAdapter,
    ProviderError,
    ProviderFlight,
    ProviderHttp,
    Quota,
)

CREDENTIAL: Final = "FLIGHTOPS_EXAMPLE_PROVIDER_KEY"
# Minutes after the window opens, and the status each fixture flight reports.
_SCHEDULE: Final = ((10, "scheduled"), (35, "delayed"), (70, "cancelled"))

LICENCE: Final = Licence(
    name="Example fixture provider (invented data)",
    licence_class="D",
    licence_name="Example fixture: replace with your own licensed provider",
    attribution="Schedules: example fixture provider. Invented flights, not real data.",
    obligations=("Customer-supplied: never bundled, cached beyond the request, or redistributed.",),
    may_cache=False,
    may_redistribute=False,
    terms_url="",
)


def _utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _fetch(
    request: FlightsRequest, credentials: Mapping[str, str], _http: ProviderHttp
) -> object:
    """A vendor-shaped answer, built locally. A real adapter calls `_http` here."""
    if not credentials.get(CREDENTIAL):
        raise ProviderError(f"{CREDENTIAL} is empty")
    rows = []
    for direction in request.directions:
        for number, (minutes, status) in enumerate(_SCHEDULE, start=1):
            rows.append(
                {
                    "ident": f"TEST{100 * (1 if direction == 'arrival' else 2) + number}",
                    "kind": "ARR" if direction == "arrival" else "DEP",
                    "counterpart": "ZZZZ",
                    "sched": request.begin_epoch + minutes * 60,
                    "state": status,
                }
            )
    return {"vendor": "example", "rows": rows}


def _normalise(raw: object, _request: FlightsRequest) -> Sequence[ProviderFlight]:
    """The vendor's shape into `ProviderFlight`. A row that does not fit is skipped and counted."""
    rows = raw.get("rows") if isinstance(raw, dict) else None
    flights: list[ProviderFlight] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("sched"), int):
            continue
        flights.append(
            ProviderFlight(
                flight=str(row.get("ident")),
                direction="arrival" if row.get("kind") == "ARR" else "departure",
                other_airport=row.get("counterpart"),
                scheduled_utc=_utc(row["sched"]),
                estimated_utc=None,
                actual_utc=None,
                status=row.get("state"),
            )
        )
    return flights


EXAMPLE_PROVIDER: Final = ProviderAdapter(
    name="example",
    licence=LICENCE,
    fetch=_fetch,
    normalise=_normalise,
    quota=Quota(requests_per_hour=60),
    credential_env=(CREDENTIAL,),
)
