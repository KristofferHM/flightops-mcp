"""The commercial edition: licence classes, the startup gate, attribution, the BYO boundary.

Each test here is one line of the brief's acceptance list, run rather than described.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import inspect
import sys
import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client

import dashboard.__main__ as board_main
from dashboard.app import create_app
from dashboard.gate import Gate
from dashboard.licensing import DEMO_STRIP, STRIP_MARKER, attribution, index_page
from dashboard.service import BoardService
from dashboard.sources import LiveSource
from flightops_mcp import adsblol, airports, opensky, ops, provenance, schiphol, weather
from flightops_mcp import server as srv
from flightops_mcp.airports import AirportDB, build_from_csv
from flightops_mcp.config import Settings
from flightops_mcp.edition import DEMO_PLAN, EditionRefusedError, check_edition
from flightops_mcp.licences import REGISTRY, Licence, UnlicensedSourceError, licensed
from flightops_mcp.providers import clear_registered, register_provider
from flightops_mcp.providers_example import EXAMPLE_PROVIDER
from flightops_mcp.runtime import Runtime, set_runtime

from .conftest import FIXTURES, LogCapture, SleepRecorder
from .test_server_tools import ALL_TOOLS, EXPECTED_TOOLS, call

NOW = int(datetime(2026, 11, 20, 12, 0, tzinfo=UTC).timestamp())
REPO = Path(__file__).resolve().parent.parent
METAR = [
    {
        "icaoId": "ESGG",
        "reportTime": "2026-11-20T11:50:00Z",
        "rawOb": "ESGG 201150Z 24008KT 9999 BKN030 08/05 Q1008",
        "fltCat": "VFR",
    }
]
# adsb.lol's /v2/point answer around ESGG, in its documented shape (openapi 0.0.2).
ADSB_POINT = {
    "ac": [
        {
            "hex": "4aca7e",
            "type": "adsb_icao",
            "flight": "SAS123  ",
            "alt_baro": 35000,
            "gs": 450.0,
            "track": 90.0,
            "baro_rate": -640,
            "lat": 57.70,
            "lon": 12.30,
            "seen": 1.4,
            "messages": 120,
            "mlat": [],
            "tisb": [],
            "rssi": -20.1,
        },
        {
            "hex": "~0abc12",
            "type": "tisb_other",
            "alt_baro": "ground",
            "gs": 6.0,
            "lat": 57.663,
            "lon": 12.279,
            "seen": 3.0,
            "messages": 4,
            "mlat": [],
            "tisb": [],
            "rssi": -30.0,
        },
    ],
    "now": NOW * 1000,
    "ctime": NOW * 1000,
    "ptime": 2,
    "total": 2,
    "msg": "No error",
}
# Everything the commercial edition may list, with adsb.lol switched on.
COMMERCIAL_TOOLS = {
    "resolve_airport",
    "search_airports",
    "airport_weather",
    "weather_overview",
    "airport_live_traffic",
    "disruption_triage",
    "upstream_status",
}
NEW_FIELDS = {"edition", "commercial_use", "sources"}


def _dated(handler: Any) -> Any:
    def answer(request: httpx.Request) -> httpx.Response:
        response: httpx.Response = handler(request)
        response.headers["Date"] = format_datetime(datetime.fromtimestamp(NOW, UTC), usegmt=True)
        return response

    return answer


class World:
    """Mocked upstreams that remember every request, by host."""

    def __init__(self) -> None:
        self.requests: list[httpx.URL] = []
        self.provider_rows: list[dict[str, Any]] = []

    def _seen(self, request: httpx.Request) -> None:
        self.requests.append(request.url)

    def opensky(self, request: httpx.Request) -> httpx.Response:
        self._seen(request)
        if "/flights/" in request.url.path:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"time": NOW, "states": []})

    def weather(self, request: httpx.Request) -> httpx.Response:
        self._seen(request)
        if request.url.path.endswith("/metar"):
            return httpx.Response(200, json=METAR)
        return httpx.Response(204)

    def adsblol(self, request: httpx.Request) -> httpx.Response:
        self._seen(request)
        return httpx.Response(200, json=ADSB_POINT)

    def provider(self, request: httpx.Request) -> httpx.Response:
        self._seen(request)
        return httpx.Response(200, json={"rows": self.provider_rows})

    def token(self, _request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})

    def hosts(self) -> set[str]:
        return {url.host for url in self.requests}


def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    database = tmp_path / "airports.sqlite3"
    if not database.is_file():
        build_from_csv(FIXTURES / "airports_sample.csv", database)
    values: dict[str, Any] = {
        "airports_db": database,
        "backoff_base_s": 0.0,
        "max_retries": 0,
        "avwx_base_url": "https://noaa.test/api/data",
        "opensky_base_url": "https://opensky.test/api",
        "adsblol_base_url": "https://adsblol.test",
    }
    return Settings(_env_file=None, **{**values, **overrides})  # type: ignore[call-arg]


def build(tmp_path: Path, world: World, **overrides: Any) -> Runtime:
    runtime = Runtime.build(
        settings_for(tmp_path, **overrides),
        opensky_transport=httpx.MockTransport(_dated(world.opensky)),
        token_transport=httpx.MockTransport(world.token),
        weather_transport=httpx.MockTransport(_dated(world.weather)),
        adsblol_transport=httpx.MockTransport(_dated(world.adsblol)),
        provider_transport=httpx.MockTransport(_dated(world.provider)),
        sleep=SleepRecorder(),
        clock=lambda: float(NOW),
        data_clock=lambda: float(NOW),
    )
    set_runtime(runtime)
    return runtime


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    yield
    set_runtime(None)
    clear_registered()


# --- 1. The registry: a source without a class is an import error ------------------


def test_a_source_without_a_licence_entry_fails_import_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "scraped_feed_adapter.py"
    module.write_text(
        "from flightops_mcp.licences import licensed\nLICENCE = licensed('scraped-feed')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ImportError, match="'scraped-feed'") as raised:
        importlib.import_module("scraped_feed_adapter")

    assert isinstance(raised.value, UnlicensedSourceError)
    assert raised.value.source == "scraped-feed"
    assert "scraped_feed_adapter" not in sys.modules


def test_every_adapter_declares_its_registry_entry_at_import() -> None:
    assert airports.LICENCE is REGISTRY["ourairports"]
    assert weather.LICENCE is REGISTRY["aviationweather"]
    assert opensky.LICENCE is REGISTRY["opensky"]
    assert schiphol.LICENCE is REGISTRY["schiphol"]
    assert ops.LICENCE is REGISTRY["ops_api"]
    assert adsblol.LICENCE is REGISTRY["adsblol"]
    assert licensed("adsblol") is REGISTRY["adsblol"]


def test_the_registry_holds_the_checked_table() -> None:
    """The classes and the wording of the table checked on 2026-11-20, not a paraphrase."""
    classes = {source: terms.licence_class for source, terms in REGISTRY.items()}
    assert classes == {
        "aviationweather": "A",
        "ourairports": "A",
        "adsblol": "B",
        "opensky": "C",
        "aviationstack": "C",
        "aerodatabox": "C",
        "schiphol": "C",
        "ops_api": "D",
    }
    opensky_terms = " ".join(REGISTRY["opensky"].obligations)
    assert "Any use by a for-profit or commercial entity requires written permission" in (
        opensky_terms
    )
    assert "regardless of the entity's non-profit status" in opensky_terms
    assert "(ODbL) 1.0" in REGISTRY["adsblol"].licence_name
    assert "derived database used publicly must be offered under ODbL" in " ".join(
        REGISTRY["adsblol"].obligations
    )
    assert "$49.99/month for 10,000 requests" in " ".join(REGISTRY["aviationstack"].obligations)
    assert "100 requests/month, non-commercial" in " ".join(REGISTRY["aviationstack"].obligations)
    assert "not permitted for mission-critical or safety-critical use" in " ".join(
        REGISTRY["aerodatabox"].obligations
    )
    for terms in REGISTRY.values():
        assert terms.name and terms.licence_name and terms.attribution and terms.obligations


def test_a_class_d_source_cannot_claim_it_may_be_cached() -> None:
    with pytest.raises(ValueError, match="class D source may not be cached"):
        Licence(
            "X", "D", "contract", "X.", ("o",), may_cache=True, may_redistribute=False, terms_url=""
        )


def test_every_tool_has_a_source_route_as_it_has_a_scope() -> None:
    assert set(provenance.TOOL_SOURCES) == ALL_TOOLS
    with pytest.raises(provenance.UnsourcedToolError):
        provenance.route_for("a_tool_nobody_sourced")


# --- 2. Editions: the startup gate --------------------------------------------------

REFUSED = [
    ("opensky_client_id", "someone", "FLIGHTOPS_OPENSKY_CLIENT_ID", "OpenSky Network"),
    ("opensky_client_secret", "s3", "FLIGHTOPS_OPENSKY_CLIENT_SECRET", "OpenSky Network"),
    ("opensky_anonymous", True, "FLIGHTOPS_OPENSKY_ANONYMOUS", "OpenSky Network"),
    ("aviationstack_access_key", "k", "FLIGHTOPS_AVIATIONSTACK_ACCESS_KEY", "AviationStack"),
    ("aerodatabox_api_key", "k", "FLIGHTOPS_AERODATABOX_API_KEY", "AeroDataBox"),
    ("schiphol_client_id", "c", "FLIGHTOPS_SCHIPHOL_CLIENT_ID", "Schiphol Public Flight API"),
]


@pytest.mark.parametrize(("field", "value", "variable", "name"), REFUSED)
def test_commercial_refuses_every_class_c_source_by_its_variable(
    tmp_path: Path, field: str, value: Any, variable: str, name: str
) -> None:
    with pytest.raises(EditionRefusedError) as refused:
        check_edition(settings_for(tmp_path, edition="commercial", **{field: value}))

    message = str(refused.value)
    assert variable in message
    assert f"configures {name}, licence class C" in message
    assert REGISTRY[refused.value.refused[0].source].licence_name in message


def test_the_exact_refusal_message(tmp_path: Path) -> None:
    with pytest.raises(EditionRefusedError) as refused:
        check_edition(settings_for(tmp_path, edition="commercial", opensky_client_id="x"))

    assert str(refused.value) == (
        "FLIGHTOPS_EDITION=commercial refuses to start.\n"
        "  FLIGHTOPS_OPENSKY_CLIENT_ID configures OpenSky Network, licence class C "
        "(commercial terms; OpenSky Network Terms of Use).\n"
        "The commercial edition serves licence classes A and B only. Unset "
        "FLIGHTOPS_OPENSKY_CLIENT_ID, or choose the edition whose terms you hold "
        "(docs/COMMERCIAL.md)."
    )


def test_commercial_names_every_refused_variable_at_once(tmp_path: Path) -> None:
    settings = settings_for(
        tmp_path,
        edition="commercial",
        opensky_client_id="x",
        opensky_client_secret="y",
        aerodatabox_api_key="z",
        ops_api_base_url="https://ops.test",
    )
    with pytest.raises(EditionRefusedError) as refused:
        check_edition(settings)

    assert [item.variable for item in refused.value.refused] == [
        "FLIGHTOPS_OPENSKY_CLIENT_ID",
        "FLIGHTOPS_OPENSKY_CLIENT_SECRET",
        "FLIGHTOPS_AERODATABOX_API_KEY",
        "FLIGHTOPS_OPS_API_BASE_URL",
    ]
    assert "licence class D (customer-supplied" in str(refused.value)


def test_byo_allows_the_customers_own_system_and_still_refuses_class_c(tmp_path: Path) -> None:
    plan = check_edition(settings_for(tmp_path, edition="byo", ops_api_base_url="https://o.test"))
    assert plan.allows("ops_api") and not plan.allows("opensky")

    with pytest.raises(EditionRefusedError, match="FLIGHTOPS_SCHIPHOL_CLIENT_SECRET"):
        check_edition(settings_for(tmp_path, edition="byo", schiphol_client_secret="s"))


def test_a_runtime_cannot_be_built_around_the_gate(tmp_path: Path) -> None:
    with pytest.raises(EditionRefusedError):
        Runtime.build(settings_for(tmp_path, edition="commercial", opensky_anonymous=True))


def test_the_server_refuses_to_start_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # no developer .env
    monkeypatch.setenv("FLIGHTOPS_EDITION", "commercial")
    monkeypatch.setenv("FLIGHTOPS_OPENSKY_CLIENT_ID", "someone")

    with pytest.raises(SystemExit) as stopped:
        srv.main(["--transport", "stdio"])

    assert stopped.value.code == 2
    error = capsys.readouterr().err
    assert "FLIGHTOPS_OPENSKY_CLIENT_ID configures OpenSky Network, licence class C" in error


def test_the_board_refuses_to_start_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLIGHTOPS_EDITION", "commercial")
    monkeypatch.setenv("FLIGHTOPS_AVIATIONSTACK_ACCESS_KEY", "paid-key")
    monkeypatch.setenv("FLIGHTOPS_AIRPORTS_DB", str(tmp_path / "airports.sqlite3"))

    with pytest.raises(SystemExit) as stopped:
        board_main.main([])

    assert stopped.value.code == 2
    assert "FLIGHTOPS_AVIATIONSTACK_ACCESS_KEY configures AviationStack" in (
        capsys.readouterr().err
    )


def test_the_board_does_not_replay_outside_the_demo(tmp_path: Path) -> None:
    args = board_main.argparse.Namespace(host="127.0.0.1", replay=tmp_path / "x.jsonl", record=None)
    with pytest.raises(board_main.EditionMismatch, match="does not replay"):
        board_main.build(args, settings_for(tmp_path, edition="commercial"))


# --- 3. commercial + A/B only: boots, and every payload carries class and attribution


async def test_commercial_with_open_sources_boots_and_attributes_every_payload(
    tmp_path: Path,
) -> None:
    world = World()
    runtime = build(tmp_path, world, edition="commercial", adsblol_enabled=True)
    server = srv.create_server(edition=runtime.edition)

    async with Client(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        payloads = [
            await call(client, "resolve_airport", {"query": "ESGG"}),
            await call(client, "search_airports", {"query": "ES"}),
            await call(client, "airport_weather", {"airport": "ESGG"}),
            await call(client, "weather_overview", {"airports": ["ESGG"]}),
            await call(client, "airport_live_traffic", {"airport": "ESGG"}),
            await call(client, "disruption_triage", {"airport": "ESGG"}),
        ]
        status = await call(client, "upstream_status", {})

    assert listed == COMMERCIAL_TOOLS
    for payload in payloads:
        assert payload["ok"] is True, payload
        assert payload["edition"] == "commercial"
        assert payload["commercial_use"] == "permitted"
        assert payload["sources"], payload
        for record in payload["sources"]:
            assert record["licence_class"] in {"A", "B"}
            assert record["attribution"] and record["licence_name"] and record["fetched_at"]
    # No request reached a class-C upstream.
    assert world.hosts() == {"noaa.test", "adsblol.test"}
    classes = {item["source"]: item["licence_class"] for item in status["licences"]["sources"]}
    assert classes == {"aviationweather": "A", "ourairports": "A", "adsblol": "B"}
    assert status["edition"] == "commercial"


async def test_adsblol_traffic_keeps_opensky_units_and_carries_odbl(tmp_path: Path) -> None:
    build(tmp_path, World(), edition="commercial", adsblol_enabled=True)

    payload = await srv.airport_live_traffic("ESGG")

    assert payload["source"]["licence_class"] == "B"
    assert "Open Database License (ODbL) 1.0" in payload["source"]["attribution"]
    by_hex = {a["icao24"]: a for a in payload["aircraft"]}
    airborne = by_hex["4aca7e"]
    assert airborne["callsign"] == "SAS123"
    assert airborne["baro_altitude_m"] == pytest.approx(10668.0)
    assert airborne["velocity_ms"] == pytest.approx(231.5, abs=0.01)
    assert airborne["vertical_rate_ms"] == pytest.approx(-3.25, abs=0.01)
    assert airborne["last_contact"] == int(NOW - 1.4)
    assert by_hex["~0abc12"]["on_ground"] is True
    assert by_hex["~0abc12"]["baro_altitude_m"] is None
    assert payload["snapshot_current"] is True
    assert payload["bookkeeping"]["credits_spent"] == 0
    assert payload["bookkeeping"]["requests_this_hour"] == 1


async def test_the_triage_names_the_leg_this_edition_cannot_see(tmp_path: Path) -> None:
    build(tmp_path, World(), edition="commercial", adsblol_enabled=True)

    triage = await srv.disruption_triage("ESGG")

    assert triage["legs"]["movements"]["source"] == "none in this edition"
    assert triage["legs"]["traffic"]["source"] == "adsb.lol /v2/point"
    assert "movements:not_in_edition" in triage["degraded"]
    assert triage["assessment"]["severity"] == "unknown"
    assert {r["name"] for r in triage["sources"]} == {"AviationWeather.gov (NOAA)", "adsb.lol"}


async def test_without_a_traffic_source_traffic_is_refused_before_any_request(
    tmp_path: Path,
) -> None:
    world = World()
    runtime = build(tmp_path, world, edition="commercial")

    payload = await srv.airport_live_traffic("ESGG")
    snapshot = await srv.airspace_snapshot("europe")

    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_in_edition"
    assert payload["edition"] == "commercial"
    assert "licence class C" in snapshot["error"]["message"]
    assert world.requests == []
    async with Client(srv.create_server(edition=runtime.edition)) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "airport_live_traffic" not in listed
    assert listed == COMMERCIAL_TOOLS - {"airport_live_traffic"}


async def test_outside_the_demo_descriptions_and_instructions_name_their_sources(
    tmp_path: Path,
) -> None:
    runtime = build(tmp_path, World(), edition="commercial", adsblol_enabled=True)

    async with Client(srv.create_server(edition=runtime.edition)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert "adsb.lol (licence class B" in (tools["airport_live_traffic"].description or "")
    assert "commercial edition" in srv.instructions_for(runtime.edition)
    assert srv.instructions_for(DEMO_PLAN) == srv.INSTRUCTIONS


# --- 4. byo + a D adapter: served, never held, never logged -----------------------

CREDENTIAL = "CANARY-BYO-KEY-7f3a9c"
DATA = "CANARY-FLIGHT-91x"
ADAPTER_SOURCE = """
from flightops_mcp.licences import Licence
from flightops_mcp.providers import ProviderAdapter, ProviderFlight, Quota

LICENCE = Licence(
    name="Acme Schedules",
    licence_class="D",
    licence_name="Acme customer contract",
    attribution="Schedules: Acme, under the customer's own licence.",
    obligations=("Customer-supplied.",),
    may_cache=False,
    may_redistribute=False,
    terms_url="",
)


async def fetch(request, credentials, http):
    # The key in the query string on purpose: httpx logs every request URL.
    return await http.get_json(
        "/flights", params={"airport": request.icao, "key": credentials["ACME_KEY"]}
    )


def normalise(raw, request):
    return [
        ProviderFlight(row["no"], "departure", None, row["at"], None, None, row["state"])
        for row in raw["rows"]
    ]


async def failing_fetch(request, credentials, http):
    raise RuntimeError("upstream said no to key " + credentials["ACME_KEY"])


ADAPTER = ProviderAdapter(
    name="acme",
    licence=LICENCE,
    fetch=fetch,
    normalise=normalise,
    quota=Quota(requests_per_hour=5),
    credential_env=("ACME_KEY",),
    base_url="https://acme.test/api",
)
FAILING = ProviderAdapter(
    name="acme_down",
    licence=LICENCE,
    fetch=failing_fetch,
    normalise=normalise,
    quota=Quota(requests_per_hour=5),
    credential_env=("ACME_KEY",),
)
"""
CORE = ("src/flightops_mcp/server.py", "dashboard/board.py", "src/flightops_mcp/cache.py")


def _files(root: Path) -> list[tuple[Path, bytes]]:
    """Every file under `root` with its bytes: what this test run wrote to disk."""
    return [(path, path.read_bytes()) for path in root.rglob("*") if path.is_file()]


def _digests() -> dict[str, str]:
    return {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in CORE}


@pytest.fixture
def outside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """An adapter module that lives outside the package, as a customer's would."""
    folder = tmp_path / "customer"
    folder.mkdir()
    (folder / "acme_adapter.py").write_text(textwrap.dedent(ADAPTER_SOURCE), encoding="utf-8")
    monkeypatch.syspath_prepend(str(folder))
    monkeypatch.setenv("ACME_KEY", CREDENTIAL)
    yield "acme_adapter"
    sys.modules.pop("acme_adapter", None)


async def test_byo_serves_a_d_adapter_and_writes_nothing_of_it_down(
    tmp_path: Path, outside: str, logs: LogCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    world = World()
    world.provider_rows = [{"no": DATA, "at": "2026-11-20T12:30:00Z", "state": "scheduled"}]
    before = _digests()
    runtime = build(tmp_path, world, edition="byo", byo_providers=f"{outside}:ADAPTER")

    payload = await srv.provider_flights("ESGG")

    assert payload["ok"] is True, payload
    assert [f["flight"] for f in payload["flights"]] == [DATA]
    assert payload["source"]["licence_class"] == "D"
    assert payload["sources"][0]["attribution"].startswith("Schedules: Acme")
    assert payload["edition"] == "byo"
    assert world.requests[0].params["key"] == CREDENTIAL, "the credential came from the env"
    # Never held: not in the tool cache, not in the payload, not in a log, not on disk.
    assert len(runtime.cache) == 0
    assert CREDENTIAL not in str(payload)
    assert "HTTP Request" in logs.text, "httpx logged the request, so the scrub was tested"
    assert CREDENTIAL not in logs.text
    assert DATA not in logs.text
    for path, content in _files(tmp_path):
        assert CREDENTIAL.encode() not in content, path
        assert DATA.encode() not in content, path
    # Registered from outside the package: no core module was edited to do it.
    assert _digests() == before
    assert "acme" not in provenance.TOOL_SOURCES


async def test_a_failing_adapter_reports_a_code_and_never_its_message(
    tmp_path: Path, outside: str, logs: LogCapture
) -> None:
    build(tmp_path, World(), edition="byo", byo_providers=f"{outside}:FAILING")

    payload = await srv.provider_flights("ESGG")

    assert payload["error"]["code"] == "provider_failed"
    assert payload["error"]["details"]["error_class"] == "RuntimeError"
    assert CREDENTIAL not in str(payload)
    assert CREDENTIAL not in logs.text


async def test_a_d_adapter_is_listed_only_where_the_edition_allows_it(
    tmp_path: Path, outside: str
) -> None:
    runtime = build(tmp_path, World(), edition="byo", byo_providers=f"{outside}:ADAPTER")
    async with Client(srv.create_server(edition=runtime.edition)) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert "provider_flights" in listed

    with pytest.raises(EditionRefusedError, match="FLIGHTOPS_BYO_PROVIDERS configures Acme"):
        check_edition(
            settings_for(tmp_path, edition="commercial", byo_providers=f"{outside}:ADAPTER")
        )


async def test_the_example_provider_is_behind_its_flag_and_labelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "example" not in check_edition(settings_for(tmp_path, edition="byo")).providers
    monkeypatch.setenv("FLIGHTOPS_EXAMPLE_PROVIDER_KEY", "any-value")
    build(tmp_path, World(), edition="byo", byo_example_provider=True)

    payload = await srv.provider_flights("ESGG", direction="departure")

    assert payload["ok"] is True
    assert {f["flight"][:4] for f in payload["flights"]} == {"TEST"}
    assert any("invented, not real" in line for line in payload["unknowns"])
    assert "Invented flights, not real data" in payload["source"]["attribution"]


async def test_a_provider_registered_from_code_is_served(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FLIGHTOPS_EXAMPLE_PROVIDER_KEY", "any-value")
    register_provider(EXAMPLE_PROVIDER)
    build(tmp_path, World(), edition="byo")

    payload = await srv.provider_flights("ESGG", provider="example")

    assert payload["ok"] is True
    assert payload["bookkeeping"]["quota_per_hour"] == 60


# --- 5. The board: a footer built from what was served -----------------------------


class Canned:
    """A tool source that answers from a table, so the footer's input is chosen exactly."""

    recorded_at = None

    def __init__(self, answers: dict[str, dict[str, Any]]) -> None:
        self._answers = answers

    async def call(self, tool: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(self._answers.get(tool, {"ok": False, "error": {"code": "x"}}))


def _record(source: str) -> dict[str, Any]:
    terms = REGISTRY[source]
    return {
        "name": terms.name,
        "licence_class": terms.licence_class,
        "licence_name": terms.licence_name,
        "attribution": terms.attribution,
        "fetched_at": "2026-11-20T12:00:00+00:00",
    }


def _board(tmp_path: Path, traffic_source: str) -> BoardService:
    database = settings_for(tmp_path).airports_db
    listed = [a for a in AirportDB(database).board() if a["ident"] == "ESGG"]
    answers = {
        "airport_weather": {
            "ok": True,
            "metar": [],
            "taf": [],
            "sources": [_record("aviationweather")],
        },
        "weather_overview": {"ok": True, "airports": {}, "sources": [_record("aviationweather")]},
        "airport_live_traffic": {
            "ok": True,
            "counts": {"returned": 0},
            "aircraft": [],
            "sources": [_record(traffic_source)],
        },
        "upstream_status": {"ok": True, "sources": []},
    }
    return BoardService(Canned(answers), listed)


ODBL = REGISTRY["adsblol"].attribution


async def test_the_footer_credits_odbl_whenever_adsblol_served_part_of_the_page(
    tmp_path: Path,
) -> None:
    service = _board(tmp_path, "adsblol")

    board = await service.snapshot("europe")
    detail = await service.detail("ESGG")

    assert ODBL not in [line["text"] for line in board["attribution"]]
    assert ODBL in [line["text"] for line in detail["attribution"]]
    assert {line["licence_class"] for line in detail["attribution"]} == {"A", "B"}


async def test_the_footer_never_credits_odbl_when_adsblol_served_nothing(tmp_path: Path) -> None:
    service = _board(tmp_path, "opensky")

    board = await service.snapshot("europe")
    detail = await service.detail("ESGG")
    texts = [line["text"] for page in (board, detail) for line in page["attribution"]]

    assert ODBL not in texts
    assert REGISTRY["opensky"].attribution in texts
    assert REGISTRY["ourairports"].attribution in texts


def test_a_failed_payload_credits_no_one() -> None:
    failed = {"ok": False, "error": {"code": "x"}, "sources": [_record("adsblol")]}
    assert attribution(failed) == []


def test_the_page_renders_the_footer_from_what_each_response_listed() -> None:
    page = (REPO / "dashboard/static/index.html").read_text(encoding="utf-8")
    script = (REPO / "dashboard/static/board.js").read_text(encoding="utf-8")
    assert '<ul id="attribution">' in page
    assert "body.attribution" in script and "renderAttribution" in script
    assert 'forgetServed("traffic", "track")' in script


# --- 6. demo: stamped, the strip rendered, and nothing else changed ---------------


async def test_the_demo_board_carries_its_strip_and_licences_page() -> None:
    app = create_app(BoardService(LiveSource(), []), Gate("pass phrase"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    ) as client:
        open_page = await client.get("/licences")
        gated = await client.get("/")
    assert open_page.status_code == 200
    assert "may not" in open_page.text and "Be sold" in open_page.text
    assert gated.status_code == 303  # the board itself stays behind the passcode

    app = create_app(BoardService(LiveSource(), []), Gate(None))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    ) as client:
        page = (await client.get("/")).text
    assert DEMO_STRIP in page
    assert "not licensed for commercial use" in page


def test_the_strip_cannot_be_quietly_removed() -> None:
    with pytest.raises(ValueError, match="edition strip"):
        index_page("<html><body></body></html>", DEMO_PLAN)
    assert STRIP_MARKER in (REPO / "dashboard/static/index.html").read_text(encoding="utf-8")


async def test_licences_page_carries_the_derived_database_notice_only_with_odbl(
    tmp_path: Path,
) -> None:
    for enabled in (True, False):
        plan = check_edition(settings_for(tmp_path, edition="commercial", adsblol_enabled=enabled))
        app = create_app(BoardService(LiveSource(), []), Gate(None), edition=plan)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            page = (await client.get("/licences")).text
            index = (await client.get("/")).text
        assert ("Derived database notice" in page) is enabled
        assert DEMO_STRIP not in index


async def test_the_mcp_http_server_publishes_its_licences(tmp_path: Path) -> None:
    plan = check_edition(settings_for(tmp_path, edition="commercial", adsblol_enabled=True))
    server = srv.create_server(edition=plan)
    srv.install_licence_route(server, plan)
    app = server.streamable_http_app(**srv.HTTP_OPTIONS)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        notice = (await client.get("/licences")).json()

    assert notice["edition"] == "commercial"
    assert notice["derived_database_notice"].startswith("This service contains information")
    assert ODBL in notice["attributions"]


DEMO_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("resolve_airport", {"query": "ESGG"}),
    ("search_airports", {"query": "ES"}),
    ("airport_weather", {"airport": "ESGG"}),
    ("weather_overview", {"airports": ["ESGG", "EKCH"]}),
    ("airport_live_traffic", {"airport": "ESGG"}),
    ("airspace_snapshot", {"region": "europe"}),
    ("airport_movements", {"airport": "ESGG"}),
    ("aircraft_track", {"icao24": "4aca7e"}),
    ("disruption_triage", {"airport": "ESGG"}),
    ("upstream_status", {}),
    ("resolve_airport", {"query": "nowhere-at-all"}),
]


@pytest.mark.parametrize(("tool", "arguments"), DEMO_CALLS)
async def test_demo_payloads_differ_from_their_bodies_only_by_the_new_fields(
    tmp_path: Path, tool: str, arguments: dict[str, Any]
) -> None:
    """The demo's behaviour, asserted: the stamp adds the documented fields and nothing else."""
    build(tmp_path, World(), opensky_client_id="id", opensky_client_secret="secret")
    wrapped = getattr(srv, tool)

    served = await wrapped(**arguments)
    # A fresh world for the bare body: a composite's parts must not answer from the cache.
    build(tmp_path, World(), opensky_client_id="id", opensky_client_secret="secret")
    try:
        body = await inspect.unwrap(wrapped)(**arguments)
    except Exception as exc:  # noqa: BLE001 - a failing body answers through `guarded`
        body = exc.to_payload()  # type: ignore[attr-defined]

    assert served["edition"] == "demo"
    assert served["commercial_use"] == "not-permitted"
    stripped = {k: v for k, v in served.items() if k not in NEW_FIELDS}
    unstamped = {k: v for k, v in body.items() if k not in NEW_FIELDS}
    block = stripped.get("source")
    if isinstance(block, dict) and served.get("ok"):
        if len(served["sources"]) == 1:
            assert set(provenance.LICENCE_FIELDS) <= set(block)
        stripped["source"] = {k: v for k, v in block.items() if k not in provenance.LICENCE_FIELDS}
    assert stripped == unstamped


async def test_every_demo_listing_is_unchanged(tmp_path: Path) -> None:
    runtime = build(tmp_path, World(), opensky_client_id="id", opensky_client_secret="secret")
    async with Client(srv.create_server(edition=runtime.edition)) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert listed == EXPECTED_TOOLS
    assert srv.mcp is not None


async def test_stamping_never_writes_into_a_shared_source_block(tmp_path: Path) -> None:
    """`WEATHER_SOURCE` is one dict for every weather answer; the stamp copies it."""
    build(tmp_path, World())
    pristine = dict(srv.WEATHER_SOURCE)

    await srv.airport_weather("ESGG")

    assert pristine == srv.WEATHER_SOURCE
    assert "fetched_at" not in srv.WEATHER_SOURCE
