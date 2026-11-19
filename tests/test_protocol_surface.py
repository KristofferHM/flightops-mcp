"""The protocol surface around the tools: annotations, output schemas, progress,
pagination, the airport resource, prompts and completions.

Through the real server object over the SDK's in-memory transport (`Client(mcp)`),
and over the authenticated HTTP app where the HTTP layer is the thing under test.
"""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from mcp import Client, MCPError
from mcp.types import PromptReference, ResourceTemplateReference

from agent.offline import OfflineProvider
from agent.provider import Observation, Turn
from agent.runner import run_clock
from evals.run import run_with
from evals.scenarios import load_all
from flightops_mcp import progress
from flightops_mcp import server as srv
from flightops_mcp.airports import AirportDB, build_from_csv
from flightops_mcp.annotations import TOOL_ANNOTATIONS, UnannotatedToolError
from flightops_mcp.auth import Scopes
from flightops_mcp.config import Settings
from flightops_mcp.runtime import Runtime, set_runtime
from flightops_mcp.schemas import OUTPUT_SCHEMAS
from flightops_mcp.surface import AIRPORT_INDEX_URI, AIRPORT_TEMPLATE, COMPLETION_CAP

from .authkit import JwksEndpoint, signing_key
from .harness import RemoteServer, remote_settings, serve, tool_payload
from .test_server_tools import EXPECTED_TOOLS, LIFR_METAR, VFR_METAR, call, install

KEY = signing_key("key-1")
WORLD_CSV = Path(__file__).parent.parent / "data" / "airports_world.csv"
# The only tools that reach nothing outside this process.
CLOSED_WORLD = {"resolve_airport", "search_airports", "upstream_status"}
# Traffic that makes the triage see something: one airborne aircraft heard now.
ONE_AIRCRAFT = {
    "time": 1_794_699_000,
    "states": [
        [
            "4a91f9",
            "SAS157  ",
            "Sweden",
            1_794_698_995,
            1_794_698_995,
            12.29,
            57.66,
            1200.0,
            False,
            80.0,
            220.0,
            -3.0,
            None,
            1250.0,
            "1234",
            False,
            0,
        ],
    ],
}


@pytest.fixture(autouse=True)
def _reset_runtime() -> Any:
    yield
    set_runtime(None)


def validator(tool: str) -> Draft202012Validator:
    return Draft202012Validator(OUTPUT_SCHEMAS[tool])


def problems(tool: str, payload: dict[str, Any]) -> list[str]:
    return [
        f"{'/'.join(map(str, error.absolute_path)) or '(root)'}: {error.message}"
        for error in validator(tool).iter_errors(payload)
    ]


# --- A: annotations ---------------------------------------------------------------
async def test_every_tool_says_whether_it_writes(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        tools = (await client.list_tools()).tools

    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    for tool in tools:
        hints = tool.annotations
        assert hints is not None, f"{tool.name} has no annotations"
        assert hints.read_only_hint is not None and hints.open_world_hint is not None
        if tool.name == "propose_rebooking":
            continue
        assert hints.read_only_hint is True, tool.name
        # Meaningful only for a write; a read leaves them unset rather than guessing.
        assert hints.destructive_hint is None and hints.idempotent_hint is None, tool.name


async def test_the_write_is_destructive_and_idempotent(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        (write,) = [t for t in (await client.list_tools()).tools if t.name == "propose_rebooking"]

    assert write.annotations is not None
    assert write.annotations.read_only_hint is False
    assert write.annotations.destructive_hint is True
    assert write.annotations.idempotent_hint is True
    assert write.annotations.open_world_hint is True


async def test_only_the_reference_and_process_state_are_a_closed_world(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        tools = (await client.list_tools()).tools

    closed = {t.name for t in tools if t.annotations and t.annotations.open_world_hint is False}
    assert closed == CLOSED_WORLD


def test_the_annotation_table_covers_exactly_the_tools() -> None:
    assert set(TOOL_ANNOTATIONS) == set(Scopes._BY_TOOL) == EXPECTED_TOOLS  # noqa: SLF001


def test_a_tool_without_annotations_cannot_be_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    # Scoped, so that the scope check passes and the annotation check is what refuses it.
    monkeypatch.setitem(Scopes._BY_TOOL, "unannotated_probe", frozenset({Scopes.STATUS_READ}))  # noqa: SLF001
    before = list(srv._TOOLS)  # noqa: SLF001

    async def unannotated_probe() -> dict[str, Any]:
        return {"ok": True}

    with pytest.raises(UnannotatedToolError):
        srv.tool(unannotated_probe)
    assert before == srv._TOOLS, "the unannotated tool must not have been added"  # noqa: SLF001


@pytest.fixture
async def remote(settings: Settings) -> AsyncIterator[RemoteServer]:
    runtime = install(remote_settings(settings), metar=VFR_METAR)
    async with serve(runtime.settings, JwksEndpoint(KEY).transport) as server:
        yield server


async def test_annotations_go_out_under_the_protocols_own_names(remote: RemoteServer) -> None:
    response = await remote.rpc("tools/list", token=KEY.mint())
    tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}

    assert tools["propose_rebooking"]["annotations"] == {
        "title": "Propose a passenger rebooking",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    }
    assert tools["airport_weather"]["annotations"]["readOnlyHint"] is True


# --- B: output schemas and structured content -----------------------------------
def test_every_output_schema_is_a_valid_2020_12_object_schema() -> None:
    assert set(OUTPUT_SCHEMAS) == {"resolve_airport", "airport_weather", "disruption_triage"}
    for schema in OUTPUT_SCHEMAS.values():
        Draft202012Validator.check_schema(schema)
        assert schema["type"] == "object", "the protocol requires an object at the root"


async def test_the_three_tools_publish_their_real_schema(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    for name, schema in OUTPUT_SCHEMAS.items():
        assert tools[name].output_schema == schema


async def test_structured_content_and_the_text_block_carry_the_same_payload(
    settings: Settings,
) -> None:
    install(settings, metar=VFR_METAR)
    async with Client(srv.mcp) as client:
        for name, arguments in (
            ("resolve_airport", {"query": "ESGG"}),
            ("airport_weather", {"airport": "ESGG"}),
            ("disruption_triage", {"airport": "ESGG"}),
        ):
            result = await client.call_tool(name, arguments)
            (block,) = result.content
            assert result.structured_content == json.loads(block.text), name


# Each case is a state of the world; the absent-evidence ones are the point.
CASES: dict[str, dict[str, Any]] = {
    "vfr": {"metar": VFR_METAR},
    "lifr_with_traffic": {"metar": LIFR_METAR, "states": ONE_AIRCRAFT},
    "no_metar_204": {"metar": None},
    "stale_metar": {"metar": [{**VFR_METAR[0], "reportTime": "2026-11-14T21:00:00Z"}]},
    "metar_without_time": {"metar": [{k: v for k, v in VFR_METAR[0].items() if k != "reportTime"}]},
    "no_credentials": {"metar": VFR_METAR, "credentials": False},
}
CALLS: list[tuple[str, dict[str, Any]]] = [
    ("resolve_airport", {"query": "ESGG"}),
    ("resolve_airport", {"query": "GOT"}),
    ("resolve_airport", {"query": "Landvetter"}),
    ("resolve_airport", {"query": "ESGG"}),  # the repeat is a cache hit, with its block
    ("resolve_airport", {"query": "Atlantis"}),
    ("airport_weather", {"airport": "ESGG"}),
    ("airport_weather", {"airport": "ESGG"}),
    ("airport_weather", {"airport": "ZZZZ"}),
    ("disruption_triage", {"airport": "ESGG"}),
    ("disruption_triage", {"airport": "ESGG", "radius_nm": 500}),
    ("disruption_triage", {"airport": "ZZZZ"}),
]


@pytest.mark.parametrize("case", sorted(CASES))
async def test_every_payload_validates_against_its_own_schema(
    case: str, settings: Settings
) -> None:
    install(settings, **CASES[case])
    async with Client(srv.mcp) as client:
        for name, arguments in CALLS:
            payload = await call(client, name, arguments)
            assert problems(name, payload) == [], f"{case}: {name}({arguments})"


class _Recording(OfflineProvider):
    """The CI planner, keeping every payload it was shown."""

    def __init__(self, policy: str, params: Any, seen: list[Observation]) -> None:
        super().__init__(policy, params)
        self._seen = seen

    async def turn(self, observations: Sequence[Observation]) -> Turn:
        self._seen.extend(observations)
        return await super().turn(observations)


async def test_every_payload_in_the_eval_scenarios_validates() -> None:
    """The golden scenarios' worlds, absent evidence included, over the real server."""
    seen: list[tuple[str, str, Observation]] = []
    groups: set[str] = set()
    for scenario in load_all():
        if not set(OUTPUT_SCHEMAS) & {*scenario.expect.required, *scenario.expect.allowed}:
            continue
        observations: list[Observation] = []
        provider = _Recording(scenario.run.policy, scenario.run.params, observations)
        await run_with(scenario, provider, run_clock("offline", scenario.run, lambda: 0.0))
        seen.extend((scenario.id, scenario.group, o) for o in observations)
        groups.add(scenario.group)

    checked = [(sid, o) for sid, _, o in seen if o.call.name in OUTPUT_SCHEMAS]
    for sid, observation in checked:
        assert problems(observation.call.name, observation.payload) == [], sid
    assert groups >= {"happy-path", "refusal", "absent-evidence"}
    absent = [o for sid, group, o in seen if group == "absent-evidence"]
    assert any(o.call.name == "disruption_triage" for o in absent)
    assert any(not o.ok for _, o in checked), "a failure payload must have been checked"
    assert any(
        (o.payload.get("assessment") or {}).get("severity") == "unknown" for _, o in checked
    ), "an unknown severity must have been checked"


async def test_a_payload_that_breaks_the_contract_fails_validation(settings: Settings) -> None:
    """The schemas do work: each change below is one a client would be hurt by."""
    install(settings, metar=VFR_METAR)
    async with Client(srv.mcp) as client:
        weather = await call(client, "airport_weather", {"airport": "ESGG"})
        triage = await call(client, "disruption_triage", {"airport": "ESGG"})
        resolved = await call(client, "resolve_airport", {"query": "ESGG"})
        missing = await call(client, "resolve_airport", {"query": "Atlantis"})
    for name, payload in (
        ("airport_weather", weather),
        ("disruption_triage", triage),
        ("resolve_airport", resolved),
        ("resolve_airport", missing),
    ):
        assert problems(name, payload) == [], name

    def broken(payload: dict[str, Any], change: Any) -> dict[str, Any]:
        copied = copy.deepcopy(payload)
        change(copied)
        return copied

    assert problems("airport_weather", broken(weather, lambda p: p.pop("unknowns")))
    assert problems("airport_weather", broken(weather, lambda p: p["latest_metar"].pop("current")))
    assert problems("airport_weather", broken(weather, lambda p: p.update(surprise=1)))
    assert problems("disruption_triage", broken(triage, lambda p: p["assessment"].pop("severity")))
    assert problems(
        "disruption_triage", broken(triage, lambda p: p["assessment"].update(severity="fine"))
    )
    assert problems("disruption_triage", broken(triage, lambda p: p.update(ok="yes")))
    assert problems("resolve_airport", broken(resolved, lambda p: p.update(matches=[])))
    assert problems("resolve_airport", broken(missing, lambda p: p["error"].pop("code")))


# --- C: progress ------------------------------------------------------------------
class Progress:
    def __init__(self) -> None:
        self.seen: list[tuple[float, float | None, str | None]] = []

    async def __call__(self, done: float, total: float | None, message: str | None) -> None:
        self.seen.append((done, total, message))


async def test_movements_report_one_notification_per_upstream_request(
    settings: Settings,
) -> None:
    install(settings)
    heard = Progress()
    async with Client(srv.mcp) as client:
        result = await client.call_tool(
            "airport_movements", {"airport": "ESGG", "direction": "both"}, progress_callback=heard
        )

    assert not result.is_error
    assert [(done, total) for done, total, _ in heard.seen] == [(1, 2), (2, 2)]
    assert "arrivals" in str(heard.seen[0][2]) and "departures" in str(heard.seen[1][2])


async def test_without_a_progress_token_nothing_is_sent_and_nothing_fails(
    settings: Settings,
) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        result = await client.call_tool("airport_movements", {"airport": "ESGG"})

    assert not result.is_error
    assert (result.structured_content or {}).get("ok") is True


async def test_a_cache_hit_is_not_progress(settings: Settings) -> None:
    """Progress counts upstream requests. A cached answer made none."""
    install(settings)
    first, second = Progress(), Progress()
    async with Client(srv.mcp) as client:
        await client.call_tool("airport_movements", {"airport": "ESGG"}, progress_callback=first)
        await client.call_tool("airport_movements", {"airport": "ESGG"}, progress_callback=second)

    assert len(first.seen) == 2
    assert second.seen == []


async def test_a_direction_never_asked_is_not_counted(settings: Settings) -> None:
    install(settings, credentials=False)
    heard = Progress()
    async with Client(srv.mcp) as client:
        await client.call_tool("airport_movements", {"airport": "ESGG"}, progress_callback=heard)

    assert heard.seen == [], "no request was sent, so there is no progress to report"


async def test_the_regional_snapshot_reports_its_one_request(settings: Settings) -> None:
    install(settings)
    heard = Progress()
    async with Client(srv.mcp) as client:
        await client.call_tool("airspace_snapshot", {"region": "europe"}, progress_callback=heard)

    assert [(done, total) for done, total, _ in heard.seen] == [(1, 1)]


async def test_a_weather_overview_reports_each_noaa_request(settings: Settings) -> None:
    install(settings, metar=VFR_METAR)
    many = [f"E{chr(65 + i // 26)}{chr(65 + i % 26)}A" for i in range(60)] + ["ESGG"]
    # Only airports in the reference are asked; build one that holds all of them.
    db = settings.airports_db
    db.unlink(missing_ok=True)
    rows = ["id,ident,type,name,latitude_deg,longitude_deg,continent,iso_country"]
    rows += [f"{n},{code},large_airport,Probe {code},1,1,EU,SE" for n, code in enumerate(many)]
    csv_path = db.with_suffix(".csv")
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    build_from_csv(csv_path, db)
    heard = Progress()
    async with Client(srv.mcp) as client:
        await client.call_tool("weather_overview", {"airports": many}, progress_callback=heard)

    # 61 airports, 50 per request: two requests, each reported against a total of two.
    assert [(done, total) for done, total, _ in heard.seen] == [(1, 2), (2, 2)]


async def test_a_composite_does_not_report_its_parts_progress(settings: Settings) -> None:
    """The triage calls the movements tool directly, without a request of its own."""
    install(settings, metar=VFR_METAR)
    heard = Progress()
    async with Client(srv.mcp) as client:
        await client.call_tool("disruption_triage", {"airport": "ESGG"}, progress_callback=heard)

    assert heard.seen == []


async def test_progress_outside_a_tracked_block_is_a_no_op() -> None:
    progress.expect(3)
    await progress.upstream_call("nothing is listening")


async def test_a_reporter_that_fails_does_not_fail_the_work() -> None:
    class Broken:
        async def report_progress(self, *_: Any) -> None:
            raise RuntimeError("transport closed")

    async with progress.tracked(Broken(), total=1):
        await progress.upstream_call("lost")


async def test_over_http_with_json_responses_a_token_is_accepted_but_nothing_streams(
    remote: RemoteServer,
) -> None:
    """The deployed HTTP server answers with one JSON body, which has no room for a
    notification. A progress token must not break the call; it is simply not served.
    docs/PROTOCOL.md says so. If this starts streaming, update that page."""
    token = KEY.mint(scp=Scopes.TRAFFIC_READ)
    response = await remote.rpc(
        "tools/call",
        {
            "name": "airport_movements",
            "arguments": {"airport": "ESGG"},
            "_meta": {"progressToken": "probe-1"},
        },
        token=token,
    )

    assert response.headers["content-type"].startswith("application/json")
    assert tool_payload(response)["ok"] is True


# --- D: pagination ------------------------------------------------------------------
async def page(client: Client, **arguments: Any) -> dict[str, Any]:
    return await call(client, "search_airports", arguments)


async def walk(client: Client, **arguments: Any) -> tuple[list[str], int]:
    """Follow nextCursor to the end. Returns every ident served, and the page count."""
    idents: list[str] = []
    pages = 0
    cursor: str | None = None
    while True:
        payload = await page(client, **arguments, **({"cursor": cursor} if cursor else {}))
        assert payload["ok"] is True, payload
        pages += 1
        idents.extend(airport["icao"] for airport in payload["airports"])
        cursor = payload.get("nextCursor")
        if cursor is None:
            return idents, pages


@pytest.fixture
def world(settings: Settings) -> Runtime:
    """The committed world reference: 3,270 airports."""
    build_from_csv(WORLD_CSV, settings.airports_db)
    return install(settings)


async def test_a_full_walk_of_the_world_reaches_every_airport_once(world: Runtime) -> None:
    every = sorted(row["ident"] for row in world.airports.placed())
    async with Client(srv.mcp) as client:
        first = await page(client, limit=100)
        idents, pages = await walk(client, limit=100)

    assert len(every) == 3270
    assert first["counts"] == {"returned": 100, "matching": 3270}
    assert idents == every, "every airport, once, in ICAO order"
    assert len(set(idents)) == len(idents)
    assert pages == 33


async def test_the_default_page_is_twenty_and_the_cap_is_a_hundred(world: Runtime) -> None:
    async with Client(srv.mcp) as client:
        default = await page(client)
        huge = await page(client, limit=10_000)
        tiny = await page(client, limit=0)

    assert len(default["airports"]) == 20
    assert len(huge["airports"]) == 100
    assert len(tiny["airports"]) == 1


async def test_pages_are_stable_and_a_cursor_can_be_replayed(world: Runtime) -> None:
    async with Client(srv.mcp) as client:
        one = await page(client, query="ES", limit=7)
        again = await page(client, query="ES", limit=7)
        second = await page(client, query="ES", limit=7, cursor=one["nextCursor"])
        replay = await page(client, query="ES", limit=7, cursor=one["nextCursor"])

    assert one["airports"] == again["airports"]
    assert second["airports"] == replay["airports"]
    assert {a["icao"] for a in one["airports"]}.isdisjoint(a["icao"] for a in second["airports"])
    assert one["airports"][-1]["icao"] < second["airports"][0]["icao"]


async def test_the_last_page_has_no_cursor(world: Runtime) -> None:
    async with Client(srv.mcp) as client:
        idents, pages = await walk(client, query="ESGG", limit=5)
        exact = await page(client, query="ESGG")

    assert idents == ["ESGG"] and pages == 1
    assert "nextCursor" not in exact


async def test_a_region_walk_is_complete_for_that_continent(world: Runtime) -> None:
    async with Client(srv.mcp) as client:
        idents, _ = await walk(client, region="oceania", limit=50)
        first = await page(client, region="Oceania", limit=50)

    assert idents and len(idents) == first["counts"]["matching"]
    assert all(world.airports.get(code)["continent"] == "OC" for code in idents)  # type: ignore[index]


async def test_a_name_query_and_an_iata_code_both_match(world: Runtime) -> None:
    async with Client(srv.mcp) as client:
        by_name = await page(client, query="landvetter")
        by_iata = await page(client, query="got")

    assert [a["icao"] for a in by_name["airports"]] == ["ESGG"]
    assert "ESGG" in [a["icao"] for a in by_iata["airports"]]


async def test_like_wildcards_in_a_query_are_literal(world: Runtime) -> None:
    async with Client(srv.mcp) as client:
        percent = await page(client, query="%")
        underscore = await page(client, query="_")

    assert percent["counts"]["matching"] == 0
    assert underscore["counts"]["matching"] == 0


async def test_an_empty_result_says_what_it_does_not_show(world: Runtime) -> None:
    async with Client(srv.mcp) as client:
        payload = await page(client, query="Narnia International")

    assert payload["ok"] is True
    assert payload["airports"] == [] and payload["counts"]["matching"] == 0
    assert payload["unknowns"] and "may still exist" in payload["unknowns"][0]
    assert "nextCursor" not in payload


@pytest.mark.parametrize(
    "cursor",
    ["not-a-cursor", "2:ESGG:{digest}", "1::{digest}", "1:ESGG:0000000000000000", "1:ESGG"],
    ids=["garbage", "wrong-version", "no-position", "wrong-digest", "no-digest"],
)
async def test_a_cursor_this_tool_did_not_issue_is_refused(cursor: str, world: Runtime) -> None:
    real = await _first_cursor()
    digest = real.rsplit(":", 1)[1]
    async with Client(srv.mcp) as client:
        payload = await page(client, cursor=cursor.format(digest=digest))

    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_cursor"


async def _first_cursor() -> str:
    async with Client(srv.mcp) as client:
        cursor = (await page(client, limit=1))["nextCursor"]
    assert isinstance(cursor, str)
    return cursor


async def test_a_cursor_does_not_look_like_a_token(world: Runtime) -> None:
    """Base64 of JSON starts with "eyJ", as a JWT does; the leak canaries would fire."""
    cursor = await _first_cursor()

    assert not cursor.startswith("eyJ")
    assert cursor.split(":")[:2] == ["1", world.airports.page(limit=1)[0][0]["ident"]]


async def test_a_cursor_from_another_query_is_refused(world: Runtime) -> None:
    async with Client(srv.mcp) as client:
        es = await page(client, query="ES", limit=3)
        other_query = await page(client, query="EK", cursor=es["nextCursor"])
        other_region = await page(client, query="ES", region="asia", cursor=es["nextCursor"])

    assert other_query["error"]["code"] == "invalid_cursor"
    assert other_region["error"]["code"] == "invalid_cursor"


async def test_an_unknown_region_is_refused_with_the_vocabulary(world: Runtime) -> None:
    async with Client(srv.mcp) as client:
        payload = await page(client, region="atlantis")

    assert payload["error"]["code"] == "invalid_argument"
    assert "north_america" in payload["error"]["message"]


async def test_a_region_needs_a_reference_built_with_continents(settings: Settings) -> None:
    runtime = install(settings)
    old = runtime.airports.path.with_name("old.sqlite3")
    with __import__("sqlite3").connect(old) as connection:
        connection.execute("CREATE TABLE airports (ident TEXT PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO airports VALUES ('ESGG', 'Landvetter')")
    runtime.airports = AirportDB(old)
    async with Client(srv.mcp) as client:
        payload = await page(client, region="europe")

    assert payload["error"]["code"] == "reference_data_outdated"


async def test_search_without_reference_data_says_so(settings: Settings) -> None:
    runtime = install(settings)
    runtime.airports = AirportDB(settings.airports_db.with_name("absent.sqlite3"))
    async with Client(srv.mcp) as client:
        payload = await page(client)

    assert payload["error"]["code"] == "reference_data_missing"


# --- D: completions -----------------------------------------------------------------
TEMPLATE = ResourceTemplateReference(uri=AIRPORT_TEMPLATE)
TRIAGE = PromptReference(name="triage_airport")
MOVEMENTS = PromptReference(name="brief_movements")


async def test_an_empty_prefix_matches_thousands_and_is_capped(world: Runtime) -> None:
    async with Client(srv.mcp) as client:
        result = await client.complete(TEMPLATE, {"name": "icao", "value": ""})

    completion = result.completion
    assert len(completion.values) == COMPLETION_CAP == 100
    assert completion.total == 3270 and completion.has_more is True
    assert completion.values == sorted(completion.values)


async def test_a_one_letter_prefix_is_capped_and_says_how_many_it_left_out(
    world: Runtime,
) -> None:
    async with Client(srv.mcp) as client:
        result = await client.complete(TRIAGE, {"name": "airport", "value": "k"})

    completion = result.completion
    assert completion.total is not None and completion.total > COMPLETION_CAP
    assert len(completion.values) == 100 and completion.has_more is True
    assert all(code.startswith("K") for code in completion.values)


async def test_a_prefix_matching_nothing_is_empty(world: Runtime) -> None:
    async with Client(srv.mcp) as client:
        result = await client.complete(MOVEMENTS, {"name": "airport", "value": "QQ"})

    assert result.completion.values == []
    assert result.completion.total == 0 and result.completion.has_more is False


async def test_a_narrow_prefix_is_complete(world: Runtime) -> None:
    async with Client(srv.mcp) as client:
        result = await client.complete(TEMPLATE, {"name": "icao", "value": "esg"})

    assert "ESGG" in result.completion.values
    assert result.completion.has_more is False
    assert result.completion.total == len(result.completion.values)


async def test_the_direction_argument_completes_from_its_vocabulary(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        all_three = await client.complete(MOVEMENTS, {"name": "direction", "value": ""})
        dep = await client.complete(MOVEMENTS, {"name": "direction", "value": "De"})
        other = await client.complete(TRIAGE, {"name": "something_else", "value": "x"})

    assert all_three.completion.values == ["arrival", "departure", "both"]
    assert dep.completion.values == ["departure"]
    assert other.completion.values == []


async def test_completion_without_reference_data_is_an_error(settings: Settings) -> None:
    runtime = install(settings)
    runtime.airports = AirportDB(settings.airports_db.with_name("absent.sqlite3"))
    async with Client(srv.mcp) as client:
        with pytest.raises(MCPError):
            await client.complete(TEMPLATE, {"name": "icao", "value": "ES"})


# --- E: the resource and the prompts ------------------------------------------------
async def test_an_airport_reads_as_a_json_resource(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        result = await client.read_resource("airport://ESGG")
        lower = await client.read_resource("airport://esgg")

    (content,) = result.contents
    assert content.mime_type == "application/json"
    record = json.loads(content.text)  # type: ignore[union-attr]
    assert record["icao"] == "ESGG" and record["iata"] == "GOT"
    assert record["source"]["name"] == "OurAirports"
    assert lower.contents[0].text == content.text  # type: ignore[union-attr]


async def test_an_unknown_airport_is_a_protocol_error_not_an_empty_document(
    settings: Settings,
) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        with pytest.raises(MCPError) as raised:
            await client.read_resource("airport://ZZZZ")

    assert raised.value.code == -32602
    assert "ZZZZ" in raised.value.error.message


async def test_the_resource_list_describes_what_is_available(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        listed = (await client.list_resources()).resources
        templates = (await client.list_resource_templates()).resource_templates
        index = await client.read_resource(AIRPORT_INDEX_URI)

    assert [str(r.uri) for r in listed] == [AIRPORT_INDEX_URI]
    assert [t.uri_template for t in templates] == [AIRPORT_TEMPLATE]
    assert all(t.mime_type == "application/json" for t in templates)
    described = json.loads(index.contents[0].text)  # type: ignore[union-attr]
    assert described["uri_template"] == AIRPORT_TEMPLATE
    assert described["airports"] == install(settings).airports.count() > 0


async def test_a_missing_reference_is_an_error_on_read(settings: Settings) -> None:
    runtime = install(settings)
    runtime.airports = AirportDB(settings.airports_db.with_name("absent.sqlite3"))
    async with Client(srv.mcp) as client:
        with pytest.raises(MCPError):
            await client.read_resource("airport://ESGG")
        with pytest.raises(MCPError):
            await client.read_resource(AIRPORT_INDEX_URI)


async def test_both_prompts_render_with_their_arguments(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        listed = {p.name: p for p in (await client.list_prompts()).prompts}
        triage = await client.get_prompt("triage_airport", {"airport": "ESGG"})
        brief = await client.get_prompt(
            "brief_movements", {"airport": "Landvetter", "direction": "departure"}
        )
        default = await client.get_prompt("brief_movements", {"airport": "EKCH"})

    assert set(listed) == {"triage_airport", "brief_movements"}
    assert [a.name for a in listed["triage_airport"].arguments or []] == ["airport"]
    assert {a.name: a.required for a in listed["brief_movements"].arguments or []} == {
        "airport": True,
        "direction": False,
    }
    triage_text = triage.messages[0].content.text  # type: ignore[union-attr]
    assert "ESGG" in triage_text and "disruption_triage" in triage_text
    assert "not evidence that operations are normal" in triage_text
    brief_text = brief.messages[0].content.text  # type: ignore[union-attr]
    assert "Landvetter" in brief_text and 'direction "departure"' in brief_text
    assert "departures" in brief_text and "estimates" in brief_text
    default_text = default.messages[0].content.text  # type: ignore[union-attr]
    assert "arrivals and departures" in default_text and 'direction "both"' in default_text


# --- cross-cutting: capabilities, and the HTTP door ---------------------------------
async def test_capabilities_declare_resources_prompts_and_completions(settings: Settings) -> None:
    install(settings)
    async with Client(srv.mcp) as client:
        capabilities = client.server_capabilities

    assert capabilities is not None
    assert capabilities.resources is not None
    assert capabilities.prompts is not None
    assert capabilities.completions is not None
    assert capabilities.tools is not None
    assert capabilities.logging is None, "not offered; docs/PROTOCOL.md says why"


async def test_over_http_reading_the_reference_needs_its_scope(remote: RemoteServer) -> None:
    allowed, other = KEY.mint(scp=Scopes.REFERENCE_READ), KEY.mint(scp=Scopes.WEATHER_READ)
    read = {"uri": "airport://ESGG"}
    complete = {
        "ref": {"type": "ref/resource", "uri": AIRPORT_TEMPLATE},
        "argument": {"name": "icao", "value": "ES"},
    }

    ok_read = (await remote.rpc("resources/read", read, token=allowed)).json()
    refused_read = (await remote.rpc("resources/read", read, token=other)).json()
    ok_complete = (await remote.rpc("completion/complete", complete, token=allowed)).json()
    refused_complete = (await remote.rpc("completion/complete", complete, token=other)).json()
    prompt = (
        await remote.rpc(
            "prompts/get", {"name": "triage_airport", "arguments": {"airport": "X"}}, token=other
        )
    ).json()

    assert json.loads(ok_read["result"]["contents"][0]["text"])["icao"] == "ESGG"
    assert "ESGG" in ok_complete["result"]["completion"]["values"]
    for refused in (refused_read, refused_complete):
        assert refused["error"]["data"]["error"] == "insufficient_scope"
        assert refused["error"]["data"]["required_scopes"] == [Scopes.REFERENCE_READ]
    assert "result" in prompt, "a prompt carries no data and needs no scope"


async def test_over_http_the_search_tool_needs_the_reference_scope(remote: RemoteServer) -> None:
    allowed = await remote.call_tool(
        "search_airports", {"query": "ES"}, token=KEY.mint(scp=Scopes.REFERENCE_READ)
    )
    refused = await remote.call_tool(
        "search_airports", {"query": "ES"}, token=KEY.mint(scp=Scopes.WEATHER_READ)
    )

    assert tool_payload(allowed)["ok"] is True
    assert refused.status_code == 403
