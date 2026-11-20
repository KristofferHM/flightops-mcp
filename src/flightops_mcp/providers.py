"""Bring-your-own providers: the boundary a customer's licensed data crosses.

A customer who already pays Cirium, OAG, FlightAware or runs an AODB plugs it in as
one `ProviderAdapter`: a declared class-D licence, a `fetch` function, a normaliser
and a quota. Nothing else in the server changes. `provider_flights` is the one tool
that reads providers, and it treats each the same way:

* **Credentials come from the environment**, by the names the adapter declares, at
  call time. They are never stored on the runtime, never in a payload, and every
  value is registered with the log redaction before the first request is made.
* **Nothing is held.** A class-D source may not be cached or redistributed
  (`Licence.__post_init__` refuses one that says otherwise), so the tool has no
  cache entry and the dashboard never asks for it.
* **A failure is a code, not a traceback.** An adapter's exception can carry its
  URL, and a URL can carry a key; only the exception's class is reported.

Adapters are found three ways, none of which edits this package:
`register_provider()` from the embedding code, `FLIGHTOPS_BYO_PROVIDERS` naming
`module:attribute` paths, and `FLIGHTOPS_BYO_EXAMPLE_PROVIDER` for the fixture.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

import httpx

from .config import Settings
from .credits import CreditLedger
from .errors import ToolFailure
from .http import SleepFn, UpstreamHttp, authorized_get
from .licences import REGISTRY, Licence
from .telemetry import register_secret_values

__all__ = [
    "EXAMPLE_PATH",
    "FLIGHT_FIELDS",
    "FlightsRequest",
    "ProviderAdapter",
    "ProviderError",
    "ProviderFlight",
    "ProviderHttp",
    "ProviderService",
    "Quota",
    "clear_registered",
    "load_providers",
    "register_provider",
    "registered",
]

EXAMPLE_PATH: Final = "flightops_mcp.providers_example:EXAMPLE_PROVIDER"
NAME: Final = re.compile(r"[a-z][a-z0-9_]{1,31}")
# The fields a normalised flight carries, in order. A provider that has no value for
# one says None; it does not leave the key out.
FLIGHT_FIELDS: Final = (
    "flight",
    "direction",
    "other_airport",
    "scheduled_utc",
    "estimated_utc",
    "actual_utc",
    "status",
)
Direction = Literal["arrival", "departure"]


class ProviderError(ValueError):
    """An adapter that cannot be registered, and why."""


@dataclass(frozen=True, slots=True)
class Quota:
    """What the customer's contract allows, as a local ceiling charged per request."""

    requests_per_hour: int

    def __post_init__(self) -> None:
        if self.requests_per_hour < 1:
            raise ProviderError("a quota allows at least one request per hour")


@dataclass(frozen=True, slots=True)
class FlightsRequest:
    """What `provider_flights` asks a provider for: one airport, one or both directions."""

    icao: str
    iata: str | None
    directions: tuple[Direction, ...]
    begin_epoch: int
    end_epoch: int


@dataclass(frozen=True, slots=True)
class ProviderFlight:
    """One flight, in the shape every provider is normalised to."""

    flight: str
    direction: Direction
    other_airport: str | None
    scheduled_utc: str | None
    estimated_utc: str | None
    actual_utc: str | None
    status: str | None

    def to_payload(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in FLIGHT_FIELDS}


class ProviderHttp(Protocol):
    """The HTTP an adapter may use: GET, JSON back, through the server's guarded client.

    The client carries the passthrough guard, retries and the circuit breaker. The
    adapter never sees the caller's token and cannot build its own client.
    """

    async def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> object: ...


Fetch = Callable[[FlightsRequest, Mapping[str, str], ProviderHttp], Awaitable[object]]
Normalise = Callable[[object, FlightsRequest], Sequence[ProviderFlight]]


@dataclass(frozen=True, slots=True)
class ProviderAdapter:
    """A customer-supplied provider. Everything `provider_flights` needs to serve it."""

    name: str
    licence: Licence
    fetch: Fetch
    normalise: Normalise
    quota: Quota
    # Environment variables the adapter needs, by name. Read at call time only.
    credential_env: tuple[str, ...] = ()
    base_url: str = ""

    def __post_init__(self) -> None:
        if not NAME.fullmatch(self.name):
            raise ProviderError(
                f"provider name {self.name!r} must be 2-32 lowercase letters, digits or _"
            )
        if self.licence.licence_class != "D":
            raise ProviderError(
                f"provider {self.name!r} declares licence class {self.licence.licence_class}. "
                "A bring-your-own provider is class D: the customer's own licence. A class "
                "A, B or C source belongs in flightops_mcp.licences.REGISTRY."
            )


class _Registered:
    """Adapters registered from code, held in a class rather than a bare `global`."""

    adapters: dict[str, ProviderAdapter] = {}


def register_provider(adapter: ProviderAdapter) -> ProviderAdapter:
    """Make `adapter` available to the next runtime built. Returns it, to allow decoration."""
    if not isinstance(adapter, ProviderAdapter):
        raise ProviderError(f"{adapter!r} is not a ProviderAdapter")
    existing = _Registered.adapters.get(adapter.name)
    if existing is not None and existing is not adapter:
        raise ProviderError(f"a provider named {adapter.name!r} is already registered")
    _Registered.adapters[adapter.name] = adapter
    return adapter


def registered() -> Mapping[str, ProviderAdapter]:
    return dict(_Registered.adapters)


def clear_registered() -> None:
    """Forget code-registered adapters (tests)."""
    _Registered.adapters.clear()


def _import(path: str) -> ProviderAdapter:
    module_name, _, attribute = path.partition(":")
    if not module_name or not attribute:
        raise ProviderError(f"{path!r} is not a module:attribute path")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ProviderError(f"{path}: cannot import {module_name} ({exc})") from exc
    adapter = getattr(module, attribute, None)
    if not isinstance(adapter, ProviderAdapter):
        raise ProviderError(f"{path} is not a ProviderAdapter")
    return adapter


def load_providers(settings: Settings) -> dict[str, tuple[ProviderAdapter, str]]:
    """Every adapter this process will serve, with the variable that brought it in."""
    found: dict[str, tuple[ProviderAdapter, str]] = {
        name: (adapter, "register_provider()") for name, adapter in registered().items()
    }
    paths = [p.strip() for p in settings.byo_providers.split(",") if p.strip()]
    wanted = [(path, "FLIGHTOPS_BYO_PROVIDERS") for path in paths]
    if settings.byo_example_provider:
        wanted.append((EXAMPLE_PATH, "FLIGHTOPS_BYO_EXAMPLE_PROVIDER"))
    for path, variable in wanted:
        adapter = _import(path)
        held = found.get(adapter.name)
        if held is not None and held[0] is not adapter:
            raise ProviderError(f"two providers are named {adapter.name!r}")
        found.setdefault(adapter.name, (adapter, variable))
    for name in found:
        if name in REGISTRY:
            # A provider called "opensky" would inherit OpenSky's place in every table.
            raise ProviderError(f"provider {name!r} has the name of a registered source")
    return found


class _GuardedHttp:
    """`ProviderHttp` over the server's own client: passthrough guard, retries, breaker."""

    def __init__(self, http: UpstreamHttp, settings: Settings, sleep: SleepFn, upstream: str):
        self._http, self._settings, self._sleep, self._upstream = http, settings, sleep, upstream

    async def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> object:
        response = await authorized_get(
            self._http,
            None,
            path,
            dict(params or {}),
            upstream=self._upstream,
            settings=self._settings,
            sleep=self._sleep,
            extra_headers=headers,
        )
        return response.json_body


class ProviderService:
    """The runtime's side of the boundary: quotas, credentials, one client per provider."""

    def __init__(
        self,
        settings: Settings,
        adapters: Mapping[str, ProviderAdapter],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: SleepFn | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self.adapters = dict(adapters)
        self._sleep = sleep or asyncio.sleep
        self._ledgers = {
            name: CreditLedger(adapter.quota.requests_per_hour, clock=clock)
            for name, adapter in self.adapters.items()
        }
        self._clients = {
            name: UpstreamHttp(
                settings,
                f"byo:{name}",
                adapter.base_url or "http://provider.invalid",
                transport=transport,
                sleep=self._sleep,
            )
            for name, adapter in self.adapters.items()
        }

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()

    def requests_this_hour(self, name: str) -> int:
        return self._ledgers[name].spent()

    def _credentials(self, adapter: ProviderAdapter) -> dict[str, str]:
        """The adapter's credentials, read now, by name. Missing ones are refused by name."""
        values = {name: os.environ.get(name, "") for name in adapter.credential_env}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ToolFailure(
                code="credentials_missing",
                message=f"Provider {adapter.name!r} needs {', '.join(missing)}, which is not set.",
                hint="The operator sets the provider's credentials in the environment. Do not "
                "retry until they have.",
                details={"provider": adapter.name, "variables": missing},
            )
        # Before the first request, so not even a failed one can log them.
        register_secret_values(values.values())
        return values

    async def flights(self, name: str, request: FlightsRequest) -> list[ProviderFlight]:
        adapter = self.adapters[name]
        credentials = self._credentials(adapter)
        ledger = self._ledgers[name]
        spent = ledger.spent()
        if spent + 1 > ledger.budget:
            raise ToolFailure(
                code="local_budget_exhausted",
                message=f"The quota declared for provider {name!r} is used up ({spent}/"
                f"{ledger.budget} requests this hour).",
                hint="The quota mirrors the customer's contract. Wait for the hour to roll.",
                details={"provider": name, "requests_this_hour": spent, "budget": ledger.budget},
            )
        ledger.charge(1)
        http = _GuardedHttp(self._clients[name], self._settings, self._sleep, f"provider {name}")
        try:
            raw = await adapter.fetch(request, credentials, http)
            flights = list(adapter.normalise(raw, request))
        except ToolFailure:
            raise
        except Exception as exc:  # noqa: BLE001 - an adapter's failure is data, never a crash
            # The class only. Its message can hold a URL, and a URL can hold a key.
            raise ToolFailure(
                code="provider_failed",
                message=f"Provider {name!r} did not answer ({type(exc).__name__}).",
                hint="Treat this provider's data as unavailable; it says nothing about the "
                "flights themselves.",
                details={"provider": name, "error_class": type(exc).__name__},
            ) from None
        return flights
