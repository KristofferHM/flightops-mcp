"""The fixture: the state of the world a scenario runs in, as what the upstreams answer.

    fixture:
      weather:
        metar: {status: 200, json: [...]}
        taf: {status: 204}
      opensky:
        credentials: false
        token: {status: 401}
        states: {status: 429, headers: {retry-after: "900"}}
        arrival: {error: connect}
        departure: {status: 200, text: "<html>"}
      schiphol:
        credentials: true
        token: {status: 403}
        flights: {status: 200, json: {flights: [...]}}
      ops:
        manifests: {SK1429: [{connecting_flight: SK903, connection_minutes: 35}]}
        seats: {SK1433: 1}
        passengers: {status: 500}
        rebookings: {status: 503}

An endpoint the fixture does not describe answers with nothing: `204` from the
weather service, no aircraft and no movements from OpenSky, no flights from
Schiphol. The operations API is the mock in `services/ops_api`, unless a response
is given in its place.

Bodies follow the shapes the upstreams document, not the shapes this
repository's unit tests happen to use. A fixture is a claim about the outside
world, and the outside world is the one thing the server cannot be asked about.

A manifest row here carries the operational fields only. Nobody is named, in a
scenario file or anywhere downstream of it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

import httpx

from agent.localstack import Upstreams
from agent.scenarios import ScenarioInvalid

_RESPONSE_KEYS: Final = frozenset({"status", "json", "text", "headers", "error"})
_ERRORS: Final[dict[str, type[httpx.TransportError]]] = {
    "connect": httpx.ConnectError,
    "timeout": httpx.ReadTimeout,
}
_SECTIONS: Final[dict[str, frozenset[str]]] = {
    "weather": frozenset({"metar", "taf"}),
    "opensky": frozenset(
        {"credentials", "token", "states", "arrival", "departure", "tracks", "flights"}
    ),
    "schiphol": frozenset({"credentials", "token", "flights"}),
    "ops": frozenset({"manifests", "seats", "passengers", "rebookings"}),
}
_ROW_KEYS: Final = frozenset(
    {"checked_in", "connecting_flight", "connection_minutes", "needs_assistance"}
)

Respond = Callable[[httpx.Request], httpx.Response]
Manifests = dict[str, list[dict[str, Any]]]


def build(fixture: Mapping[str, Any]) -> tuple[Upstreams, Manifests | None]:
    """The upstreams and passenger manifests a fixture describes. Raises `ScenarioInvalid`."""
    unknown = sorted(set(fixture) - set(_SECTIONS))
    if unknown:
        raise ScenarioInvalid(f"fixture has unknown section(s): {', '.join(unknown)}")
    sections: dict[str, Mapping[str, Any]] = {}
    for name, known in _SECTIONS.items():
        section = fixture.get(name) or {}
        if not isinstance(section, dict) or set(section) - known:
            raise ScenarioInvalid(f"fixture.{name} may only hold: {', '.join(sorted(known))}")
        sections[name] = section
    weather, opensky, ops = sections["weather"], sections["opensky"], sections["ops"]
    schiphol = sections["schiphol"]

    defaults = Upstreams()
    credentials = opensky.get("credentials", True)
    if not isinstance(credentials, bool):
        raise ScenarioInvalid("fixture.opensky.credentials must be true or false")
    schiphol_credentials = schiphol.get("credentials", True)
    if not isinstance(schiphol_credentials, bool):
        raise ScenarioInvalid("fixture.schiphol.credentials must be true or false")
    upstreams = Upstreams(
        weather=_weather(_response(weather, "metar"), _response(weather, "taf")),
        opensky=_opensky(
            _response(opensky, "states"),
            _response(opensky, "arrival"),
            _response(opensky, "departure"),
            _response(opensky, "tracks"),
            _response(opensky, "flights"),
        ),
        opensky_token=_response(opensky, "token") or defaults.opensky_token,
        opensky_credentials=credentials,
        schiphol=_response(schiphol, "flights") or defaults.schiphol,
        schiphol_token=_response(schiphol, "token") or defaults.schiphol_token,
        schiphol_credentials=schiphol_credentials,
        ops=_ops(_response(ops, "passengers"), _response(ops, "rebookings")),
        seats=_seats(ops.get("seats")),
    )
    return upstreams, _manifests(ops.get("manifests"))


def _response(section: Mapping[str, Any], name: str) -> Respond | None:
    """What one endpoint answers, or None if the fixture leaves it alone."""
    spec = section.get(name)
    if spec is None:
        return None
    if not isinstance(spec, dict) or set(spec) - _RESPONSE_KEYS:
        raise ScenarioInvalid(f"a response may only hold: {', '.join(sorted(_RESPONSE_KEYS))}")
    error = spec.get("error")
    if error is not None:
        if error not in _ERRORS or len(spec) != 1:
            raise ScenarioInvalid(f"error must be one of {', '.join(_ERRORS)}, and stand alone")
        failure = _ERRORS[error]

        def fail(request: httpx.Request) -> httpx.Response:
            raise failure(f"fixture: {error}", request=request)

        return fail
    status = spec.get("status", 200)
    headers = spec.get("headers") or {}
    if not isinstance(status, int) or not isinstance(headers, dict):
        raise ScenarioInvalid("a response needs an integer status and a mapping of headers")
    if "json" in spec and "text" in spec:
        raise ScenarioInvalid("a response has a json body or a text body, not both")

    def respond(_request: httpx.Request) -> httpx.Response:
        if "json" in spec:
            return httpx.Response(status, json=spec["json"], headers=headers)
        if "text" in spec:
            return httpx.Response(status, text=str(spec["text"]), headers=headers)
        return httpx.Response(status, headers=headers)

    return respond


def _weather(metar: Respond | None, taf: Respond | None) -> Respond:
    def handler(request: httpx.Request) -> httpx.Response:
        chosen = metar if request.url.path.endswith("/metar") else taf
        # NOAA's answer to a valid question it has no data for.
        return chosen(request) if chosen else httpx.Response(204)

    return handler


def _opensky(
    states: Respond | None,
    arrival: Respond | None,
    departure: Respond | None,
    tracks: Respond | None = None,
    flights: Respond | None = None,
) -> Respond:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # OpenSky's documented answer for "nothing for this aircraft" on both is 404.
        if path.endswith("/tracks/all"):
            return tracks(request) if tracks else httpx.Response(404)
        if path.endswith("/flights/aircraft"):
            return flights(request) if flights else httpx.Response(404)
        if path.endswith("/flights/arrival"):
            return arrival(request) if arrival else httpx.Response(200, json=[])
        if path.endswith("/flights/departure"):
            return departure(request) if departure else httpx.Response(200, json=[])
        return states(request) if states else httpx.Response(200, json={"time": 0, "states": []})

    return handler


def _ops(
    passengers: Respond | None, rebookings: Respond | None
) -> Callable[[httpx.Request], httpx.Response | None] | None:
    if passengers is None and rebookings is None:
        return None

    def override(request: httpx.Request) -> httpx.Response | None:
        path = request.url.path
        if passengers and path.endswith("/passengers"):
            return passengers(request)
        if rebookings and path == "/rebookings":
            return rebookings(request)
        return None

    return override


def _seats(raw: Any) -> dict[str, int] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not all(
        isinstance(flight, str) and isinstance(free, int) and not isinstance(free, bool)
        for flight, free in raw.items()
    ):
        raise ScenarioInvalid("fixture.ops.seats maps a flight to its free seats")
    return dict(raw)


def _manifests(raw: Any) -> Manifests | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ScenarioInvalid("fixture.ops.manifests maps a flight to its passengers")
    manifests: Manifests = {}
    for flight, rows in raw.items():
        if not isinstance(flight, str) or not isinstance(rows, list):
            raise ScenarioInvalid("fixture.ops.manifests maps a flight to a list of passengers")
        manifests[flight] = [_row(row) for row in rows]
    return manifests


def _row(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - _ROW_KEYS:
        # Refused, not dropped: a name in a scenario file is a name in the repository.
        raise ScenarioInvalid(
            f"a passenger in a fixture may only hold: {', '.join(sorted(_ROW_KEYS))}"
        )
    return {
        "checked_in": raw.get("checked_in", True),
        "connecting_flight": raw.get("connecting_flight"),
        "connection_minutes": raw.get("connection_minutes"),
        "needs_assistance": raw.get("needs_assistance", False),
    }
