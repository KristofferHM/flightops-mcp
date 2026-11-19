"""A neighbour may answer for an airport with no report, labelled as one, and only on the board."""

from __future__ import annotations

from typing import Any

import pytest

from dashboard import board
from dashboard.service import BoardService
from flightops_mcp.airports import haversine_nm

from .test_board_world import NOW, STATUS, FakeSource

LAT, LON = 57.0, 12.0
# One degree of latitude, in nautical miles, on the haversine's sphere.
NM_PER_DEGREE = haversine_nm(0.0, 0.0, 1.0, 0.0)


def place(ident: str, north_nm: float = 0.0, east_deg: float = 0.0) -> dict[str, Any]:
    return {
        "ident": ident,
        "iata": None,
        "name": f"{ident} Airport",
        "type": "medium_airport",
        "latitude": LAT + north_nm / NM_PER_DEGREE,
        "longitude": LON + east_deg,
        "continent": "EU",
        "municipality": None,
        "iso_country": "SE",
    }


def reported(category: str = "VFR", age_s: int = 720) -> dict[str, Any]:
    return {
        "ok": True,
        "upstream_time_epoch": NOW,
        "metar": [{"flight_category": category, "observation_time": "2026-11-19T14:48:00Z"}],
        "latest_metar": {"age_s": age_s, "current": abs(age_s) <= 90 * 60},
        "unknowns": [],
    }


SILENT = {"ok": True, "upstream_time_epoch": NOW, "metar": [], "latest_metar": {}, "unknowns": []}
FAILED = {"ok": False, "error": {"code": "upstream_unavailable", "message": "down"}}


def views(**payloads: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {ident: board.weather_view(payload) for ident, payload in payloads.items()}


# --- the rule -----------------------------------------------------------------------------
def test_the_nearest_reporting_airport_answers_with_its_distance_and_age() -> None:
    airports = [place("ESGT"), place("ESGG", 18), place("ESGP", 10)]

    found = board.neighbours(
        airports, views(ESGT=SILENT, ESGG=reported("VFR"), ESGP=reported("IFR", 300))
    )

    assert found == {
        "ESGT": {
            "icao": "ESGP",
            "distance_nm": 10,
            "category": "IFR",
            "age_s": 300,
            "text": "No report at ESGT — ESGP (10 nm away) reports IFR, 5 min old",
        }
    }


def test_the_line_reads_as_the_brief_wrote_it() -> None:
    found = board.neighbours(
        [place("ESGT"), place("ESGG", 18)], views(ESGT=SILENT, ESGG=reported())
    )

    assert found["ESGT"]["text"] == "No report at ESGT — ESGG (18 nm away) reports VFR, 12 min old"


def test_a_tie_goes_to_the_lower_icao_code() -> None:
    # Mirror images east and west: the same distance to the last bit.
    airports = [place("ESGT"), place("ESZZ", east_deg=0.2), place("ESAA", east_deg=-0.2)]
    assert haversine_nm(LAT, LON, LAT, LON + 0.2) == haversine_nm(LAT, LON, LAT, LON - 0.2)

    for order in (airports, airports[::-1]):
        found = board.neighbours(order, views(ESGT=SILENT, ESZZ=reported(), ESAA=reported()))
        assert found["ESGT"]["icao"] == "ESAA"


def test_beyond_25_nm_is_not_offered() -> None:
    # 18 nm north and about 17.6 nm east: inside the latitude band, past the cap.
    diagonal = place("ESDD", 18, east_deg=0.546)
    north = place("ESNN", 25.2)
    for far in (diagonal, north):
        distance = haversine_nm(LAT, LON, far["latitude"], far["longitude"])
        assert 25.0 < distance < 25.5

        assert (
            board.neighbours([place("ESGT"), far], views(ESGT=SILENT, **{far["ident"]: reported()}))
            == {}
        )


def test_just_inside_the_cap_is_offered() -> None:
    edge = place("ESEE", 24.99)

    found = board.neighbours([place("ESGT"), edge], views(ESGT=SILENT, ESEE=reported()))

    assert board.NEIGHBOUR_MAX_NM == 25.0
    assert found["ESGT"]["distance_nm"] == 25


def test_exactly_at_the_cap_is_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    edge = place("ESEE", 20.0, east_deg=0.1)
    monkeypatch.setattr(
        board, "NEIGHBOUR_MAX_NM", haversine_nm(LAT, LON, edge["latitude"], edge["longitude"])
    )

    found = board.neighbours([place("ESGT"), edge], views(ESGT=SILENT, ESEE=reported()))

    assert found["ESGT"]["icao"] == "ESEE"


def test_the_latitude_band_keeps_distant_airports_out_of_the_sums(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    measured: list[tuple[float, ...]] = []

    def counting(*points: float) -> float:
        measured.append(points)
        return haversine_nm(*points)

    monkeypatch.setattr(board, "haversine_nm", counting)
    # Just outside 25 nm of latitude either way, and far away: none can be in reach.
    airports = [place("ESGT"), place("ESNO", 25.0 * 1.003), place("ESSO", -25.0 * 1.003)]
    airports += [place(f"EF{n:02d}", 600.0 + n) for n in range(20)]
    every = views(ESGT=SILENT, **{a["ident"]: reported() for a in airports[1:]})

    assert board.neighbours(airports, every) == {}
    assert measured == []


def test_an_airport_without_a_neighbour_does_not_end_the_search() -> None:
    # ESAL is alone, 300 nm south. ESGT, after it, has ESGG 18 nm away.
    airports = [place("ESAL", -300), place("ESGT"), place("ESGG", 18)]

    found = board.neighbours(airports, views(ESAL=SILENT, ESGT=SILENT, ESGG=reported()))

    assert list(found) == ["ESGT"]


def test_a_stale_neighbour_is_not_offered_and_a_current_one_further_away_is() -> None:
    airports = [place("ESGT"), place("ESNE", 5), place("ESFA", 20)]
    stale = reported(age_s=91 * 60)

    found = board.neighbours(airports, views(ESGT=SILENT, ESNE=stale, ESFA=reported()))

    assert found["ESGT"]["icao"] == "ESFA"
    only_stale = board.neighbours(airports[:2], views(ESGT=SILENT, ESNE=stale))
    assert only_stale == {}


@pytest.mark.parametrize(
    "own",
    [FAILED, reported(age_s=3 * 3600), reported(category="XXX")],
    ids=["fetch failed", "own report old", "own report without a category"],
)
def test_only_an_airport_noaa_holds_nothing_for_borrows_a_line(own: dict[str, Any]) -> None:
    found = board.neighbours([place("ESGT"), place("ESGG", 18)], views(ESGT=own, ESGG=reported()))

    assert found == {}


# --- on the board -------------------------------------------------------------------------
def overview(answers: dict[str, dict[str, Any]]) -> Any:
    def answer(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "airports": {code: answers[code] for code in arguments["airports"]}}

    return answer


TRIAGE = {
    "ok": True,
    "assessment": {"severity": "unknown", "reasons": [], "unknowns": ["No METAR returned."]},
}


def board_with_a_neighbour() -> tuple[BoardService, FakeSource]:
    source = FakeSource(
        weather_overview=overview({"ESGT": SILENT, "ESGG": reported()}),
        upstream_status=STATUS,
        airport_weather=SILENT,
        disruption_triage=TRIAGE,
        airport_live_traffic={"ok": False, "error": {"code": "credentials_missing"}},
    )
    return BoardService(source, [place("ESGT"), place("ESGG", 18)]), source


async def test_the_board_shows_the_line_and_keeps_the_marker_and_banner() -> None:
    service, _ = board_with_a_neighbour()

    snapshot = await service.snapshot("europe")

    by_code = {airport["icao"]: airport for airport in snapshot["airports"]}
    esgt = by_code["ESGT"]
    assert esgt["weather"]["summary"].startswith("No report at ESGT — ESGG (18 nm away)")
    assert esgt["weather"]["neighbour"]["icao"] == "ESGG"
    assert esgt["marker"] == {
        "kind": "unknown",
        "category": None,
        "label": "Unknown",
        "scope": "weather",
    }
    assert by_code["ESGG"]["weather"]["neighbour"] is None
    # The banner counts airports with their own report: one, not two.
    assert snapshot["banner"]["label"] == "LIVE: 1 of 2 with a current METAR"


async def test_the_list_and_the_detail_label_it_the_same_way() -> None:
    service, _ = board_with_a_neighbour()
    await service.snapshot("europe")

    listed = service.airport_list("europe", "ESGT")["items"][0]
    detail = await service.detail("ESGT")

    assert listed["weather"]["summary"].startswith("No report at ESGT — ESGG")
    assert listed["marker"]["kind"] == "unknown"
    assert detail["weather"]["summary"].startswith("No report at ESGT — ESGG")
    assert detail["weather"]["state"] == "unknown"
    assert detail["marker"]["kind"] == "unknown"
    # The airport's own unknowns stay: the neighbour does not answer them.
    assert detail["weather"]["unknowns"] == []
    assert detail["weather"]["no_observation"] is True


async def test_a_neighbour_never_reaches_the_triage() -> None:
    service, source = board_with_a_neighbour()
    await service.snapshot("europe")

    triage = await service.triage("ESGT")

    assert triage == {"icao": "ESGT", **board.triage_view(TRIAGE)}
    assert triage["severity"] == "unknown"
    assert "ESGG" not in str(triage)
    assert source.calls[-1] == ("disruption_triage", {"airport": "ESGT"})


async def test_a_later_snapshot_withdraws_a_line_that_no_longer_holds() -> None:
    service, source = board_with_a_neighbour()
    await service.snapshot("europe")
    source.answers["weather_overview"] = overview({"ESGT": SILENT, "ESGG": reported(age_s=6000)})

    snapshot = await service.snapshot("europe")

    esgt = next(a for a in snapshot["airports"] if a["icao"] == "ESGT")
    assert esgt["weather"] == {"summary": "Unknown: no METAR returned", "neighbour": None}
    assert service.airport_list("europe", "ESGT")["items"][0]["weather"]["neighbour"] is None
