"""Batched METARs: one request per chunk, and a failure that stays with its own airports.

The mock below behaves as NOAA was measured to behave on 2026-11-19: a hyphenated id
refuses the whole request with 400, an answer stops at 400 records, and more than 400
ids is 200 OK with an error object.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from flightops_mcp import server as srv
from flightops_mcp.airports import build_from_csv
from flightops_mcp.config import Settings
from flightops_mcp.errors import ToolFailure
from flightops_mcp.runtime import Runtime, set_runtime
from flightops_mcp.weather import METAR_CHUNK, NOAA_RECORD_CAP, Batch, WeatherClient

DATE = "Thu, 19 Nov 2026 15:00:00 GMT"
OBSERVED = "2026-11-19T14:50:00Z"
Handler = Callable[[httpx.Request], httpx.Response]


def codes(count: int, prefix: str = "K") -> list[str]:
    return [f"{prefix}{n:03d}" for n in range(count)]


def asked(request: httpx.Request) -> list[str]:
    return request.url.params["ids"].split(",")


def metars(ids: list[str], per_station: int = 1) -> list[dict[str, Any]]:
    return [
        {"icaoId": code, "reportTime": OBSERVED, "fltCat": "VFR", "rawOb": f"{code} {n}"}
        for code in ids
        for n in range(per_station)
    ]


def noaa(
    *,
    refuse: Callable[[list[str]], int | None] = lambda ids: None,
    silent: frozenset[str] = frozenset(),
    per_station: int = 1,
    log: list[list[str]] | None = None,
) -> Handler:
    """NOAA as measured: `refuse` names a status for a request, `silent` holds no METAR."""

    def handle(request: httpx.Request) -> httpx.Response:
        ids = asked(request)
        if log is not None:
            log.append(ids)
        status = refuse(ids)
        if status is not None:
            body = {"status": "error", "error": "Must specify station IDs"}
            return httpx.Response(status, json=body, headers={"Date": DATE})
        if any("-" in code for code in ids):
            return httpx.Response(400, json={"status": "error"}, headers={"Date": DATE})
        body = metars([code for code in ids if code not in silent], per_station)
        return httpx.Response(200, json=body[:NOAA_RECORD_CAP], headers={"Date": DATE})

    return handle


@pytest.fixture
def client(settings: Settings) -> Iterator[Callable[[Handler], WeatherClient]]:
    # No retries inside the HTTP layer, so every request the client makes is one line.
    single = settings.model_copy(update={"max_retries": 0})
    made: list[WeatherClient] = []

    def make(handler: Handler) -> WeatherClient:
        made.append(WeatherClient(single, transport=httpx.MockTransport(handler)))
        return made[-1]

    yield make


def accounted(batch: Batch, wanted: list[str]) -> bool:
    """Every id ends in exactly one of the two maps."""
    return set(batch.reports) | set(batch.failed) == set(wanted) and not (
        set(batch.reports) & set(batch.failed)
    )


# --- chunking -----------------------------------------------------------------------------
async def test_n_ids_are_one_request_per_chunk_of_fifty(
    client: Callable[[Handler], WeatherClient],
) -> None:
    log: list[list[str]] = []
    wanted = codes(120)

    batch = await client(noaa(log=log)).metar_batch(wanted)

    assert METAR_CHUNK == 50
    assert [len(ids) for ids in log] == [50, 50, 20]
    assert [code for ids in log for code in ids] == wanted
    assert batch.requests == 3
    assert accounted(batch, wanted)
    assert all(len(batch.reports[code].items) == 1 for code in wanted)


async def test_a_chunk_rejected_at_the_limit_is_halved_and_passes(
    client: Callable[[Handler], WeatherClient],
) -> None:
    log: list[list[str]] = []

    weather = client(noaa(refuse=lambda ids: 414 if len(ids) > 25 else None, log=log))
    result = await weather.metar_batch(codes(50))

    assert [len(ids) for ids in log] == [50, 25, 25]
    assert result.failed == {}
    assert len(result.reports) == 50


@pytest.mark.parametrize("status", [400, 413, 414])
async def test_a_rejection_splits_until_it_passes(
    client: Callable[[Handler], WeatherClient], status: int
) -> None:
    log: list[list[str]] = []

    weather = client(noaa(refuse=lambda ids: status if len(ids) > 13 else None, log=log))
    batch = await weather.metar_batch(codes(50))

    # Depth first: each half is settled before the next is asked.
    assert [len(ids) for ids in log] == [50, 25, 13, 12, 25, 13, 12]
    assert batch.failed == {}
    assert len(batch.reports) == 50


async def test_one_refused_id_costs_only_itself(
    client: Callable[[Handler], WeatherClient],
) -> None:
    wanted = codes(50)

    weather = client(noaa(refuse=lambda ids: 400 if "K017" in ids else None))
    batch = await weather.metar_batch(wanted)

    assert list(batch.failed) == ["K017"]
    assert batch.failed["K017"].code == "station_rejected"
    assert "NOAA refused a request for K017 alone: HTTP 400" in batch.failed["K017"].message
    assert '"Must specify station IDs"' in batch.failed["K017"].message
    assert len(batch.reports) == 49
    assert accounted(batch, wanted)


async def test_an_identifier_noaa_refuses_is_never_sent(
    client: Callable[[Handler], WeatherClient],
) -> None:
    log: list[list[str]] = []
    wanted = ["ESGG", "CN-0062", "EKCH"]

    batch = await client(noaa(log=log)).metar_batch(wanted)

    assert log == [["ESGG", "EKCH"]]
    assert batch.failed["CN-0062"].code == "station_rejected"
    assert "not an identifier NOAA accepts" in batch.failed["CN-0062"].message
    assert set(batch.reports) == {"ESGG", "EKCH"}


async def test_an_error_object_with_200_is_a_refusal_not_an_empty_answer(
    client: Callable[[Handler], WeatherClient],
) -> None:
    # NOAA's answer to more than 400 ids. Read as a report it made every airport "no METAR".
    def handler(request: httpx.Request) -> httpx.Response:
        if len(asked(request)) > 25:
            body = {"status": "error", "error": "Maximum number of stations exceeded (400)"}
            return httpx.Response(200, json=body, headers={"Date": DATE})
        return httpx.Response(200, json=metars(asked(request)), headers={"Date": DATE})

    batch = await client(handler).metar_batch(codes(50))

    assert batch.requests == 3
    assert batch.failed == {}
    assert all(batch.reports[code].items for code in codes(50))


async def test_an_answer_cut_at_the_record_cap_is_split(
    client: Callable[[Handler], WeatherClient],
) -> None:
    log: list[list[str]] = []
    wanted = codes(50)

    # Ten records a station: fifty stations is 500, cut at 400, so ten would read "no METAR".
    batch = await client(noaa(per_station=10, log=log)).metar_batch(wanted)

    assert [len(ids) for ids in log] == [50, 25, 25]
    assert all(len(batch.reports[code].items) == 10 for code in wanted)


async def test_a_single_station_at_the_cap_is_not_split(
    client: Callable[[Handler], WeatherClient],
) -> None:
    log: list[list[str]] = []

    batch = await client(noaa(per_station=NOAA_RECORD_CAP, log=log)).metar_batch(["ESGG"])

    assert log == [["ESGG"]]
    assert len(batch.reports["ESGG"].items) == NOAA_RECORD_CAP


# --- partial and total failure ------------------------------------------------------------
async def test_a_partial_answer_keeps_the_answered_and_names_the_rest(
    client: Callable[[Handler], WeatherClient],
) -> None:
    wanted = codes(50)
    silent = frozenset(wanted[10:13])

    batch = await client(noaa(silent=silent)).metar_batch(wanted)

    assert batch.requests == 1
    assert batch.failed == {}
    assert sum(1 for code in wanted if batch.reports[code].items) == 47
    assert {code for code in wanted if not batch.reports[code].items} == silent


@pytest.mark.parametrize("status", [503, 429])
async def test_a_failed_chunk_is_asked_once_at_half_size_then_marked_unavailable(
    client: Callable[[Handler], WeatherClient], status: int
) -> None:
    log: list[list[str]] = []
    wanted = codes(50)

    batch = await client(noaa(refuse=lambda ids: status, log=log)).metar_batch(wanted)

    assert [len(ids) for ids in log] == [50, 25, 25]
    assert batch.reports == {}
    assert set(batch.failed) == set(wanted)
    failure = batch.failed["K000"]
    assert failure.code == "upstream_unavailable"
    assert "also when asked again at half size" in failure.message
    assert f"(HTTP {status} for metar 25 stations)" in failure.message


async def test_a_failed_chunk_whose_halves_answer_loses_nothing(
    client: Callable[[Handler], WeatherClient],
) -> None:
    log: list[list[str]] = []

    weather = client(noaa(refuse=lambda ids: 503 if len(ids) == 50 else None, log=log))
    batch = await weather.metar_batch(codes(50))

    assert [len(ids) for ids in log][-2:] == [25, 25]
    assert batch.failed == {}
    assert len(batch.reports) == 50


async def test_a_lone_station_that_fails_is_not_asked_again(
    client: Callable[[Handler], WeatherClient],
) -> None:
    log: list[list[str]] = []

    batch = await client(noaa(refuse=lambda ids: 503, log=log)).metar_batch(["ESGG"])

    assert log == [["ESGG"]]
    assert batch.failed["ESGG"].message.startswith("NOAA did not answer for ESGG: ")
    assert "half size" not in batch.failed["ESGG"].message


async def test_a_transport_failure_is_a_per_airport_reason(
    client: Callable[[Handler], WeatherClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    batch = await client(handler).metar_batch(codes(4))

    assert set(batch.failed) == set(codes(4))
    assert batch.failed["K000"].message.endswith(": ReadTimeout")


async def test_an_open_circuit_marks_airports_and_raises_nothing(
    client: Callable[[Handler], WeatherClient],
) -> None:
    log: list[list[str]] = []
    wanted = codes(300)

    batch = await client(noaa(refuse=lambda ids: 503, log=log)).metar_batch(wanted)

    assert accounted(batch, wanted)
    assert batch.reports == {}
    # After five failures the breaker answers for NOAA, and no request is made.
    assert {failure.code for failure in batch.failed.values()} == {
        "upstream_unavailable",
        "upstream_circuit_open",
    }
    assert len(log) == 5
    assert batch.requests == 18


# --- the tool -----------------------------------------------------------------------------
WORLD_HEADER = (
    "ident,type,name,latitude_deg,longitude_deg,elevation_ft,continent,iso_country,"
    "iso_region,municipality,scheduled_service,iata_code"
)


@pytest.fixture
def fifty(tmp_path: Path, settings: Settings) -> Callable[[Handler], Runtime]:
    database = tmp_path / "fifty.sqlite3"
    csv_path = tmp_path / "fifty.csv"
    rows = [
        f"{code},medium_airport,{code} Airport,50.0,10.0,0,EU,XX,XX-1,,yes,"
        for code in [*codes(50), "XX-0001"]
    ]
    csv_path.write_text("\n".join([WORLD_HEADER, *rows]) + "\n", encoding="utf-8")
    build_from_csv(csv_path, database)

    def install(handler: Handler) -> Runtime:
        runtime = Runtime.build(
            settings.model_copy(update={"airports_db": database}),
            weather_transport=httpx.MockTransport(handler),
        )
        set_runtime(runtime)
        return runtime

    return install


async def test_the_tool_passes_47_through_and_names_the_3(
    fifty: Callable[[Handler], Runtime],
) -> None:
    wanted = codes(50)
    fifty(noaa(silent=frozenset(wanted[:3])))

    answer = await srv.weather_overview(wanted)

    assert answer["ok"] is True
    entries = answer["airports"]
    assert sum(1 for code in wanted if entries[code]["metar"]) == 47
    for code in wanted[:3]:
        assert entries[code]["ok"] is True
        assert entries[code]["metar"] == []
        assert entries[code]["unknowns"][0].startswith("No METAR returned.")
    assert answer["counts"]["with_metar"] == 47
    assert answer["counts"]["requests"] == 1


async def test_the_tool_gives_a_failed_airport_its_own_error(
    fifty: Callable[[Handler], Runtime],
) -> None:
    fifty(noaa(refuse=lambda ids: 400 if "K004" in ids else None))

    answer = await srv.weather_overview([*codes(10), "XX-0001"])

    assert answer["ok"] is True
    for code in ("K004", "XX-0001"):
        entry = answer["airports"][code]
        assert entry["ok"] is False
        assert entry["error"]["code"] == "station_rejected"
        assert entry["error"]["message"]
        assert entry["error"]["hint"]
    assert all(answer["airports"][code]["ok"] for code in codes(10) if code != "K004")
    assert answer["counts"]["failed"] == 2
    assert "2 of 11 airports got no answer from NOAA" in answer["unknowns"][-1]


async def test_one_airport_still_asks_once_with_the_same_parameters(
    fifty: Callable[[Handler], Runtime],
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return noaa()(request)

    fifty(handler)
    await srv.airport_weather("K001")

    assert [(r.url.path.rsplit("/", 1)[-1], dict(r.url.params)) for r in seen] == [
        ("metar", {"ids": "K001", "format": "json", "hours": "2"}),
        ("taf", {"ids": "K001", "format": "json"}),
    ]


# --- what NOAA's answers can hold ---------------------------------------------------------
async def test_one_airport_given_an_error_object_is_a_typed_failure(
    client: Callable[[Handler], WeatherClient],
) -> None:
    body = {"status": "error", "error": "Maximum number of stations exceeded (400)"}
    weather = client(lambda request: httpx.Response(200, json=body, headers={"Date": DATE}))

    with pytest.raises(ToolFailure) as caught:
        await weather.metar_reports("ESGG")

    assert caught.value.code == "upstream_unavailable"
    assert "Maximum number of stations exceeded" in caught.value.details["detail"]


async def test_a_station_nobody_asked_about_is_left_out(
    client: Callable[[Handler], WeatherClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=metars(["ESGG", "ZZZZ"]), headers={"Date": DATE})

    batch = await client(handler).metar_batch(["ESGG", "EKCH"])

    assert set(batch.reports) == {"ESGG", "EKCH"}
    assert batch.reports["EKCH"].items == []


async def test_a_refusal_without_noaa_words_names_the_status(
    client: Callable[[Handler], WeatherClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(414, text="<html>414 Request-URI Too Large</html>")

    batch = await client(handler).metar_batch(["ESGG"])

    assert batch.failed["ESGG"].message == "NOAA refused a request for ESGG alone: HTTP 414."
